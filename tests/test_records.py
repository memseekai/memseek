"""M1 canonical public-record validation and insertion tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.definitions.models import ProcessorDefinition
from memseek.records import (
    DedupeConflict,
    RecordBatchRequest,
    RecordValidationError,
    insert_public_records,
)


@pytest.fixture(scope="session")
def catalog(settings: Settings) -> DefinitionCatalog:
    return load_definition_catalog(settings)


async def _insert(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    workspace: str,
    records: list[dict[str, Any]],
):
    request = RecordBatchRequest.model_validate({"records": records})
    return await insert_public_records(
        db_pool,
        workspace=workspace,
        request=request,
        catalog=catalog,
        settings=settings,
    )


async def _row(db_pool: DatabasePool, record_id: object) -> dict[str, Any]:
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select id,
                   seq,
                   collection,
                   collection_version,
                   collection_hash,
                   entity,
                   key,
                   content,
                   scores,
                   annotations,
                   annotation_meta,
                   depth,
                   derived_from,
                   occurred_at,
                   enriched_at
            from record
            where id = %s
            """,
            (record_id,),
        )
        row = await result.fetchone()
    assert row is not None
    return row


async def test_text_projection_schema_format_and_ready_outbox(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "calendar")
    result = await _insert(
        db_pool,
        settings,
        catalog,
        "calendar",
        [
            {
                "entity": "maria",
                "collection": "calendar_events",
                "type": "meeting",
                "content": {
                    "title": "Planning",
                    "starts_at": "2026-07-20T14:00:00Z",
                    "ends_at": "2026-07-20T14:30:00Z",
                    "attendees": ["maria@example.test"],
                    "external_id": "event-1",
                },
            }
        ],
    )

    assert result.model_dump(mode="json") == {
        "inserted": [{"index": 0, "id": str(result.inserted[0].id), "ready": True}],
        "duplicates": [],
    }
    row = await _row(db_pool, result.inserted[0].id)
    assert row["collection_version"] == 1
    assert row["collection_hash"] == catalog.resolve_collection("calendar_events").contract_hash
    assert row["content"]["text"] == (
        "Planning starts 2026-07-20T14:00:00Z and ends 2026-07-20T14:30:00Z; "
        'attendees: ["maria@example.test"]'
    )
    assert row["enriched_at"] is not None
    async with db_pool.connection() as conn:
        jobs = await (
            await conn.execute(
                "select kind, payload from job where workspace = %s",
                ("calendar",),
            )
        ).fetchall()
    assert jobs == [
        {
            "kind": "index_upsert",
            "payload": {
                "records": [
                    {
                        "id": str(result.inserted[0].id),
                        "collection": "calendar_events",
                    }
                ]
            },
        }
    ]


async def test_required_processors_keep_record_unready_without_outbox(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "unready")
    result = await _insert(
        db_pool,
        settings,
        catalog,
        "unready",
        [{"entity": "maria", "type": "event", "text": "Waiting for enrichment."}],
    )
    assert result.inserted[0].ready is False
    row = await _row(db_pool, result.inserted[0].id)
    assert row["enriched_at"] is None
    assert row["annotations"] == {}
    assert row["annotation_meta"] == {}
    async with db_pool.connection() as conn:
        count = await (await conn.execute("select count(*) as n from job")).fetchone()
    assert count == {"n": 0}


async def test_schema_or_declared_field_failure_rolls_back_whole_batch(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "atomic-schema")
    common = {
        "entity": "maria",
        "collection": "calendar_events",
        "type": "meeting",
        "text": "Supplied text",
    }
    with pytest.raises(RecordValidationError) as raised:
        await _insert(
            db_pool,
            settings,
            catalog,
            "atomic-schema",
            [
                {
                    **common,
                    "content": {
                        "title": "Valid",
                        "starts_at": "2026-07-20T14:00:00Z",
                        "ends_at": "2026-07-20T14:30:00Z",
                    },
                },
                {
                    **common,
                    "content": {
                        "title": "Invalid attendee",
                        "starts_at": "2026-07-20T14:00:00Z",
                        "ends_at": "2026-07-20T14:30:00Z",
                        "attendees": [42],
                    },
                },
            ],
        )
    assert raised.value.code == "field_type"
    assert raised.value.index == 1
    async with db_pool.connection() as conn:
        count = await (await conn.execute("select count(*) as n from record")).fetchone()
    assert count == {"n": 0}


