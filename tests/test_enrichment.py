from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.definitions.models import ProcessorDefinition
from memseek.enrichment import AnnotationConflict, enrich_once, truncate_middle
from memseek.llm.fake import fake
from memseek.llm.registry import TEXT_OUTPUT, Completion, CompletionOutput, LLMTransportError
from memseek.llm.runtime import ResolvedCompletion


async def _workspace(db_pool: Any, name: str = "enrichment") -> None:
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            (name, "a" * 64),
        )


async def _record(
    db_pool: Any,
    catalog: DefinitionCatalog,
    *,
    workspace: str = "enrichment",
    collection: str = "main",
    record_type: str = "event",
    text: str = "remember this [importance=8]",
    enriched: bool = False,
    annotations: dict[str, object] | None = None,
    annotation_meta: dict[str, object] | None = None,
    run_id: UUID | None = None,
) -> UUID:
    definition = catalog.resolve_collection(collection)
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, annotations, annotation_meta, enriched_at, run_id
            )
            values (%s, %s, %s, %s, 'entity-1', %s, %s, %s, %s,
                    case when %s then now() else null end, %s)
            returning id
            """,
            (
                workspace,
                collection,
                definition.version,
                definition.contract_hash,
                record_type,
                Jsonb({"text": text}),
                Jsonb(annotations or {}),
                Jsonb(annotation_meta or {}),
                enriched,
                run_id,
            ),
        )
        row = await result.fetchone()
        assert row is not None
        return row["id"]


async def test_required_sweep_writes_embeddings_scores_runs_and_ready_outbox(
    db_pool: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="memseek.enrichment")
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    record_id = await _record(
        db_pool,
        catalog,
        text="[importance=8] prefix " + "x" * 200 + " suffix",
    )
    fake.reset()
    # The embedding bound is part of the embedding declaration; the scorer bound
    # remains a deployment setting.
    bounded = settings.model_copy(update={"scorer_text_chars": 64})
    bounded_catalog = replace(
        catalog,
        models=catalog.models.model_copy(
            update={"embedding": catalog.models.embedding.model_copy(update={"max_text_chars": 64})}
        ),
    )

    result = await enrich_once(db_pool, bounded, bounded_catalog)

    assert result.kind == "required"
    assert (result.selected, result.ready, result.annotations_written) == (1, 1, 2)
    async with db_pool.connection() as conn:
        row_result = await conn.execute(
            """
            select enriched_at is not null as ready, vector_dims(embedding) as dimensions,
                   embedding_space, annotations, scores, annotation_meta, enrichment_meta
            from record where id = %s
            """,
            (record_id,),
        )
        row = await row_result.fetchone()
        run_result = await conn.execute(
            """
            select content from record
            where collection = '_system' and type = 'run'
            order by seq
            """
        )
        runs = await run_result.fetchall()
        job_result = await conn.execute(
            "select payload from job where kind = 'index_upsert' order by created_at"
        )
        jobs = await job_result.fetchall()
    assert row is not None
    assert row["ready"] is True
    assert row["dimensions"] == 1_536
    assert row["embedding_space"] == catalog.models.embedding.space
    assert row["annotations"]["embedding_v1"] == {"space": catalog.models.embedding.space}
    assert row["annotations"]["importance"] == {"value": 8.0}
    assert row["scores"]["importance"] == 8.0
    assert row["annotation_meta"]["importance"]["processor"] == "importance"
    embed_model = catalog.models.embedding.model
    assert row["enrichment_meta"]["embedding"]["resolved"] == f"fake:{embed_model}"
    assert len(runs) == 2
    assert {run["content"]["target_record_id"] for run in runs} == {str(record_id)}
    projected_ids = {item["id"] for job in jobs for item in job["payload"].get("records", [])}
    assert str(record_id) in projected_ids
    expected_embed, was_truncated = truncate_middle(
        "[importance=8] prefix " + "x" * 200 + " suffix", 64
    )
    assert was_truncated is True
    assert fake.embedding_calls[0].texts == (expected_embed,)
    assert "[importance=8]" in fake.completion_calls[0].prompt
    assert fake.completion_calls[0].output_mode == "text"
    completed_logs = [record for record in caplog.records if record.msg == "run.completed"]
    assert len(completed_logs) == 2
    event_fields = [getattr(record, "event_fields", {}) for record in completed_logs]
    assert {fields["processor"] for fields in event_fields} == {
        "embedding_v1",
        "importance",
    }
    assert all(fields["workspace"] == "enrichment" for fields in event_fields)
    assert all("text" not in fields for fields in event_fields)


async def test_transport_failures_use_terminal_defaults_and_do_not_block_ready(
    db_pool: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    record_id = await _record(db_pool, catalog)

    async def failed(*_args: object, **_kwargs: object) -> object:
        raise LLMTransportError("offline")

    monkeypatch.setattr("memseek.enrichment.embed", failed)
    monkeypatch.setattr("memseek.enrichment.complete", failed)

    result = await enrich_once(db_pool, settings, catalog)

    assert result.ready == 1
    async with db_pool.connection() as conn:
        query = await conn.execute(
            """
            select enriched_at is not null as ready, embedding, annotations,
                   scores, enrichment_error
            from record where id = %s
            """,
            (record_id,),
        )
        row = await query.fetchone()
    assert row is not None
    assert row["ready"] is True
    assert row["embedding"] is None
    assert row["annotations"]["embedding_v1"] == {"space": catalog.models.embedding.space}
    assert row["annotations"]["importance"] == {"value": 5.0}
    assert row["scores"]["importance"] == 5.0
    assert "embedding_transport" in row["enrichment_error"]
    assert "scorer" in row["enrichment_error"]


async def test_malformed_scorer_is_corrected_once_before_persisting(
    db_pool: Any, settings: Settings
) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    record_id = await _record(db_pool, catalog)
    fake.reset()
    fake.enqueue("not-json", "[9]")

    result = await enrich_once(db_pool, settings, catalog)

    assert (result.ready, result.annotations_written) == (1, 2)
    assert len(fake.completion_calls) == 2
    assert "prior result was invalid" in fake.completion_calls[1].prompt
    assert all(call.output_mode == "text" for call in fake.completion_calls)
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, scores, enrichment_error from record where id = %s",
                (record_id,),
            )
        ).fetchone()
        run = await (
            await conn.execute(
                """
                select content from record
                where collection = '_system'
                  and type = 'run'
                  and content->>'processor' = 'importance'
                """
            )
        ).fetchone()
    assert row is not None
    assert run is not None
    assert row["annotations"]["importance"] == {"value": 9.0}
    assert row["scores"]["importance"] == 9.0
    assert row["enrichment_error"] is None
    assert run["content"]["warnings"] == ["scorer_correction_call"]
    assert [call["outcome"] for call in run["content"]["model_calls"]] == ["ok", "ok"]


async def test_malformed_scorer_and_correction_fall_back_once(
    db_pool: Any, settings: Settings
) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    record_id = await _record(db_pool, catalog)
    fake.reset()
    fake.enqueue("{}", '"still invalid"')

    result = await enrich_once(db_pool, settings, catalog)

    assert (result.ready, result.annotations_written) == (1, 2)
    assert len(fake.completion_calls) == 2
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, scores, enrichment_error from record where id = %s",
                (record_id,),
            )
        ).fetchone()
        run = await (
            await conn.execute(
                """
                select content from record
                where collection = '_system'
                  and type = 'run'
                  and content->>'processor' = 'importance'
                """
            )
        ).fetchone()
    assert row is not None
    assert run is not None
    assert row["annotations"]["importance"] == {"value": 5.0}
    assert row["scores"]["importance"] == 5.0
    assert "scorer:" in row["enrichment_error"]
    assert run["content"]["warnings"] == ["scorer_default"]
    assert [call["outcome"] for call in run["content"]["model_calls"]] == ["ok", "ok"]


def _sentiment_catalog(
    catalog: DefinitionCatalog, *, required: bool, client: bool = False
) -> DefinitionCatalog:
    collections = dict(catalog.collections)
    main = catalog.resolve_collection("main")
    collections[(main.name, main.version)] = main.model_copy(
        update={
            "required_processors": ("sentiment_v1",) if required else (),
            "optional_processors": () if required else ("sentiment_v1",),
        }
    )
    processors = dict(catalog.processors)
    if client:
        processors["sentiment_v1"] = processors["sentiment_v1"].model_copy(
            update={"source": "client", "model": None, "prompt": None}
        )
    return replace(
        catalog,
        collections=MappingProxyType(collections),
        processors=MappingProxyType(processors),
    )


async def test_generic_annotations_share_one_token_packed_call_batch(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    await _workspace(db_pool)
    first = await _record(
        db_pool,
        catalog,
        record_type="chat",
        text="hello [sentiment=positive]",
        enriched=True,
    )
    second = await _record(
        db_pool,
        catalog,
        record_type="message",
        text="goodbye [sentiment=negative]",
        enriched=True,
    )
    fake.reset()

    result = await enrich_once(db_pool, settings, catalog)

    assert result.kind == "optional"
    assert (result.selected, result.ready, result.annotations_written) == (2, 0, 2)
    assert len(fake.completion_calls) == 1
    assert fake.completion_calls[0].output_mode == "text"
    assert fake.completion_calls[0].output_schema is None
    async with db_pool.connection() as conn:
        query = await conn.execute(
            """
            select id, annotations, annotation_meta
            from record where id = any(%s::uuid[]) order by id
            """,
            ([first, second],),
        )
        rows = await query.fetchall()
        runs_query = await conn.execute(
            """
            select content->>'call_batch_id' as call_batch_id
            from record
            where collection = '_system' and type = 'run'
            """
        )
        runs = await runs_query.fetchall()
        jobs_query = await conn.execute("select payload from job where kind = 'index_upsert'")
        jobs = await jobs_query.fetchall()
    labels = {row["annotations"]["sentiment_v1"]["label"] for row in rows}
    assert labels == {"positive", "negative"}
    assert len({run["call_batch_id"] for run in runs}) == 1
    projected_ids = {item["id"] for job in jobs for item in job["payload"].get("records", [])}
    assert {str(first), str(second)}.isdisjoint(projected_ids)


async def test_concurrent_exact_annotation_retry_is_write_once(
    db_pool: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    await _workspace(db_pool)
    target = await _record(db_pool, catalog, record_type="chat", enriched=True)
    first_started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def delayed_complete(
        settings_arg: Settings,
        catalog_arg: DefinitionCatalog,
        alias: str,
        system: str,
        prompt: str,
        *,
        params: Mapping[str, object] | None = None,
        output: CompletionOutput = TEXT_OUTPUT,
        max_output_tokens: int | None = None,
        context: str | None = None,
    ) -> ResolvedCompletion:
        nonlocal call_count
        del settings_arg, catalog_arg, alias, system, prompt
        del params, output, max_output_tokens, context
        call_count += 1
        first_started.set()
        if call_count == 2:
            both_started.set()
        await release.wait()
        return ResolvedCompletion(
            completion=Completion('[{"label":"positive","confidence":0.75}]'),
            resolved="fake:cheap-model",
            effective_params=MappingProxyType({}),
            attempts=(),
        )

    monkeypatch.setattr("memseek.enrichment.complete", delayed_complete)
    first = asyncio.create_task(enrich_once(db_pool, settings, catalog))
    await asyncio.wait_for(first_started.wait(), timeout=2)
    second = asyncio.create_task(enrich_once(db_pool, settings, catalog))
    await asyncio.wait_for(both_started.wait(), timeout=2)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert sorted(result.annotations_written for result in results) == [0, 1]
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, annotation_meta from record where id = %s", (target,)
            )
        ).fetchone()
        run_count = await (
            await conn.execute(
                """
                select count(*) as count from record
                where collection = '_system'
                  and type = 'run'
                  and content->>'processor' = 'sentiment_v1'
                """
            )
        ).fetchone()
    assert row is not None
    assert run_count == {"count": 1}
    assert row["annotations"]["sentiment_v1"] == {
        "label": "positive",
        "confidence": 0.75,
    }
    assert row["annotation_meta"]["sentiment_v1"]["run_id"]


async def test_concurrent_different_annotation_retry_conflicts_without_overwrite(
    db_pool: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    await _workspace(db_pool)
    target = await _record(db_pool, catalog, record_type="chat", enriched=True)
    first_started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def delayed_complete(
        settings_arg: Settings,
        catalog_arg: DefinitionCatalog,
        alias: str,
        system: str,
        prompt: str,
        *,
        params: Mapping[str, object] | None = None,
        output: CompletionOutput = TEXT_OUTPUT,
        max_output_tokens: int | None = None,
        context: str | None = None,
    ) -> ResolvedCompletion:
        nonlocal call_count
        del settings_arg, catalog_arg, alias, system, prompt
        del params, output, max_output_tokens, context
        ordinal = call_count
        call_count += 1
        first_started.set()
        if call_count == 2:
            both_started.set()
        await release.wait()
        completion = (
            '[{"label":"positive","confidence":1.0}]'
            if ordinal == 0
            else '[{"label":"negative","confidence":1.0}]'
        )
        return ResolvedCompletion(
            completion=Completion(completion),
            resolved="fake:cheap-model",
            effective_params=MappingProxyType({}),
            attempts=(),
        )

    monkeypatch.setattr("memseek.enrichment.complete", delayed_complete)
    first = asyncio.create_task(enrich_once(db_pool, settings, catalog))
    await asyncio.wait_for(first_started.wait(), timeout=2)
    second = asyncio.create_task(enrich_once(db_pool, settings, catalog))
    await asyncio.wait_for(both_started.wait(), timeout=2)
    release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=2
    )

    assert sum(isinstance(outcome, AnnotationConflict) for outcome in outcomes) == 1
    assert sum(getattr(outcome, "annotations_written", 0) == 1 for outcome in outcomes) == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute("select annotations from record where id = %s", (target,))
        ).fetchone()
        run_count = await (
            await conn.execute(
                """
                select count(*) as count from record
                where collection = '_system'
                  and type = 'run'
                  and content->>'processor' = 'sentiment_v1'
                """
            )
        ).fetchone()
    assert row is not None
    assert run_count == {"count": 1}
    assert row["annotations"]["sentiment_v1"] in (
        {"label": "positive", "confidence": 1.0},
        {"label": "negative", "confidence": 1.0},
    )


async def test_existing_client_value_wins_and_absent_required_client_uses_default(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=True, client=True)
    await _workspace(db_pool)
    supplied = {"label": "positive", "confidence": 0.7}
    existing = await _record(
        db_pool,
        catalog,
        record_type="chat",
        annotations={"sentiment_v1": supplied},
        annotation_meta={"sentiment_v1": {"source": "client"}},
    )
    missing = await _record(db_pool, catalog, record_type="chat")

    result = await enrich_once(db_pool, settings, catalog)

    assert result.ready == 2
    async with db_pool.connection() as conn:
        query = await conn.execute(
            "select id, annotations, annotation_meta from record where id = any(%s::uuid[])",
            ([existing, missing],),
        )
        rows = {row["id"]: row for row in await query.fetchall()}
    assert rows[existing]["annotations"]["sentiment_v1"] == supplied
    assert rows[existing]["annotation_meta"]["sentiment_v1"] == {"source": "client"}
    assert rows[missing]["annotations"]["sentiment_v1"] == {
        "label": "neutral",
        "confidence": 0,
    }
    assert "required_client_default" in rows[missing]["annotation_meta"]["sentiment_v1"]["warnings"]


async def test_optional_selection_filters_before_limit_so_old_rows_cannot_starve_work(
    db_pool: Any, settings: Settings
) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    no_optional = catalog.resolve_collection("prompt_snapshots")
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, enriched_at
            )
            select 'enrichment', 'prompt_snapshots', %s, %s,
                   'old-' || value::text, 'snapshot',
                   jsonb_build_object('text', 'already complete'), now()
            from generate_series(1, 300) value
            """,
            (no_optional.version, no_optional.contract_hash),
        )
    target = await _record(
        db_pool,
        catalog,
        collection="calendar_events",
        record_type="event",
        enriched=True,
    )
    fake.reset()

    result = await enrich_once(db_pool, settings, catalog)

    assert result.kind == "optional"
    assert result.selected == 1
    async with db_pool.connection() as conn:
        query = await conn.execute("select annotations from record where id = %s", (target,))
        row = await query.fetchone()
    assert row is not None
    assert row["annotations"]["embedding_v1"] == {"space": catalog.models.embedding.space}


