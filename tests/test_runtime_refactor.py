"""Exact output and operation-count regressions for runtime hot paths."""

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest
from starlette.responses import JSONResponse

from memseek import enrichment
from memseek.api import _bounded_json
from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.derive import runner
from memseek.derive.basis import DerivationRecord
from memseek.render import estimate_tokens, render_rows
from memseek.search import engine
from memseek.search.spec import SearchSpec


def _row(number: int, text: str) -> DerivationRecord:
    return DerivationRecord(
        id=UUID(int=number),
        seq=number,
        collection="main",
        collection_version=1,
        entity="entity",
        key=None,
        type="event",
        status="active",
        content={"text": text},
        scores={},
        annotations={},
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        depth=0,
    )


def test_pipeline_packing_matches_complete_prefixes_at_every_budget(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    rows = [_row(index + 1, text) for index, text in enumerate(("", "é漢🙂<&>", "x" * 200))]
    rendered = [
        runner.render_record(runner._renderable(row), profile="derivation_input", catalog=catalog)
        for row in rows
    ]
    for max_records in (0, 1, 3, None):
        bounded_rows = rows if max_records is None else rows[:max_records]
        for budget in range(estimate_tokens("\n".join(rendered)) + 2):
            count = 0
            while (
                count < len(bounded_rows)
                and estimate_tokens("\n".join(rendered[: count + 1])) <= budget
            ):
                count += 1
            selected, text = runner._pack_rows(
                rows,
                catalog=catalog,
                max_tokens=budget,
                max_records=max_records,
            )
            assert selected == tuple(rows[:count])
            assert text == "\n".join(rendered[:count])
    with patch.object(runner, "render_record", wraps=runner.render_record) as render:
        runner._pack_rows(rows, catalog=catalog, max_tokens=10000)
        assert render.call_count == len(rows)
    assert runner._pack_rows([], catalog=catalog, max_tokens=0) == ((), "")


def test_enrichment_batches_match_full_prompt_budgets() -> None:
    records = [
        enrichment._Record(
            id=UUID(int=n),
            seq=n,
            workspace="test",
            collection="main",
            collection_version=1,
            collection_hash="a" * 64,
            entity="entity",
            key=None,
            type="event",
            status="active",
            content={"text": text},
            scores={},
            annotations={},
            annotation_meta={},
            enrichment_meta={},
            run_id=None,
            depth=0,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for n, text in enumerate(("é漢🙂<&>", "x" * 300, "", "small"), 1)
    ]
    rendered = {row.id: enrichment._render_record(row, row.text) for row in records}
    prefix = "classify é\n"

    def tokens(rows: list[enrichment._Record]) -> int:
        return estimate_tokens(
            prefix
            + render_rows(
                [rendered[row.id] for row in rows],
                fence=enrichment._ENRICHMENT_FENCE,
            )
        )

    for max_rows in (1, 2, 4):
        for budget in range(tokens(records) + 2):
            expected: list[list[enrichment._Record]] = []
            unpackable: list[enrichment._Record] = []
            current: list[enrichment._Record] = []
            for row in records:
                candidate = [*current, row]
                if len(candidate) <= max_rows and tokens(candidate) <= budget:
                    current = candidate
                else:
                    if current:
                        expected.append(current)
                    current = [row] if tokens([row]) <= budget else []
                    if not current:
                        unpackable.append(row)
            if current:
                expected.append(current)
            assert enrichment._batch_rows(
                records, rendered, prefix=prefix, max_rows=max_rows, max_tokens=budget
            ) == (expected, unpackable)


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_structured_sort_converts_each_value_once(settings: Settings, direction: str) -> None:
    catalog = load_definition_catalog(settings)
    source = engine.resolve_search(
        SearchSpec.model_validate(
            {
                "mode": "structured",
                "scope": {"collections": ["calendar_events"]},
                "order_by": [{"field": "starts_at", "direction": direction}],
            }
        ),
        catalog=catalog,
        settings=settings,
    ).sources[0]
    dates = ["2026-01-02T00:00:00Z", None, "2026-01-01T19:00:00-05:00", "2026-01-01T00:00:00Z"]
    rows: list[dict[str, Any]] = [
        {
            "id": UUID(int=n),
            "seq": n,
            "collection": "calendar_events",
            "collection_version": 1,
            "content": {"starts_at": date},
            "annotations": {},
        }
        for n, date in enumerate(dates, 1)
    ]
    with patch.object(engine, "_typed_value", wraps=engine._typed_value) as convert:
        result = engine._structured_sort(source, rows)
    assert convert.call_count == len(rows)
    assert [row["seq"] for row in result] == ([4, 1, 3, 2] if direction == "asc" else [1, 3, 4, 2])


def test_bounded_response_serializes_once_and_uses_exact_utf8_size(settings: Settings) -> None:
    content = {"text": 'é漢🙂"\\\n' * 500}
    size = len(JSONResponse(content).body)
    with patch.object(
        JSONResponse, "render", autospec=True, side_effect=JSONResponse.render
    ) as render:
        response = _bounded_json(content, settings.model_copy(update={"max_response_bytes": size}))
        assert response.status_code == 200
        assert len(response.body) == size
        assert render.call_count == 1
    rejected = _bounded_json(content, settings.model_copy(update={"max_response_bytes": size - 1}))
    assert rejected.status_code == 409


async def test_retrieved_hits_are_the_longest_prefix_whose_render_fits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from memseek.derive.basis import EvaluationBasis
    from memseek.derive.schema import PipelineDefinition
    from memseek.derive.tasks import SearchTaskConfig

    hits = [
        {
            "id": str(UUID(int=number)),
            "collection": "main",
            "type": "event",
            "occurred_at": "2026-01-01T00:00:00Z",
            "text": text,
        }
        for number, text in enumerate(("", "é漢🙂<&>", "x" * 200, "short"), start=1)
    ]

    async def search(*_: Any, **__: Any) -> dict[str, Any]:
        return {"hits": hits}

    monkeypatch.setattr(runner, "execute_search", search)
    definition = PipelineDefinition.model_validate(
        {
            "name": "packing",
            "sources": {"evidence": {"kind": "changes", "collections": ["main"]}},
            "tasks": [
                {
                    "id": "found",
                    "use": "search",
                    "with": {"q": "x", "spec": {"q": "x"}},
                    "input": "{{evidence.records}}",
                }
            ],
            "emit": {"from": "{{found}}", "collection": "main", "type": "event"},
        }
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    base = runner._Execution(
        workspace="packing",
        build_sha="test",
        definition=definition,
        config_hash=definition.definition_hash,
        basis=EvaluationBasis(
            mode="changes",
            from_seq=0,
            through_seq=0,
            predecessor_run_id=None,
            predecessor_source_hash=None,
            input_rows=(),
            read_rows={},
            expected_heads=(),
        ),
        wm_before=0,
        predecessor=None,
        visible_ids=set(),
        final_source_ids=frozenset(),
        final_visible_ids=frozenset(),
        high_seq=0,
        started_at=now,
        completed_at=now,
        model_calls=[],
    )
    catalog = load_definition_catalog(settings)
    rendered = [runner._hit_row(hit) for hit in hits]
    for budget in range(1, estimate_tokens("\n".join(rendered)) + 2):
        expected = 0
        while expected < len(hits) and estimate_tokens("\n".join(rendered[: expected + 1])) <= (
            budget
        ):
            expected += 1
        execution = replace(base, visible_ids=set())
        value = await runner._retrieve(
            execution,
            cast(Any, None),
            settings,
            catalog,
            step=SearchTaskConfig.model_validate(
                {"q": "x", "spec": {"mode": "text"}, "max_tokens": budget}
            ),
            variables={},
        )
        (result,) = value.value
        assert [hit["id"] for hit in result["hits"]] == [hit["id"] for hit in hits[:expected]]
        assert result["rendered"].value == "\n".join(rendered[:expected])
    with patch.object(runner, "escape_untrusted", wraps=runner.escape_untrusted) as escape:
        await runner._retrieve(
            replace(base, visible_ids=set()),
            cast(Any, None),
            settings,
            catalog,
            step=SearchTaskConfig.model_validate(
                {"q": "x", "spec": {"mode": "text"}, "max_tokens": 10_000}
            ),
            variables={},
        )
    assert escape.call_count == len(hits)