async def test_schema_format_and_top_level_text_consistency_are_enforced(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "formats")
    with pytest.raises(RecordValidationError) as invalid_format:
        await _insert(
            db_pool,
            settings,
            catalog,
            "formats",
            [
                {
                    "entity": "maria",
                    "collection": "calendar_events",
                    "type": "meeting",
                    "text": "Invalid time",
                    "content": {
                        "title": "Invalid time",
                        "starts_at": "tomorrow",
                        "ends_at": "2026-07-20T14:30:00Z",
                    },
                }
            ],
        )
    assert invalid_format.value.code == "field_type"

    with pytest.raises(RecordValidationError) as mismatch:
        await _insert(
            db_pool,
            settings,
            catalog,
            "formats",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "canonical",
                    "content": {"text": "different"},
                }
            ],
        )
    assert mismatch.value.code == "text_mismatch"


async def test_exact_dedupe_is_idempotent_but_conflicts_are_atomic(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "dedupe")
    payload = {
        "entity": "maria",
        "type": "event",
        "text": "Same immutable event",
        "dedupe_key": "crm:event:123",
    }
    first = await _insert(db_pool, settings, catalog, "dedupe", [payload])
    retry = await _insert(db_pool, settings, catalog, "dedupe", [payload])
    assert retry.inserted == ()
    assert retry.duplicates[0].id == first.inserted[0].id

    with pytest.raises(DedupeConflict) as raised:
        await _insert(
            db_pool,
            settings,
            catalog,
            "dedupe",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Would otherwise insert",
                    "dedupe_key": "new-in-rolled-back-batch",
                },
                {**payload, "text": "Conflicting immutable event"},
            ],
        )
    assert raised.value.code == "dedupe_conflict"
    assert raised.value.index == 1
    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "select dedupe_key from record where workspace = %s order by seq",
                ("dedupe",),
            )
        ).fetchall()
    assert rows == [{"dedupe_key": "crm:event:123"}]


async def test_explicit_occurred_at_participates_in_dedupe_comparison(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "dedupe-time")
    base = {
        "entity": "maria",
        "type": "event",
        "text": "Timed event",
        "dedupe_key": "timed",
        "occurred_at": "2026-07-01T10:22:00Z",
    }
    first = await _insert(db_pool, settings, catalog, "dedupe-time", [base])
    retry = await _insert(db_pool, settings, catalog, "dedupe-time", [base])
    assert retry.duplicates[0].id == first.inserted[0].id
    with pytest.raises(DedupeConflict):
        await _insert(
            db_pool,
            settings,
            catalog,
            "dedupe-time",
            [{**base, "occurred_at": "2026-07-01T10:23:00Z"}],
        )


async def test_tombstone_is_ready_keyed_and_preserves_continuity_depth(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "tombstones")
    parent = await _insert(
        db_pool,
        settings,
        catalog,
        "tombstones",
        [
            {
                "entity": "maria",
                "collection": "profiles",
                "type": "fact",
                "key": "role",
                "text": "Leads the platform team",
            }
        ],
    )
    tombstone = await _insert(
        db_pool,
        settings,
        catalog,
        "tombstones",
        [
            {
                "entity": "maria",
                "collection": "profiles",
                "type": "fact",
                "key": "role",
                "text": "",
                "tombstone": True,
                "derived_from": [str(parent.inserted[0].id)],
            }
        ],
    )
    assert tombstone.inserted[0].ready is True
    row = await _row(db_pool, tombstone.inserted[0].id)
    assert row["content"] == {"text": "", "tombstone": True}
    assert row["derived_from"] == [parent.inserted[0].id]
    assert row["depth"] == 0


