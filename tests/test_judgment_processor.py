from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from reference_catalog import declare_test_sources, materialize_reference_catalog

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog, DefinitionError, load_definition_catalog
from memseek.enrichment import enrich_once
from memseek.llm.fake import fake

IMPORTANCE_JEV = """
  - name: importance
    kind: score
    source: judgment
    model: jev
    input: {collections: [main, outcomes, calendar_events]}
    scale: [1, 10]
    default: 5
    render: true
    questions:
      importance:
        type: score
        instructions: "How significant is `record.text` for future decisions?"
        criteria: ["Routine", "Mildly useful", "Worth remembering", "Changes decisions"]
"""

INTENT_V1 = """
  - name: intent_v1
    kind: json
    source: judgment
    model: jev
    input: {collections: [main], types: [chat, message]}
    questions:
      actionable:
        type: noul
        instructions: "Does `record.text` ask someone to do something?"
        criteria:
          true: "An explicit request, task, or commitment"
          false: "Information or chatter only"
      topic:
        type: choice
        instructions: "What is `record.text` mainly about?"
        criteria: {bug: "Something is broken", feature: "A new capability", other: null}
    score_fields:
      actionable_p: actionable.noul
      topic_bug: topic.probabilities.bug
"""


def _catalog_root(tmp_path: Path, processors: str) -> Path:
    root = materialize_reference_catalog(tmp_path / "catalog")
    path = root / "conf" / "processors.yaml"
    source = path.read_text(encoding="utf-8")
    start = source.index("  - name: importance")
    end = source.index("  - name: sentiment_v1")
    path.write_text(source[:start] + processors.lstrip("\n") + "\n" + source[end:])
    core = root / "collections" / "core.yaml"
    core.write_text(
        core.read_text().replace(
            "required_processors: [embedding_v1, importance]\n    search_profile",
            "required_processors: [embedding_v1, importance]\n"
            "    optional_processors: [intent_v1]\n    search_profile",
            1,
        )
    )
    declare_test_sources(root, "conf/processors.yaml")
    return root


@pytest.fixture
def judged(tmp_path: Path, settings: Settings) -> tuple[Settings, DefinitionCatalog]:
    root = _catalog_root(tmp_path, IMPORTANCE_JEV + INTENT_V1)
    judged_settings = settings.model_copy(update={"catalog_file": root / "catalog.yaml"})
    return judged_settings, load_definition_catalog(judged_settings)


async def _record(db_pool: Any, catalog: DefinitionCatalog, record_type: str, text: str) -> UUID:
    main = catalog.resolve_collection("main")
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('judged', %s) on conflict do nothing",
            ("a" * 64,),
        )
        row = await (
            await conn.execute(
                """
                insert into record (workspace, collection, collection_version, collection_hash,
                                    entity, type, content)
                values ('judged', 'main', %s, %s, 'entity-1', %s, %s)
                returning id
                """,
                (main.version, main.contract_hash, record_type, Jsonb({"text": text})),
            )
        ).fetchone()
    assert row is not None
    return row["id"]


async def _stored(db_pool: Any, record_id: UUID) -> dict[str, Any]:
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select annotations, scores, enrichment_error from record where id = %s",
                (record_id,),
            )
        ).fetchone()
    assert row is not None
    return dict(row)


async def _run(db_pool: Any, processor: str) -> dict[str, Any]:
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


async def test_score_judgment_maps_onto_the_scale_and_keeps_the_raw_answer(
    db_pool: Any, judged: tuple[Settings, DefinitionCatalog]
) -> None:
    settings, catalog = judged
    record_id = await _record(db_pool, catalog, "event", "We are moving to Lisbon in May.")
    fake.reset()
    fake.enqueue_judgment(
        {"importance": {"score": 2.4, "probabilities": [0.0, 0.1, 0.4, 0.5], "confidence": 0.8}}
    )

    result = await enrich_once(db_pool, settings, catalog)

    assert result.ready == 1
    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["importance"] == {
        "value": pytest.approx(8.2),
        "judgment": {
            "importance": {
                "score": 2.4,
                "probabilities": [0.0, 0.1, 0.4, 0.5],
                "confidence": 0.8,
            }
        },
    }
    assert stored["scores"]["importance"] == pytest.approx(8.2)
    assert fake.judgment_calls[0].model == "jev-1.13.0"
    assert fake.judgment_calls[0].state["record"]["text"] == "We are moving to Lisbon in May."
    assert fake.judgment_calls[0].state["record"]["content"] == {}
    run = await _run(db_pool, "importance")
    assert [(call["outcome"], call["resolved"]) for call in run["model_calls"]] == [
        ("ok", "fake:jev-1.13.0")
    ]


