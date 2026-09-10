"""Contract tests for Computer definitions, fake execution, and durable invocations."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from memseek.computers import (
    FAKE_COMPUTER_PROVIDER,
    ComputerExecutionError,
    ComputerResult,
    execute_program,
)
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import (
    DefinitionSources,
    compile_definition_catalog,
    load_definition_catalog,
)
from memseek.definitions.models import ComputerDefinition, ProgramDefinition
from memseek.derive.basis import EvaluationBasis
from memseek.derive.provenance import ProvenanceValue
from memseek.derive.runner import _execute_tasks, _Execution
from memseek.derive.schema import PipelineDefinition
from memseek.evidence_spine import ContextPressure, read_memory_nodes
from memseek.invocations import (
    InvocationCreate,
    SessionSelection,
    create_invocation,
    execute_invocation,
    list_invocation_artifacts,
    read_invocation,
    read_invocation_artifact,
    read_invocation_events,
)


def _catalog(settings: Settings) -> Any:
    base = load_definition_catalog(settings)
    computer = ComputerDefinition.model_validate(
        {
            "name": "contract_worker",
            "version": 1,
            "active": True,
            "provider": "fake",
            "writable": ["/workspace", "/outbox"],
            "runtime": {"default": "worker-javascript"},
            "capabilities": {"filesystem": True, "exec": True, "network": False},
            "writeback": [
                {"path": "/outbox/final-result.json", "type": "final_result", "review": False}
            ],
            "retention": {"workspace_days": 30, "preserve": ["/outbox"]},
        }
    )
    program = ProgramDefinition.model_validate(
        {
            "name": "contract_extract",
            "version": 3,
            "active": True,
            "runtime": "worker-javascript",
            "entrypoint": "main.js",
            "files": {"main.js": "export default async (input) => input;"},
            "input_schema": {
                "type": "object",
                "required": ["contract"],
                "properties": {"contract": {"type": "string"}},
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "required": ["term"],
                "properties": {"term": {"type": "string"}},
                "additionalProperties": False,
            },
            "capabilities": ["filesystem"],
        }
    )
    sources = DefinitionSources.from_catalog(base)
    return compile_definition_catalog(
        settings,
        replace(sources, computers=(computer,), programs=(program,)),
    )


@pytest.fixture(autouse=True)
def clean_fake_provider() -> Iterator[None]:
    FAKE_COMPUTER_PROVIDER.clear()
    yield
    FAKE_COMPUTER_PROVIDER.clear()


def test_program_and_computer_policy_models_reject_unsafe_shapes() -> None:
    with pytest.raises(ValidationError, match="immutable command"):
        ProgramDefinition.model_validate(
            {
                "name": "bad",
                "version": 1,
                "runtime": "container",
                "entrypoint": "main.js",
                "files": {"main.js": "console.log(1)"},
                "input_schema": {},
                "output_schema": {},
                "capabilities": ["filesystem", "exec"],
            }
        )
    with pytest.raises(ValidationError, match="read-only"):
        ComputerDefinition.model_validate(
            {
                "name": "bad",
                "version": 1,
                "provider": "fake",
                "writable": ["/.memseek"],
            }
        )
    with pytest.raises(ValidationError, match="require collection and record_type"):
        ComputerDefinition.model_validate(
            {
                "name": "bad_writeback",
                "version": 1,
                "provider": "fake",
                "writeback": [
                    {
                        "path": "/outbox/observations.jsonl",
                        "type": "observations",
                        "review": False,
                    }
                ],
            }
        )


def test_context_pressure_uses_declared_evidence_spine_thresholds() -> None:
    assert (
        ContextPressure(used_tokens=10, max_input_tokens=100, reserve_output_tokens=10).action
        == "keep"
    )


async def test_direct_computer_task_runs_inside_derivation_boundary(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = _catalog(settings)
    definition = PipelineDefinition.model_validate(
        {
            "name": "contract_terms",
            "sources": {
                "contract": {
                    "kind": "changes",
                    "collections": ["main"],
                    "types": ["event"],
                }
            },
            "limits": {
                "max_computer_runs": 1,
                "max_computer_output_bytes": 1024,
            },
            "tasks": [
                {
                    "id": "extracted",
                    "use": "computer",
                    "input": {"contract": "{{contract.text}}"},
                    "with": {
                        "computer": "contract_worker@1",
                        "program": "contract_extract@3",
                        "output": "/outbox/result.json",
                    },
                }
            ],
            "emit": {
                "from": "{{extracted}}",
                "collection": "main",
                "type": "observation",
            },
        }
    )
    now = datetime.now(UTC)
    execution = _Execution(
        workspace="computer-test",
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
    source_id = uuid4()

    async def handler(request: Any) -> ComputerResult:
        assert request.mode == "derivation"
        assert request.input == {"contract": "Payment is due in 30 days."}
        return ComputerResult(
            value={"term": "net 30"},
            citation_ids=request.citation_ids,
            receipt={"provider": "fake", "output_sha256": "c" * 64},
        )

    FAKE_COMPUTER_PROVIDER.register_program("contract_extract@3", handler)
    variables = {
        "entity": "account:acme",
        "run": {"now": now.isoformat(), "checkpoint": 1, "source_ids": []},
        "contract": ProvenanceValue(
            {"text": "Payment is due in 30 days."},
            frozenset({source_id}),
        ),
    }
    await _execute_tasks(
        execution,
        db_pool,
        settings,
        catalog,
        variables=variables,
        citations={"contract": frozenset({source_id})},
    )

    assert execution.output == {"term": "net 30"}
    assert execution.final_visible_ids == frozenset({source_id})
    assert execution.computer_runs == 1
    assert execution.computer_trace is not None
    assert execution.computer_trace[0]["executor"] == "contract_extract@3"
    assert (
        ContextPressure(used_tokens=65, max_input_tokens=100, reserve_output_tokens=10).action
        == "pointerize"
    )
    assert (
        ContextPressure(used_tokens=75, max_input_tokens=100, reserve_output_tokens=10).action
        == "compact"
    )
    assert (
        ContextPressure(used_tokens=85, max_input_tokens=100, reserve_output_tokens=10).action
        == "pause"
    )


async def test_fake_provider_validates_output_and_preserves_session_key(settings: Settings) -> None:
    catalog = _catalog(settings)

    async def handler(request: Any) -> ComputerResult:
        return ComputerResult(
            value={"term": "net 30"},
            citation_ids=request.citation_ids,
            receipt={"provider": "fake", "session_key": request.session_key},
        )

    FAKE_COMPUTER_PROVIDER.register_program("contract_extract@3", handler)
    result = await execute_program(
        settings=settings,
        catalog=catalog,
        workspace="computer-test",
        entity="account:acme",
        run_key="run-1",
        task_id="extract",
        computer_ref="contract_worker@1",
        program_ref="contract_extract@3",
        input_value={"contract": "Payment is due in 30 days."},
        source_ids=frozenset(),
        citation_ids=frozenset(),
        output_path="/outbox/result.json",
        max_output_bytes=1024,
    )
    assert result.value == {"term": "net 30"}
    assert len(FAKE_COMPUTER_PROVIDER.requests) == 1
    assert len(FAKE_COMPUTER_PROVIDER.requests[0].session_key) == 64

    async def invalid(_request: Any) -> Any:
        return {"unexpected": True}

    FAKE_COMPUTER_PROVIDER.register_program("contract_extract@3", invalid)
    with pytest.raises(ComputerExecutionError, match="required property"):
        await execute_program(
            settings=settings,
            catalog=catalog,
            workspace="computer-test",
            entity="account:acme",
            run_key="run-2",
            task_id="extract",
            computer_ref="contract_worker@1",
            program_ref="contract_extract@3",
            input_value={"contract": "Payment is due in 30 days."},
            source_ids=frozenset(),
            citation_ids=frozenset(),
            output_path="/outbox/result.json",
            max_output_bytes=1024,
        )


async def test_durable_program_invocation_is_idempotent_and_builds_evidence_spine(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = _catalog(settings)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            ("computer-test", "a" * 64),
        )

    async def handler(request: Any) -> ComputerResult:
        return ComputerResult(
            value={"term": "net 30"},
            citation_ids=frozenset(),
            receipt={
                "provider": "fake",
                "session_key": request.session_key,
                "output_sha256": "b" * 64,
                "commands": [{"backend": "worker-javascript", "exit_code": 0}],
                "files": [{"path": "/workspace/terms.json", "after_sha256": "c" * 64}],
                "events": [
                    {
                        "kind": "model_step",
                        "payload": {"index": 0, "finish_reason": "stop"},
                    }
                ],
            },
        )

    FAKE_COMPUTER_PROVIDER.register_program("contract_extract@3", handler)
    body = InvocationCreate.model_validate(
        {
            "entity": "account:acme",
            "computer": "contract_worker@1",
            "executor": {"kind": "program", "program": "contract_extract@3"},
            "task": {"kind": "compute", "input": {"contract": "Payment due in 30 days."}},
            "session": {"mode": "new"},
            "idempotency_key": "acme-contract-2026",
        }
    )
    created = await create_invocation(
        db_pool, workspace="computer-test", request=body, catalog=catalog
    )
    replay = await create_invocation(
        db_pool, workspace="computer-test", request=body, catalog=catalog
    )
    assert replay["invocation_id"] == created["invocation_id"]

    invocation_id = UUID(created["invocation_id"])
    await execute_invocation(
        db_pool,
        workspace="computer-test",
        invocation_id=invocation_id,
        catalog=catalog,
        settings=settings,
    )
    finished = await read_invocation(
        db_pool, workspace="computer-test", invocation_id=invocation_id
    )
    assert finished["status"] == "succeeded"
    assert finished["result"]["value"] == {"term": "net 30"}
    events = await read_invocation_events(
        db_pool, workspace="computer-test", invocation_id=invocation_id
    )
    assert [event["kind"] for event in events["events"]] == [
        "queued",
        "started",
        "command",
        "file_change",
        "model_step",
        "completed",
    ]
    memory = await read_memory_nodes(
        db_pool, workspace="computer-test", invocation_id=invocation_id
    )
    assert {node["kind"] for node in memory["nodes"]} == {
        "working_brief",
        "episode_receipt",
    }
    artifacts = await list_invocation_artifacts(
        db_pool, workspace="computer-test", invocation_id=invocation_id
    )
    assert len(artifacts["artifacts"]) == 1
    artifact = await read_invocation_artifact(
        db_pool,
        workspace="computer-test",
        invocation_id=invocation_id,
        artifact_id=UUID(artifacts["artifacts"][0]["id"]),
    )
    assert artifact["path"] == "/outbox/final-result.json"
    assert artifact["content"] == {"term": "net 30"}

    fork_request = body.model_copy(
        update={
            "session": SessionSelection(
                mode="fork",
                session_id=UUID(created["session_id"]),
            ),
            "idempotency_key": "acme-contract-2026-fork",
        }
    )
    forked = await create_invocation(
        db_pool,
        workspace="computer-test",
        request=fork_request,
        catalog=catalog,
    )
    await execute_invocation(
        db_pool,
        workspace="computer-test",
        invocation_id=UUID(forked["invocation_id"]),
        catalog=catalog,
        settings=settings,
    )
    assert FAKE_COMPUTER_PROVIDER.requests[-1].parent_session_key
    assert (
        FAKE_COMPUTER_PROVIDER.requests[-1].parent_session_key
        == FAKE_COMPUTER_PROVIDER.requests[0].session_key
    )