async def test_parents_are_same_workspace_canonical_and_depth_bounded(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "parents-a")
    await create_workspace(db_pool, "parents-b")
    parents = await _insert(
        db_pool,
        settings,
        catalog,
        "parents-a",
        [
            {"entity": "maria", "type": "event", "text": "Parent A"},
            {"entity": "maria", "type": "event", "text": "Parent B"},
        ],
    )
    child = await _insert(
        db_pool,
        settings,
        catalog,
        "parents-a",
        [
            {
                "entity": "maria",
                "type": "event",
                "text": "Child",
                "derived_from": [
                    str(parents.inserted[1].id),
                    str(parents.inserted[0].id),
                ],
            }
        ],
    )
    row = await _row(db_pool, child.inserted[0].id)
    assert row["derived_from"] == sorted([parents.inserted[0].id, parents.inserted[1].id], key=str)
    assert row["depth"] == 1

    with pytest.raises(RecordValidationError) as foreign:
        await _insert(
            db_pool,
            settings,
            catalog,
            "parents-b",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Foreign child",
                    "derived_from": [str(parents.inserted[0].id)],
                }
            ],
        )
    assert foreign.value.code == "parent_workspace"

    shallow = settings.model_copy(update={"max_derivation_depth": 1})
    with pytest.raises(RecordValidationError) as too_deep:
        await _insert(
            db_pool,
            shallow,
            catalog,
            "parents-a",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Grandchild",
                    "derived_from": [str(child.inserted[0].id)],
                }
            ],
        )
    assert too_deep.value.code == "depth_limit"


async def test_existing_cyclic_lineage_is_not_extended(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "cycles")
    parent = await _insert(
        db_pool,
        settings,
        catalog,
        "cycles",
        [{"entity": "maria", "type": "event", "text": "Parent"}],
    )
    parent_id = parent.inserted[0].id
    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set derived_from = %s where id = %s",
            ([parent_id], parent_id),
        )
    with pytest.raises(RecordValidationError) as raised:
        await _insert(
            db_pool,
            settings,
            catalog,
            "cycles",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Must not extend corruption",
                    "derived_from": [str(parent_id)],
                }
            ],
        )
    assert raised.value.code == "parent_cycle"


