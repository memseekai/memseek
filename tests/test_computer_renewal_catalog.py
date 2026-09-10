"""Deterministic CI proof for the Computer-backed renewal fixture."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from memseek.computers import (
    FAKE_COMPUTER_PROVIDER,
    ComputerExecutionError,
    ComputerResult,
    execute_agent,
)
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.derive.basis import EvaluationBasis
from memseek.derive.provenance import ProvenanceValue
from memseek.derive.runner import _execute_tasks, _Execution
from memseek.derive.schema import PipelineDefinition
from memseek.invocations import (
    InvocationCreate,
    InvocationTurn,
    continue_invocation,
    create_invocation,
    execute_invocation,
    read_invocation,
    read_invocation_events,
)
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

ROOT = Path(__file__).resolve().parents[1] / "examples" / "computer_renewal_catalog"


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "models_file": ROOT / "conf/models.yaml",
            "processors_file": ROOT / "conf/processors.yaml",
            "collections_dir": ROOT / "collections",
            "derivations_dir": ROOT / "derivations",
            "views_dir": None,
            "artifacts_dir": ROOT / "artifacts",
            "computers_dir": ROOT / "computers",
            "programs_dir": ROOT / "programs",
            "agents_dir": ROOT / "agents",
            "context_policies_dir": ROOT / "context_policies",
            "toolsets_dir": ROOT / "toolsets",
            "mcp_dir": ROOT / "mcp",
            "packages_dir": ROOT / "packages",
            "triggers_dir": None,
            "search_profiles_file": ROOT / "conf/search_profiles.yaml",
            "rank_default_file": ROOT / "conf/rank_default.yaml",
        }
    )


def _execution(
    definition: PipelineDefinition,
    settings: Settings,
    *,
    visible_ids: set[Any],
) -> _Execution:
    now = datetime.now(UTC)
    return _Execution(
        workspace="renewal-demo",
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
        visible_ids=visible_ids,
        final_source_ids=frozenset(),
        final_visible_ids=frozenset(),
        high_seq=0,
        started_at=now,
        completed_at=now,
        model_calls=[],
        task_trace=[],
    )


async def test_renewal_fixture_runs_program_and_agent_derivations(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    demo_settings = _settings(settings)
    catalog = load_definition_catalog(demo_settings)
    assert set(catalog.derivations) == {"contract_extract", "renewal_assessment"}
    assert catalog.packages[("computer_renewal_demo", "1.0.0")].definition_hash

    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            ("renewal-demo", "d" * 64),
        )

    contract_id = uuid4()
    incident_id = uuid4()
    promise_id = uuid4()

    async def extract(request: Any) -> ComputerResult:
        source = request.input["contracts"][0]
        return ComputerResult(
            value={
                "records": [
                    {
                        "text": "Renewal term: 12% service-credit protection.",
                        "citations": [source["id"]],
                        "content": {
                            "kind": "contract_term",
                            "value": "12% service-credit protection",
                        },
                    }
                ]
            },
            citation_ids=request.citation_ids,
            receipt={"provider": "fake", "output_sha256": "1" * 64},
        )

    async def assess(request: Any) -> ComputerResult:
        assert request.context_files
        assert "/.memseek/instructions.md" in request.context_files
        return ComputerResult(
            value={
                "records": [
                    {
                        "text": "High renewal risk: 99.91% uptime activates the buried "
                        "12% discount promise.",
                        "citations": [str(promise_id), str(incident_id)],
                        "content": {"kind": "renewal_risk", "severity": "high"},
                    }
                ]
            },
            citation_ids=frozenset({promise_id, incident_id}),
            receipt={"provider": "fake", "output_sha256": "2" * 64},
            steps=7,
        )

    FAKE_COMPUTER_PROVIDER.clear()
    FAKE_COMPUTER_PROVIDER.register_program("contract_extract@3", extract)
    FAKE_COMPUTER_PROVIDER.register_agent("renewal_analyst@2", assess)
    try:
        direct = _execution(
            catalog.derivations["contract_extract"],
            demo_settings,
            visible_ids={contract_id},
        )
        await _execute_tasks(
            direct,
            db_pool,
            demo_settings,
            catalog,
            variables={
                "entity": "account:acme",
                "run": {"now": datetime.now(UTC).isoformat(), "checkpoint": 1},
                "new_contracts": ProvenanceValue(
                    {
                        "records": [
                            {
                                "id": str(contract_id),
                                "content": {
                                    "text": "12% protection if uptime misses 99.95%",
                                    "kind": "contract",
                                },
                            }
                        ]
                    },
                    frozenset({contract_id}),
                ),
            },
            citations={"new_contracts": frozenset({contract_id})},
        )
        assert direct.output[0]["content"]["kind"] == "contract_term"
        assert direct.logical_llm_calls == 0

        agentic = _execution(
            catalog.derivations["renewal_assessment"],
            demo_settings,
            visible_ids={promise_id, incident_id},
        )
        await _execute_tasks(
            agentic,
            db_pool,
            demo_settings,
            catalog,
            variables={
                "entity": "account:acme",
                "run": {"now": datetime.now(UTC).isoformat(), "checkpoint": 2},
                "renewal_basis": ProvenanceValue(
                    {
                        "records": [
                            {
                                "id": str(promise_id),
                                "content": {"text": "We promise a 12% discount below 99.95%."},
                            },
                            {
                                "id": str(incident_id),
                                "content": {"text": "Quarter uptime was 99.91%."},
                            },
                        ]
                    },
                    frozenset({promise_id, incident_id}),
                ),
            },
            citations={"renewal_basis": frozenset({promise_id, incident_id})},
        )
        assert agentic.output[0]["content"]["severity"] == "high"
        assert agentic.computer_trace is not None
        assert agentic.computer_trace[0]["executor"] == "renewal_analyst@2"
    finally:
        FAKE_COMPUTER_PROVIDER.clear()


async def test_agent_pauses_before_protected_context_is_evicted(settings: Settings) -> None:
    demo_settings = _settings(settings)
    catalog = load_definition_catalog(demo_settings)
    with pytest.raises(ComputerExecutionError, match="protected Agent context") as caught:
        await execute_agent(
            settings=demo_settings,
            catalog=catalog,
            workspace="renewal-demo",
            entity="account:acme",
            run_key="oversized-context",
            task_id="assessment",
            computer_ref="research_workspace@1",
            agent_ref="renewal_analyst@1",
            context_policy_ref="evidence_spine@1",
            input_value={"objective": "renewal"},
            source_ids=frozenset(),
            citation_ids=frozenset(),
            output_path="/outbox/final-result.json",
            output_schema={"type": "object"},
            context_files={"/.memseek/context.md": "e" * 210_000},
            max_output_bytes=1024,
            max_steps=24,
        )
    assert caught.value.code == "context_exhausted"


async def test_interactive_outbox_auto_ingests_observation_and_stages_proposal(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    demo_settings = _settings(settings)
    catalog = load_definition_catalog(demo_settings)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            ("renewal-demo", "e" * 64),
        )
    evidence = await insert_public_records(
        db_pool,
        workspace="renewal-demo",
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="account:acme",
                    collection="renewal_evidence",
                    type="evidence",
                    key="latest-incident",
                    text="Quarter uptime was 99.91%.",
                    content={"kind": "incident"},
                ),
            )
        ),
        catalog=catalog,
        settings=demo_settings,
    )
    evidence_id = evidence.inserted[0].id
    async with db_pool.connection() as conn:
        await conn.execute("update record set enriched_at = now() where id = %s", (evidence_id,))

    async def agent(request: Any) -> ComputerResult:
        observation = {
            "text": "The uptime miss is material renewal evidence.",
            "content": {"kind": "observation"},
            "citations": [str(evidence_id)],
        }
        schemas = {
            item["path"]: item["schema"]
            for item in json.loads(request.context_files["/.memseek/writeback-schemas.json"])
        }
        assert schemas["/outbox/observations.jsonl"]["additionalProperties"] is False
        assert schemas["/outbox/proposals"]["properties"]["kind"] == {"const": "pricing_commitment"}
        proposal = {
            "text": "Offer a new 12% renewal discount.",
            "content": {"kind": "pricing_commitment"},
            "citations": [str(evidence_id)],
        }
        observation_content = json.dumps(observation) + "\n"
        proposal_content = json.dumps(proposal)
        return ComputerResult(
            value={"brief": "Escalate the renewal."},
            citation_ids=frozenset({evidence_id}),
            receipt={
                "provider": "fake",
                "output_sha256": "f" * 64,
                "outbox": [
                    {
                        "path": "/outbox/observations.jsonl",
                        "type": "observations",
                        "content": observation_content,
                        "bytes": len(observation_content.encode()),
                        "sha256": hashlib.sha256(observation_content.encode()).hexdigest(),
                    },
                    {
                        "path": "/outbox/proposals/pricing.json",
                        "type": "maintained_state",
                        "content": proposal_content,
                        "bytes": len(proposal_content.encode()),
                        "sha256": hashlib.sha256(proposal_content.encode()).hexdigest(),
                    },
                ],
            },
            steps=3,
        )

    FAKE_COMPUTER_PROVIDER.clear()
    FAKE_COMPUTER_PROVIDER.register_agent("renewal_analyst@1", agent)
    try:
        created = await create_invocation(
            db_pool,
            workspace="renewal-demo",
            request=InvocationCreate.model_validate(
                {
                    "entity": "account:acme",
                    "computer": "research_workspace@1",
                    "executor": {
                        "kind": "agent",
                        "agent": "renewal_analyst@1",
                        "context_policy": "evidence_spine@1",
                    },
                    "task": {"kind": "answer", "prompt": "Prepare renewal strategy."},
                    "session": {"mode": "new"},
                }
            ),
            catalog=catalog,
        )
        invocation_id = UUID(created["invocation_id"])
        await execute_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
            catalog=catalog,
            settings=demo_settings,
        )
        async with db_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select collection, status from record where run_id is null "
                    "and collection in ('task_observations', 'renewal_proposals') "
                    "order by collection"
                )
            ).fetchall()
        assert [(row["collection"], row["status"]) for row in rows] == [
            ("renewal_proposals", "draft"),
            ("task_observations", "active"),
        ]
        events = await read_invocation_events(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
        )
        assert "writeback_ingested" in [event["kind"] for event in events["events"]]
    finally:
        FAKE_COMPUTER_PROVIDER.clear()


async def test_durable_agent_can_pause_for_a_turn_and_continue(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    demo_settings = _settings(settings)
    catalog = load_definition_catalog(demo_settings)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            ("renewal-demo", "f" * 64),
        )

    async def agent(request: Any) -> ComputerResult:
        turns = request.input["turns"]
        return ComputerResult(
            value=(
                {"question": "Which pricing guardrail should I use?"}
                if not turns
                else {"brief": f"Guardrail recorded: {turns[-1]['prompt']}"}
            ),
            citation_ids=frozenset(),
            receipt={"provider": "fake", "output_sha256": "a" * 64},
            steps=2,
            awaiting_input=not turns,
        )

    FAKE_COMPUTER_PROVIDER.clear()
    FAKE_COMPUTER_PROVIDER.register_agent("renewal_analyst@1", agent)
    try:
        created = await create_invocation(
            db_pool,
            workspace="renewal-demo",
            request=InvocationCreate.model_validate(
                {
                    "entity": "account:acme",
                    "computer": "research_workspace@1",
                    "executor": {
                        "kind": "agent",
                        "agent": "renewal_analyst@1",
                        "context_policy": "evidence_spine@1",
                    },
                    "task": {"kind": "answer", "prompt": "Prepare renewal strategy."},
                }
            ),
            catalog=catalog,
        )
        invocation_id = UUID(created["invocation_id"])
        await execute_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
            catalog=catalog,
            settings=demo_settings,
        )
        paused = await read_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
        )
        assert paused["status"] == "awaiting_input"
        await continue_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
            turn=InvocationTurn(prompt="Do not exceed the approved 12%."),
        )
        await execute_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
            catalog=catalog,
            settings=demo_settings,
        )
        finished = await read_invocation(
            db_pool,
            workspace="renewal-demo",
            invocation_id=invocation_id,
        )
        assert finished["status"] == "succeeded"
        assert "approved 12%" in finished["result"]["value"]["brief"]
    finally:
        FAKE_COMPUTER_PROVIDER.clear()
