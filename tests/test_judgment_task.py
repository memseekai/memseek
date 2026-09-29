from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, DefinitionError, load_definition_catalog
from memseek.derive.basis import EvaluationBasis
from memseek.derive.candidates import compile_candidate_set
from memseek.derive.errors import DerivationError
from memseek.derive.provenance import ProvenanceValue
from memseek.derive.runner import (
    _execute_tasks,
    _Execution,
    enqueue_derivation_job,
    process_derivation_job,
)
from memseek.derive.schema import PipelineDefinition
from memseek.enrichment import enrich_once
from memseek.jobs import claim_job
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

CATALOG = Path(__file__).resolve().parents[1] / "examples" / "judgment_catalog"


@pytest.fixture
def judgment_settings(bare_settings: Settings) -> Settings:
    return bare_settings.model_copy(update={"catalog_file": CATALOG / "catalog.yaml"})


@pytest.fixture
def catalog(judgment_settings: Settings) -> DefinitionCatalog:
    return load_definition_catalog(judgment_settings)


def _execution(definition: PipelineDefinition, settings: Settings) -> _Execution:
    now = datetime.now(UTC)
    return _Execution(
        workspace="judge-test",
        build_sha=settings.memseek_build_sha,
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
        predecessor_hash=None,
        visible_ids=set(),
        final_source_ids=frozenset(),
        final_visible_ids=frozenset(),
        high_seq=0,
        started_at=now,
        completed_at=now,
        model_calls=[],
        task_trace=[],
    )


def _records(source: str, texts: list[str], **content: str) -> tuple[dict[str, Any], list[UUID]]:
    ids = [uuid4() for _ in texts]
    rows = [
        {"id": str(record_id), "key": None, "content": {"text": text, **content}}
        for record_id, text in zip(ids, texts, strict=True)
    ]
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        source: ProvenanceValue({"records": rows}, frozenset(ids)),
    }
    return variables, ids


def _noul(value: float) -> dict[str, Any]:
    return {"actionable": {"type": "noul", "noul": value}}


def _team(choice: str, confidence: float) -> dict[str, Any]:
    probabilities = {"billing": 0.1, "technical": 0.1, "sales": 0.1}
    probabilities[choice] = 0.8
    return {
        "team": {
            "type": "choice",
            "choice": choice,
            "probabilities": probabilities,
            "confidence": confidence,
        }
    }


def test_example_catalog_compiles_judgment_processors_and_tasks(
    catalog: DefinitionCatalog,
) -> None:
    action_items = catalog.derivations["action_items"]
    assert [task.use for task in action_items.tasks] == ["judge", "llm"]
    assert action_items.trigger is not None
    assert action_items.trigger.accumulator is not None
    metric = action_items.trigger.accumulator.metric
    assert not isinstance(metric, str)
    assert (metric.annotation, metric.path) == ("intent_v1", "actionable.noul")
    assert catalog.derivations["ticket_triage"].limits.max_judgments == 200
    assert catalog.processors["intent_v1"].source == "judgment"
    assert catalog.processors["importance"].source == "judgment"


async def test_judge_filters_in_front_of_an_llm(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["action_items"]
    execution = _execution(definition, judgment_settings)
    variables, (fix_it, chatter, send_it) = _records(
        "new_messages", ["Please fix the export by Friday", "lol nice", "Send me the deck"]
    )
    fake.reset()
    fake.enqueue_judgment(_noul(0.9), _noul(0.2), _noul(0.7))
    fake.enqueue(
        Completion(
            '{"records":[{"text":"Fix the export","citations":["'
            + str(fix_it)
            + '"],"content":{"kind":"action_item","due":"Friday"}}]}'
        )
    )

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations={"new_messages": frozenset({fix_it, chatter, send_it})},
    )

    assert [call.state["item"]["content"]["text"] for call in fake.judgment_calls] == [
        "Please fix the export by Friday",
        "lol nice",
        "Send me the deck",
    ]
    prompt = fake.completion_calls[0].prompt
    assert (str(fix_it) in prompt, str(chatter) in prompt, str(send_it) in prompt) == (
        True,
        False,
        True,
    )
    assert execution.task_trace is not None
    assert execution.task_trace[0]["citation_ids"] == sorted([str(fix_it), str(send_it)])
    assert execution.output == [
        {
            "text": "Fix the export",
            "citations": [str(fix_it)],
            "content": {"kind": "action_item", "due": "Friday"},
        }
    ]
    assert (execution.judgments, execution.logical_llm_calls) == (3, 1)
    assert [call["resolved"] for call in execution.model_calls[:3]] == ["fake:jev-1.13.0"] * 3


async def test_judge_records_cite_only_kept_items_and_pass_emit(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["ticket_triage"]
    execution = _execution(definition, judgment_settings)
    variables, (refund, unclear) = _records(
        "tickets", ["Refund request", "Something about my account"]
    )
    fake.reset()
    fake.enqueue_judgment(_team("billing", 0.9), _team("sales", 0.4))

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations={"tickets": frozenset({refund, unclear})},
    )

    billing = _team("billing", 0.9)["team"]
    del billing["type"]
    assert execution.output == [
        {
            "text": "Refund request",
            "content": {"kind": "ticket_label", "judgment": {"team": billing}},
            "citations": [str(refund)],
        }
    ]
    assert execution.final_visible_ids == frozenset({refund})
    candidate = compile_candidate_set(
        execution.output,
        emit=definition.emit,
        basis=execution.basis,
        visible=execution.final_visible_ids,
        settings=judgment_settings,
        catalog=catalog,
    )
    assert [(record.content["kind"], record.citations) for record in candidate.records] == [
        ("ticket_label", (refund,))
    ]
    assert execution.logical_llm_calls == 0