def _catalog_with_clients(catalog: DefinitionCatalog) -> DefinitionCatalog:
    scorer = ProcessorDefinition.model_validate(
        {
            "name": "client_score",
            "kind": "score",
            "source": "client",
            "scale": [0, 10],
            "input": {"collections": ["main"]},
        }
    )
    annotation = ProcessorDefinition.model_validate(
        {
            "name": "client_note",
            "kind": "json",
            "source": "client",
            "input": {"collections": ["main"], "types": ["event"]},
            "output_schema": {
                "type": "object",
                "required": ["label", "confidence"],
                "properties": {
                    "label": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "additionalProperties": False,
            },
            "score_fields": {"client_confidence": "confidence"},
        }
    )
    return replace(
        catalog,
        processors={
            **catalog.processors,
            scorer.name: scorer,
            annotation.name: annotation,
        },
        score_names=catalog.score_names | {scorer.name, "client_confidence"},
        score_owners={
            **catalog.score_owners,
            scorer.name: scorer.name,
            "client_confidence": annotation.name,
        },
        processor_config_hashes={
            **catalog.processor_config_hashes,
            scorer.name: "a" * 64,
            annotation.name: "b" * 64,
        },
    )


def _catalog_with_required_client_scorer(catalog: DefinitionCatalog) -> DefinitionCatalog:
    clients = _catalog_with_clients(catalog)
    main = clients.resolve_collection("main").model_copy(
        update={
            "required_processors": (
                *clients.resolve_collection("main").required_processors,
                "client_score",
            )
        }
    )
    return replace(
        clients,
        collections={**clients.collections, ("main", main.version): main},
    )


async def test_client_outputs_are_validated_clamped_mirrored_and_hash_only(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "client-output")
    clients = _catalog_with_clients(catalog)
    payload = {
        "entity": "maria",
        "type": "event",
        "text": "Client annotated",
        "dedupe_key": "client:event:1",
        "scores": {"client_score": 99},
        "annotations": {"client_note": {"label": "verified", "confidence": 0.75}},
    }
    result = await _insert(
        db_pool,
        settings,
        clients,
        "client-output",
        [payload],
    )
    row = await _row(db_pool, result.inserted[0].id)
    assert row["scores"] == {"client_score": 10.0, "client_confidence": 0.75}
    assert row["annotations"] == {
        "client_score": {"value": 10.0},
        "client_note": {"label": "verified", "confidence": 0.75},
    }
    assert set(row["annotation_meta"]) == {"client_score", "client_note"}
    for name, metadata in row["annotation_meta"].items():
        assert metadata["processor"] == name
        assert metadata["source"] == "client"
        assert metadata["source_record_id"] == str(result.inserted[0].id)
        assert len(metadata["processor_config_hash"]) == 64
        assert len(metadata["output_hash"]) == 64
        assert len(metadata["run_id"]) == 36

    async with db_pool.connection() as conn:
        runs = await (
            await conn.execute(
                """
                select id, content, depth, derived_from
                from record
                where collection = '_system' and type = 'run'
                order by content->>'processor'
                """
            )
        ).fetchall()
        jobs = await (
            await conn.execute(
                "select payload from job where kind = 'index_upsert' order by created_at"
            )
        ).fetchall()
    assert len(runs) == 2
    assert {run["content"]["processor"] for run in runs} == {
        "client_score",
        "client_note",
    }
    assert {metadata["run_id"] for metadata in row["annotation_meta"].values()} == {
        str(run["id"]) for run in runs
    }
    for run in runs:
        assert run["content"]["operation"] == "annotate"
        assert run["content"]["status"] == "ok"
        assert run["content"]["source"] == "client"
        assert run["content"]["target_record_id"] == str(result.inserted[0].id)
        assert run["content"]["model_calls"] == []
        assert run["derived_from"] == [result.inserted[0].id]
        assert run["depth"] == row["depth"]
    assert len(jobs) == 1
    assert {item["id"] for item in jobs[0]["payload"]["records"]} == {
        str(run["id"]) for run in runs
    }

    retry = await _insert(db_pool, settings, clients, "client-output", [payload])
    assert retry.duplicates[0].id == result.inserted[0].id
    with pytest.raises(DedupeConflict):
        await _insert(
            db_pool,
            settings,
            clients,
            "client-output",
            [
                {
                    **payload,
                    "dedupe_key": "rolled-back-client",
                    "text": "This batch must roll back",
                },
                {**payload, "text": "Conflicts with the original"},
            ],
        )
    async with db_pool.connection() as conn:
        counts = await (
            await conn.execute(
                """
                select count(*) filter (where collection <> '_system') as targets,
                       count(*) filter (where collection = '_system') as runs
                from record
                """
            )
        ).fetchone()
        job_count = await (await conn.execute("select count(*) as n from job")).fetchone()
    assert counts == {"targets": 1, "runs": 2}
    assert job_count == {"n": 1}


async def test_required_client_scorer_must_be_supplied_at_ingest(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "required-client")
    required_client = _catalog_with_required_client_scorer(catalog)
    with pytest.raises(RecordValidationError) as missing:
        await _insert(
            db_pool,
            settings,
            required_client,
            "required-client",
            [{"entity": "maria", "type": "event", "text": "No client score"}],
        )
    assert missing.value.code == "required_client_output"

    accepted = await _insert(
        db_pool,
        settings,
        required_client,
        "required-client",
        [
            {
                "entity": "maria",
                "type": "event",
                "text": "Client score supplied",
                "scores": {"client_score": 7},
            }
        ],
    )
    row = await _row(db_pool, accepted.inserted[0].id)
    assert row["annotations"]["client_score"] == {"value": 7.0}
    tombstone = await _insert(
        db_pool,
        settings,
        required_client,
        "required-client",
        [
            {
                "entity": "maria",
                "type": "event",
                "key": "retired",
                "tombstone": True,
                "derived_from": [str(accepted.inserted[0].id)],
            }
        ],
    )
    assert tombstone.inserted[0].ready is True


async def test_non_client_or_malformed_client_outputs_are_rejected(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "client-invalid")
    clients = _catalog_with_clients(catalog)
    with pytest.raises(RecordValidationError) as scorer:
        await _insert(
            db_pool,
            settings,
            clients,
            "client-invalid",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Invalid scorer source",
                    "scores": {"importance": 8},
                }
            ],
        )
    assert scorer.value.code == "client_scorer"

    with pytest.raises(RecordValidationError) as annotation:
        await _insert(
            db_pool,
            settings,
            clients,
            "client-invalid",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Malformed annotation",
                    "annotations": {"client_note": {"label": "missing confidence"}},
                }
            ],
        )
    assert annotation.value.code == "client_annotation_schema"