async def test_optional_without_default_persists_terminal_attempt_and_does_not_hot_loop(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    processors = dict(catalog.processors)
    processors["sentiment_v1"] = processors["sentiment_v1"].model_copy(
        update={"source": "constant", "model": None, "prompt": None, "default_output": None}
    )
    catalog = replace(catalog, processors=MappingProxyType(processors))
    await _workspace(db_pool)
    target = await _record(
        db_pool,
        catalog,
        record_type="chat",
        text="best effort has no configured value",
        enriched=True,
    )

    first = await enrich_once(db_pool, settings, catalog)
    second = await enrich_once(db_pool, settings, catalog)

    assert (first.kind, first.selected, first.annotations_written) == ("optional", 1, 0)
    assert second.kind == "none"
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, enrichment_meta from record where id = %s", (target,)
            )
        ).fetchone()
        run = await (
            await conn.execute(
                """
                select content from record
                where collection = '_system'
                  and type = 'run'
                  and content->>'processor' = 'sentiment_v1'
                """
            )
        ).fetchone()
    assert row is not None
    assert run is not None
    assert "sentiment_v1" not in row["annotations"]
    assert row["enrichment_meta"]["sentiment_v1"]["terminal"] is True
    assert run["content"]["status"] == "failed"


async def test_schema_invalid_completion_keeps_successful_provider_call_in_run_audit(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    await _workspace(db_pool)
    target = await _record(db_pool, catalog, record_type="chat", enriched=True)
    fake.reset()
    fake.enqueue("not-json")

    result = await enrich_once(db_pool, settings, catalog)

    assert result.annotations_written == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, enrichment_error from record where id = %s", (target,)
            )
        ).fetchone()
        run = await (
            await conn.execute(
                """
                select content from record
                where collection = '_system'
                  and content->>'processor' = 'sentiment_v1'
                """
            )
        ).fetchone()
    assert row is not None
    assert run is not None
    assert row["annotations"]["sentiment_v1"] == {"label": "neutral", "confidence": 0}
    assert "annotation" in row["enrichment_error"]
    assert len(run["content"]["model_calls"]) == 1
    assert run["content"]["model_calls"][0]["outcome"] == "ok"
    assert "annotation_default" in run["content"]["warnings"]