async def test_score_judgment_falls_back_to_the_default_when_attempts_run_out(
    db_pool: Any, judged: tuple[Settings, DefinitionCatalog], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, catalog = judged
    record_id = await _record(db_pool, catalog, "event", "Lunch was fine.")
    fake.reset()
    out_of_range = {"importance": {"score": 7, "probabilities": [0.25] * 4, "confidence": 0.5}}
    fake.enqueue_judgment(out_of_range, out_of_range)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("memseek.llm.runtime.asyncio.sleep", no_sleep)

    await enrich_once(db_pool, settings, catalog)

    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["importance"] == {"value": 5.0}
    assert stored["scores"]["importance"] == 5.0
    assert "judgment:" in stored["enrichment_error"]
    run = await _run(db_pool, "importance")
    assert run["warnings"] == ["judgment_default"]
    assert [(call["outcome"], call["error_kind"]) for call in run["model_calls"]] == [
        ("error", "response"),
        ("error", "response"),
    ]


async def test_json_judgment_stores_the_answer_map_and_promotes_score_fields(
    db_pool: Any, judged: tuple[Settings, DefinitionCatalog]
) -> None:
    settings, catalog = judged
    record_id = await _record(db_pool, catalog, "chat", "The export button crashes, please fix")
    fake.reset()
    await enrich_once(db_pool, settings, catalog)
    fake.enqueue_judgment(
        {
            "actionable": {"type": "noul", "noul": 0.91},
            "topic": {
                "type": "choice",
                "choice": "bug",
                "probabilities": {"bug": 0.8, "feature": 0.15, "other": 0.05},
                "confidence": 0.73,
            },
        }
    )

    result = await enrich_once(db_pool, settings, catalog)

    assert result.kind == "optional"
    stored = await _stored(db_pool, record_id)
    assert stored["annotations"]["intent_v1"] == {
        "actionable": {"noul": 0.91},
        "topic": {
            "choice": "bug",
            "probabilities": {"bug": 0.8, "feature": 0.15, "other": 0.05},
            "confidence": 0.73,
        },
    }
    assert (stored["scores"]["actionable_p"], stored["scores"]["topic_bug"]) == (0.91, 0.8)


@pytest.mark.parametrize(
    ("processors", "message"),
    [
        (
            IMPORTANCE_JEV
            + INTENT_V1.replace(
                "    score_fields:", "    output_schema: {type: object}\n    score_fields:"
            ),
            "judgment json processor forbids output_schema; questions define it",
        ),
        (
            IMPORTANCE_JEV.replace("type: score", "type: choice").replace(
                'criteria: ["Routine", "Mildly useful", "Worth remembering", "Changes decisions"]',
                "criteria: {low: null, high: null}",
            )
            + INTENT_V1,
            "judgment score processor requires exactly one noul or score question",
        ),
        (
            IMPORTANCE_JEV
            + INTENT_V1.replace("actionable_p: actionable.noul", "actionable_p: topic.choice"),
            "score path 'topic.choice' must resolve to a numeric schema leaf",
        ),
        (
            IMPORTANCE_JEV + INTENT_V1.replace("model: jev", "model: cheap"),
            "model alias 'cheap' cannot be used for judgment",
        ),
    ],
)
def test_invalid_judgment_processors_fail_to_load(
    tmp_path: Path, processors: str, message: str
) -> None:
    root = _catalog_root(tmp_path, processors)

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))

    assert message in caught.value.message


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
