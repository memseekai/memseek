from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from reference_catalog import declare_test_sources, materialize_reference_catalog
from support_inbox import CATALOG, TOPICS, choice, noul, score, script_inbox

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, DefinitionError, load_definition_catalog
from memseek.enrichment import enrich_once
from memseek.llm.fake import fake
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

ENTITY = "customer:northwind"


@pytest.fixture
def inbox_settings(bare_settings: Settings) -> Settings:
    return bare_settings.model_copy(update={"catalog_file": CATALOG / "catalog.yaml"})


@pytest.fixture
def catalog(inbox_settings: Settings) -> DefinitionCatalog:
    return load_definition_catalog(inbox_settings)


async def _tickets(
    db_pool: DatabasePool, settings: Settings, catalog: DefinitionCatalog, *texts: str
) -> tuple[str, list[UUID]]:
    credential = await create_workspace(db_pool, "judged")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=tuple(
                PublicRecordInput(entity=ENTITY, collection="tickets", type="ticket", text=text)
                for text in texts
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    return credential.workspace, [record.id for record in inserted.inserted]


async def _stored(db_pool: DatabasePool, record_id: UUID) -> dict[str, Any]:
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, scores, enrichment_error from record where id = %s",
                (record_id,),
            )
        ).fetchone()
    assert row is not None
    return dict(row)


