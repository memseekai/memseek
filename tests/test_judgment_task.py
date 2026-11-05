from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from support_inbox import CATALOG, TEAMS, choice, noul, script_inbox

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


ACCOUNT_ID = uuid4()
PROFILE: dict[str, Any] = {
    "id": str(ACCOUNT_ID),
    "key": "profile",
    "content": {"text": "Northwind, basic plan", "plan": "basic", "notes": "No SSO"},
}


def _tickets(texts: list[str]) -> tuple[dict[str, Any], list[UUID]]:
    ids = [uuid4() for _ in texts]
    rows = [
        {"id": str(record_id), "key": None, "content": {"text": text}}
        for record_id, text in zip(ids, texts, strict=True)
    ]
    variables = {
        "entity": "customer:northwind",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "new_tickets": ProvenanceValue({"records": rows}, frozenset(ids)),
        "account": ProvenanceValue({"records": [PROFILE]}, frozenset({ACCOUNT_ID})),
    }
    return variables, ids


def _citations(ids: list[UUID]) -> dict[str, frozenset[UUID]]:
    return {"new_tickets": frozenset(ids), "account": frozenset({ACCOUNT_ID})}


def _route(team: str, confidence: float, covered: float = 0.5) -> dict[str, Any]:
    return {"team": choice(team, confidence, TEAMS), "covered": noul(covered)}


def test_example_catalog_compiles_judgment_processors_and_tasks(
    catalog: DefinitionCatalog,
) -> None:
    escalate = catalog.derivations["escalate"]
    assert [task.use for task in escalate.tasks] == ["judge", "llm"]
    assert escalate.trigger is not None
    assert escalate.trigger.accumulator is not None
    metric = escalate.trigger.accumulator.metric
    assert not isinstance(metric, str)
    assert (metric.annotation, metric.path, metric.aggregate) == (
        "signals",
        "frustration.score",
        "sum",
    )
    route = catalog.derivations["route_tickets"]
    assert route.trigger is None
    assert (route.limits.max_llm_calls, route.limits.max_judgments) == (0, 50)
    assert {name: catalog.processors[name].source for name in ("urgency", "signals")} == {
        "urgency": "judgment",
        "signals": "judgment",
    }


async def test_judge_filters_in_front_of_an_llm(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    execution = _execution(catalog.derivations["escalate"], judgment_settings)
    variables, (outage, thanks, refund) = _tickets(
        ["The API is down", "Thanks, all good now", "Refund the double charge"]
    )
    fake.reset()
    fake.enqueue_judgment(
        {"needs_escalation": noul(0.95)},
        {"needs_escalation": noul(0.1)},
        {"needs_escalation": noul(0.7)},
    )
    fake.enqueue(
        Completion(
            '{"records":[{"text":"Outage: page on-call","citations":["'
            + str(outage)
            + '"],"content":{"kind":"escalation","severity":"critical"}}]}'
        )
    )

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations=_citations([outage, thanks, refund]),
    )

    assert [call.state["item"]["content"]["text"] for call in fake.judgment_calls] == [
        "The API is down",
        "Thanks, all good now",
        "Refund the double charge",
    ]
    prompt = fake.completion_calls[0].prompt
    assert (str(outage) in prompt, str(thanks) in prompt, str(refund) in prompt) == (
        True,
        False,
        True,
    )
    assert execution.task_trace is not None
    assert execution.task_trace[0]["citation_ids"] == sorted([str(outage), str(refund)])
    assert execution.output == [
        {
            "text": "Outage: page on-call",
            "citations": [str(outage)],
            "content": {"kind": "escalation", "severity": "critical"},
        }
    ]
    assert (execution.judgments, execution.logical_llm_calls) == (3, 1)
    assert [call["resolved"] for call in execution.model_calls[:3]] == ["fake:jev-1.13.0"] * 3


async def test_route_records_cite_only_kept_items_and_pass_emit(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["route_tickets"]
    execution = _execution(definition, judgment_settings)
    variables, (refund, vague) = _tickets(["Refund invoice 42", "Something is off"])
    fake.reset()
    fake.enqueue_judgment(_route("billing", 0.9, covered=0.8), _route("sales", 0.4))

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations=_citations([refund, vague]),
    )

    assert [call.state["context"] for call in fake.judgment_calls] == [
        {"account": [PROFILE]},
        {"account": [PROFILE]},
    ]
    assert execution.output == [
        {
            "text": "Refund invoice 42",
            "content": {"kind": "route", "judgment": _route("billing", 0.9, covered=0.8)},
            "citations": [str(refund)],
        }
    ]
    assert vague not in execution.final_visible_ids
    candidate = compile_candidate_set(
        execution.output,
        emit=definition.emit,
        basis=execution.basis,
        visible=execution.final_visible_ids,
        settings=judgment_settings,
        catalog=catalog,
    )
    assert [(record.content["kind"], record.citations) for record in candidate.records] == [
        ("route", (refund,))
    ]
    assert execution.logical_llm_calls == 0