async def test_generic_annotation_batches_split_at_the_actual_prompt_budget(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _sentiment_catalog(load_definition_catalog(settings), required=False)
    await _workspace(db_pool)
    for sentiment in ("positive", "negative"):
        await _record(
            db_pool,
            catalog,
            record_type="chat",
            text=f"[sentiment={sentiment}] " + "x" * 600,
            enriched=True,
        )
    fake.reset()
    bounded = settings.model_copy(update={"max_prompt_tokens": 400})

    result = await enrich_once(db_pool, bounded, catalog)

    assert result.annotations_written == 2
    assert [len(call.prompt) for call in fake.completion_calls]
    assert len(fake.completion_calls) == 2


async def test_embedding_batches_are_capped_at_sixty_four(db_pool: Any, settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    for index in range(65):
        await _record(db_pool, catalog, text=f"record {index}")
    fake.reset()

    result = await enrich_once(db_pool, settings.model_copy(update={"enrich_batch": 65}), catalog)

    assert (result.selected, result.ready, result.annotations_written) == (65, 65, 130)
    assert [len(call.texts) for call in fake.embedding_calls] == [64, 1]


def _required_client_group_catalog(catalog: DefinitionCatalog) -> DefinitionCatalog:
    scorer = ProcessorDefinition.model_validate(
        {
            "name": "client_gate",
            "kind": "score",
            "source": "client",
            "scale": [0, 1],
            "input": {"collections": ["main"]},
        }
    )
    main = catalog.resolve_collection("main").model_copy(
        update={"required_processors": (scorer.name,)}
    )
    return replace(
        catalog,
        processors={**catalog.processors, scorer.name: scorer},
        score_names=catalog.score_names | {scorer.name},
        score_owners={**catalog.score_owners, scorer.name: scorer.name},
        collections={**catalog.collections, (main.name, main.version): main},
        processor_config_hashes={**catalog.processor_config_hashes, scorer.name: "c" * 64},
    )


async def test_derivation_output_group_readiness_is_all_or_none(
    db_pool: Any, settings: Settings
) -> None:
    catalog = _required_client_group_catalog(load_definition_catalog(settings))
    await _workspace(db_pool)
    run_id = UUID("00000000-0000-4000-8000-000000000123")
    blocked = await _record(db_pool, catalog, run_id=run_id)
    otherwise_ready = await _record(
        db_pool,
        catalog,
        collection="prompt_snapshots",
        record_type="snapshot",
        run_id=run_id,
    )

    result = await enrich_once(db_pool, settings, catalog)

    assert (result.selected, result.ready) == (2, 0)
    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "select id, enriched_at from record where id = any(%s::uuid[])",
                ([blocked, otherwise_ready],),
            )
        ).fetchall()
    assert {row["id"] for row in rows} == {blocked, otherwise_ready}
    assert all(row["enriched_at"] is None for row in rows)


async def test_derivation_group_accepts_fifty_outputs_and_rejects_fifty_one(
    db_pool: Any, settings: Settings
) -> None:
    catalog = load_definition_catalog(settings)
    await _workspace(db_pool)
    allowed_run = UUID("00000000-0000-4000-8000-000000000050")
    for index in range(50):
        await _record(
            db_pool,
            catalog,
            collection="prompt_snapshots",
            record_type="snapshot",
            text=f"allowed {index}",
            run_id=allowed_run,
        )

    allowed = await enrich_once(db_pool, settings, catalog)

    assert (allowed.selected, allowed.ready) == (50, 50)
    oversized_run = UUID("00000000-0000-4000-8000-000000000051")
    for index in range(51):
        await _record(
            db_pool,
            catalog,
            collection="prompt_snapshots",
            record_type="snapshot",
            text=f"oversized {index}",
            run_id=oversized_run,
        )
    with pytest.raises(RuntimeError, match="50-output"):
        await enrich_once(db_pool, settings, catalog)