async def test_client_run_bound_failure_rolls_back_target_and_metadata(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "client-run-bound")
    clients = _catalog_with_clients(catalog)
    with pytest.raises(RecordValidationError) as raised:
        await _insert(
            db_pool,
            settings.model_copy(update={"max_run_content_bytes": 1}),
            clients,
            "client-run-bound",
            [
                {
                    "entity": "maria",
                    "type": "event",
                    "text": "Must roll back",
                    "scores": {"client_score": 5},
                }
            ],
        )
    assert raised.value.code == "run_too_large"
    async with db_pool.connection() as conn:
        counts = await (await conn.execute("select count(*) as n from record")).fetchone()
    assert counts == {"n": 0}


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (
            {"entity": "maria", "collection": "_system", "type": "event", "text": "x"},
            "reserved_collection",
        ),
        ({"entity": "maria", "type": "run", "text": "x"}, "reserved_type"),
        ({"entity": "*", "type": "event", "text": "x"}, "entity"),
        (
            {
                "entity": "maria",
                "collection": "profiles",
                "type": "fact",
                "text": "key omitted",
            },
            "record_mode",
        ),
        (
            {
                "entity": "maria",
                "collection": "calendar_events",
                "type": "meeting",
                "key": "not-an-event",
                "text": "x",
                "content": {
                    "title": "x",
                    "starts_at": "2026-07-20T14:00:00Z",
                    "ends_at": "2026-07-20T14:30:00Z",
                },
            },
            "record_mode",
        ),
    ],
)
async def test_reserved_names_entities_and_collection_modes(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    record: dict[str, Any],
    code: str,
) -> None:
    await create_workspace(db_pool, "validation")
    with pytest.raises(RecordValidationError) as raised:
        await _insert(db_pool, settings, catalog, "validation", [record])
    assert raised.value.code == code


async def test_runtime_batch_text_and_content_bounds(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    await create_workspace(db_pool, "bounds")
    one = {"entity": "maria", "type": "event", "text": "x"}
    with pytest.raises(RecordValidationError) as batch:
        await _insert(
            db_pool,
            settings.model_copy(update={"max_batch": 1}),
            catalog,
            "bounds",
            [one, one],
        )
    assert batch.value.code == "batch_too_large"

    with pytest.raises(RecordValidationError) as text:
        await _insert(
            db_pool,
            settings.model_copy(update={"max_text_chars": 2}),
            catalog,
            "bounds",
            [{**one, "text": "abc"}],
        )
    assert text.value.code == "text_too_large"

    with pytest.raises(RecordValidationError) as content:
        await _insert(
            db_pool,
            settings.model_copy(update={"max_content_bytes": 10}),
            catalog,
            "bounds",
            [one],
        )
    assert content.value.code == "content_too_large"