async def test_judge_output_lists_every_item_with_its_answers(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["ticket_triage"]
    probe = definition.model_copy(
        update={
            "tasks": definition.tasks,
            "emit": definition.emit.model_copy(update={"from_": "{{labels.items}}"}),
        }
    )
    execution = _execution(probe, judgment_settings)
    variables, (ticket,) = _records("tickets", ["Upgrade pricing?"])
    fake.reset()
    fake.enqueue_judgment(_team("sales", 0.3))

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations={"tickets": frozenset({ticket})},
    )

    sales = _team("sales", 0.3)["team"]
    del sales["type"]
    assert execution.output == [{"id": str(ticket), "answers": {"team": sales}, "kept": False}]


async def test_judge_refuses_to_exceed_max_judgments(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["ticket_triage"]
    tight = definition.model_copy(
        update={"limits": definition.limits.model_copy(update={"max_judgments": 1})}
    )
    execution = _execution(tight, judgment_settings)
    variables, ids = _records("tickets", ["one", "two"])
    fake.reset()

    with pytest.raises(DerivationError, match="run exceeds max_judgments") as caught:
        await _execute_tasks(
            execution,
            db_pool,
            judgment_settings,
            catalog,
            variables=variables,
            citations={"tickets": frozenset(ids)},
        )

    assert caught.value.kind == "budget"
    assert fake.judgment_calls == []


def _catalog_error(tmp_path: Path, old: str, new: str) -> DefinitionError:
    root = tmp_path / "catalog"
    shutil.copytree(CATALOG, root)
    path = root / "derivations" / "ticket_triage.yaml"
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))
    return caught.value


@pytest.mark.parametrize(
    ("old", "new", "code", "message"),
    [
        (
            "      model: jev\n",
            "",
            "model_kind",
            "judge Task requires with.model or a judgment pipeline model",
        ),
        (
            "      model: jev\n",
            "      model: strong\n",
            "model_kind",
            "model alias 'strong' cannot be used for judgment",
        ),
        (
            "limits: {max_llm_calls: 0, max_judgments: 200}",
            "limits: {max_llm_calls: 0}",
            "budget",
            "judge Tasks need limits.max_judgments; opt in explicitly",
        ),
        (
            'input: "{{tickets.records}}"',
            'input: "tickets: {{tickets.records}}"',
            "task_input",
            "judge Task input must be one exact list reference, such as {{source.records}}",
        ),
        (
            "keep: {team: {confidence_gte: 0.6}}",
            "keep: {team: {noul_gte: 0.6}}",
            "task_config",
            "keep.team.noul_gte does not apply to a choice question",
        ),
        (
            'instructions: "Which team should handle `item.content.text`?"',
            'instructions: "Which team should handle {{entity}}?"',
            "template_reference",
            "tasks[0].with references unavailable variable(s): entity",
        ),
    ],
)
def test_invalid_judge_tasks_fail_to_load(
    tmp_path: Path, old: str, new: str, code: str, message: str
) -> None:
    error = _catalog_error(tmp_path, old, new)

    assert error.code == code
    assert message in error.message


async def test_ticket_triage_labels_confident_tickets_end_to_end(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "judge-e2e")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=tuple(
                PublicRecordInput(
                    entity="acme",
                    collection="support",
                    type="ticket",
                    text=text,
                )
                for text in ("Please refund invoice 42", "Hmm")
            )
        ),
        catalog=catalog,
        settings=judgment_settings,
    )
    refund_id, vague_id = (record.id for record in inserted.inserted)
    fake.reset()
    assert (await enrich_once(db_pool, judgment_settings, catalog)).ready == 2
    await enqueue_derivation_job(
        db_pool, workspace=credential.workspace, derivation="ticket_triage", entity="acme"
    )
    claimed = await claim_job(
        db_pool,
        worker_id="judge-e2e",
        kinds=("derive",),
        derivations=("ticket_triage",),
        lease_s=judgment_settings.job_lease_s,
        max_attempts=judgment_settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue_judgment(_team("billing", 0.9), _team("sales", 0.3))

    result = await process_derivation_job(
        db_pool, claimed=claimed, settings=judgment_settings, catalog=catalog
    )

    assert (result.disposition, result.output_count) == ("done", 1)
    async with db_pool.connection() as conn:
        labels = await (
            await conn.execute(
                """
                select type, content, derived_from from record
                where workspace = %s and collection = 'ticket_labels'
                """,
                (credential.workspace,),
            )
        ).fetchall()
        run = await (
            await conn.execute(
                """
                select content from record
                where workspace = %s and collection = '_system' and type = 'run'
                  and content->>'operation' = 'derive'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    billing = _team("billing", 0.9)["team"]
    del billing["type"]
    assert [(row["type"], row["content"]) for row in labels] == [
        (
            "label",
            {
                "text": "Please refund invoice 42",
                "kind": "ticket_label",
                "judgment": {"team": billing},
            },
        )
    ]
    assert refund_id in labels[0]["derived_from"]
    assert vague_id not in labels[0]["derived_from"]
    assert run is not None
    assert run["content"]["usage"]["judgments"] == 2
    assert [call["resolved"] for call in run["content"]["model_calls"]] == [
        "fake:jev-1.13.0",
        "fake:jev-1.13.0",
    ]