async def _run(db_pool: DatabasePool, processor: str) -> dict[str, Any]:
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content from record
                where collection = '_system' and type = 'run' and content->>'processor' = %s
                """,
                (processor,),
            )
        ).fetchone()
    assert row is not None
    return row["content"]


async def _escalate_jobs(db_pool: DatabasePool, workspace: str) -> int:
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select count(*) as jobs from job where workspace = %s and derivation = 'escalate'",
                (workspace,),
            )
        ).fetchone()
    assert row is not None
    return int(row["jobs"])


async def test_urgency_maps_the_score_onto_the_scale_and_keeps_the_raw_answer(
    db_pool: DatabasePool,
    inbox_settings: Settings,
    catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (record_id,) = await _tickets(
        db_pool, inbox_settings, catalog, "You charged our card twice for September."
    )
    fake.reset()
    script_inbox(monkeypatch)

    assert (await enrich_once(db_pool, inbox_settings, catalog)).ready == 1

    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["urgency"] == {
        "value": pytest.approx(8.8),
        "judgment": {"urgency": score(2.6, 4)},
    }
    assert stored["scores"]["urgency"] == pytest.approx(8.8)
    urgency_call = next(call for call in fake.judgment_calls if "urgency" in call.questions)
    assert urgency_call.model == "jev-1.13.0"
    assert urgency_call.state["record"]["text"] == "You charged our card twice for September."
    run = await _run(db_pool, "urgency")
    assert [(call["outcome"], call["resolved"]) for call in run["model_calls"]] == [
        ("ok", "fake:jev-1.13.0")
    ]


async def test_urgency_falls_back_to_the_default_when_attempts_run_out(
    db_pool: DatabasePool,
    inbox_settings: Settings,
    catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (record_id,) = await _tickets(db_pool, inbox_settings, catalog, "Lunch was fine.")
    fake.reset()
    judge = fake.judge

    async def out_of_range(endpoint: Any, model: str, request: Any) -> Any:
        if set(request.questions) == {"urgency"}:
            fake.enqueue_judgment({"urgency": score(7, 4)})
        return await judge(endpoint, model, request)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(fake, "judge", out_of_range)
    monkeypatch.setattr("memseek.llm.runtime.asyncio.sleep", no_sleep)

    await enrich_once(db_pool, inbox_settings, catalog)

    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["urgency"] == {"value": 5.0}
    assert stored["scores"]["urgency"] == 5.0
    assert "judgment:" in stored["enrichment_error"]
    run = await _run(db_pool, "urgency")
    assert run["warnings"] == ["judgment_default"]
    assert [(call["outcome"], call["error_kind"]) for call in run["model_calls"]] == [
        ("error", "response"),
        ("error", "response"),
    ]


async def test_signals_stores_the_answer_map_and_promotes_score_fields(
    db_pool: DatabasePool,
    inbox_settings: Settings,
    catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (record_id,) = await _tickets(
        db_pool, inbox_settings, catalog, "Every call has returned 503 since 9am."
    )
    fake.reset()
    script_inbox(monkeypatch)

    await enrich_once(db_pool, inbox_settings, catalog)

    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["signals"] == {
        "needs_reply": noul(0.99),
        "topic": choice("technical", 0.95, TOPICS),
        "frustration": score(1.2, 3),
    }
    assert {
        name: stored["scores"][name]
        for name in ("needs_reply_p", "frustration", "topic_confidence")
    } == {"needs_reply_p": 0.99, "frustration": 1.2, "topic_confidence": 0.95}


@pytest.mark.parametrize(
    ("texts", "jobs"),
    [
        # Two tickets, frustration 1.9 + 1.2: the sum crosses 3 on fewer than 3 records.
        (("You charged our card twice.", "Every call returned 503."), 1),
        # Three calm tickets, 0.1 + 0.3 + 0.8: three records, but the sum stays under 3.
        (("How do I export last quarter?", "SSO with Okta?", "Something looks off."), 0),
    ],
)
async def test_escalate_fires_on_the_sum_of_signals_frustration_score(
    db_pool: DatabasePool,
    inbox_settings: Settings,
    catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
    texts: tuple[str, ...],
    jobs: int,
) -> None:
    workspace, _ = await _tickets(db_pool, inbox_settings, catalog, *texts)
    fake.reset()
    script_inbox(monkeypatch)

    assert (await enrich_once(db_pool, inbox_settings, catalog)).ready == len(texts)

    assert await _escalate_jobs(db_pool, workspace) == jobs


def _processor_error(tmp_path: Path, old: str, new: str) -> DefinitionError:
    root = tmp_path / "catalog"
    shutil.copytree(CATALOG, root)
    path = root / "conf" / "processors.yaml"
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))
    return caught.value


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "    score_fields:\n",
            "    output_schema: {type: object}\n    score_fields:\n",
            "judgment json processor forbids output_schema; questions define it",
        ),
        (
            "      urgency:\n        type: score\n",
            '      calm:\n        type: noul\n        instructions: "Is `record.text` calm?"\n'
            "      urgency:\n        type: score\n",
            "judgment score processor requires exactly one noul or score question",
        ),
        (
            "needs_reply_p: needs_reply.noul",
            "needs_reply_p: topic.choice",
            "score path 'topic.choice' must resolve to a numeric schema leaf",
        ),
        (
            "    model: jev\n    input: {collections: [tickets]}\n    questions:",
            "    model: strong\n    input: {collections: [tickets]}\n    questions:",
            "model alias 'strong' cannot be used for judgment",
        ),
        (
            "    default_output:\n"
            "      needs_reply: {noul: 0}\n"
            "      topic: {choice: other, probabilities: {}, confidence: 0}\n"
            "      frustration: {score: 0, probabilities: [1, 0, 0], confidence: 0}\n",
            "",
            "required processor 'signals' has no terminal default",
        ),
    ],
)
def test_invalid_judgment_processors_fail_to_load(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    assert message in _processor_error(tmp_path, old, new).message


def test_shipped_commented_judgment_processors_compile_once_enabled(tmp_path: Path) -> None:
    root = materialize_reference_catalog(tmp_path / "catalog")
    path = root / "conf" / "processors.yaml"
    path.write_text(re.sub(r"(?m)^  # (?=- |  )", "  ", path.read_text(encoding="utf-8")))
    declare_test_sources(root, "conf/processors.yaml")

    catalog = load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))

    assert {name: catalog.processors[name].kind for name in ("importance_jev", "intent_v1")} == {
        "importance_jev": "score",
        "intent_v1": "json",
    }
    assert set(catalog.processors["intent_v1"].score_fields) == {
        "actionable_p",
        "urgency",
        "topic_confidence",
    }