async def test_judge_output_lists_every_item_with_its_answers(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["route_tickets"]
    probe = definition.model_copy(
        update={"emit": definition.emit.model_copy(update={"from_": "{{route.items}}"})}
    )
    execution = _execution(probe, judgment_settings)
    variables, (ticket,) = _tickets(["Upgrade pricing?"])
    fake.reset()
    fake.enqueue_judgment(_route("sales", 0.3))

    await _execute_tasks(
        execution,
        db_pool,
        judgment_settings,
        catalog,
        variables=variables,
        citations=_citations([ticket]),
    )

    assert execution.output == [{"id": str(ticket), "answers": _route("sales", 0.3), "kept": False}]


async def test_judge_refuses_to_exceed_max_judgments(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["route_tickets"]
    tight = definition.model_copy(
        update={"limits": definition.limits.model_copy(update={"max_judgments": 1})}
    )
    execution = _execution(tight, judgment_settings)
    variables, ids = _tickets(["one", "two"])
    fake.reset()

    with pytest.raises(DerivationError, match="run exceeds max_judgments") as caught:
        await _execute_tasks(
            execution,
            db_pool,
            judgment_settings,
            catalog,
            variables=variables,
            citations=_citations(ids),
        )

    assert caught.value.kind == "budget"
    assert fake.judgment_calls == []


async def test_judge_refuses_to_exceed_max_judgment_tokens(
    judgment_settings: Settings, catalog: DefinitionCatalog, db_pool: DatabasePool
) -> None:
    definition = catalog.derivations["route_tickets"]
    tight = definition.model_copy(
        update={"limits": definition.limits.model_copy(update={"max_judgment_tokens": 50})}
    )
    execution = _execution(tight, judgment_settings)
    variables, ids = _tickets(["one"])
    fake.reset()

    with pytest.raises(DerivationError, match="cannot fit max_judgment_tokens"):
        await _execute_tasks(
            execution,
            db_pool,
            judgment_settings,
            catalog,
            variables=variables,
            citations=_citations(ids),
        )

    assert fake.judgment_calls == []


def _catalog_error(tmp_path: Path, old: str, new: str) -> DefinitionError:
    root = tmp_path / "catalog"
    shutil.copytree(CATALOG, root)
    path = root / "derivations" / "route_tickets.yaml"
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
            "max_judgments: 50, ",
            "",
            "budget",
            "judge Tasks need limits.max_judgments; opt in explicitly",
        ),
        (
            'input: "{{new_tickets.records}}"',
            'input: "tickets: {{new_tickets.records}}"',
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
            'instructions: "Which team should handle the request in `item.content.text`?"',
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


async def test_route_tickets_routes_confident_tickets_end_to_end(
    judgment_settings: Settings,
    catalog: DefinitionCatalog,
    db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = await create_workspace(db_pool, "judge-e2e")
    account = PublicRecordInput(
        entity="customer:northwind",
        collection="accounts",
        type="profile",
        key="profile",
        text="Northwind Traders on the basic plan.",
        content={"plan": "basic"},
    )
    tickets = tuple(
        PublicRecordInput(
            entity="customer:northwind", collection="tickets", type="ticket", text=text
        )
        for text in (
            "You charged our card twice for September.",
            "Something looks off with our account.",
        )
    )
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(records=(account, *tickets)),
        catalog=catalog,
        settings=judgment_settings,
    )
    account_id, refund_id, vague_id = (record.id for record in inserted.inserted)
    fake.reset()
    script_inbox(monkeypatch)
    assert (await enrich_once(db_pool, judgment_settings, catalog)).ready == 3
    await enqueue_derivation_job(
        db_pool,
        workspace=credential.workspace,
        derivation="route_tickets",
        entity="customer:northwind",
    )
    claimed = await claim_job(
        db_pool,
        worker_id="judge-e2e",
        kinds=("derive",),
        derivations=("route_tickets",),
        lease_s=judgment_settings.job_lease_s,
        max_attempts=judgment_settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()

    result = await process_derivation_job(
        db_pool, claimed=claimed, settings=judgment_settings, catalog=catalog
    )

    assert (result.disposition, result.output_count) == ("done", 1)
    assert [call.state["context"]["account"][0]["id"] for call in fake.judgment_calls] == [
        str(account_id),
        str(account_id),
    ]
    async with db_pool.connection() as conn:
        routes = await (
            await conn.execute(
                """
                select type, content, derived_from from record
                where workspace = %s and collection = 'routes'
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
    assert [(row["type"], row["content"]) for row in routes] == [
        (
            "route",
            {
                "text": "You charged our card twice for September.",
                "kind": "route",
                "judgment": {
                    "team": choice("billing", 0.91, TEAMS),
                    "covered": noul(0.85),
                },
            },
        )
    ]
    assert refund_id in routes[0]["derived_from"]
    assert vague_id not in routes[0]["derived_from"]
    assert run is not None
    assert run["content"]["usage"]["judgments"] == 2
    assert [call["resolved"] for call in run["content"]["model_calls"]] == [
        "fake:jev-1.13.0",
        "fake:jev-1.13.0",
    ]
