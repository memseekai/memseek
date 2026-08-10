"""General trusted Task Adapter Interface tests."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

import memseek.derive.runner as runner_module
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.derive.basis import EvaluationBasis
from memseek.derive.candidates import compile_candidate_set
from memseek.derive.errors import DerivationError
from memseek.derive.provenance import ProvenanceValue
from memseek.derive.runner import _execute_tasks, _Execution
from memseek.derive.schema import PipelineDefinition
from memseek.derive.tasks import (
    LLMTaskConfig,
    TaskConfigModel,
    TaskContext,
    TaskResult,
    register_task,
    task_adapter,
)
from memseek.llm.fake import fake
from memseek.llm.registry import Completion


class _MirrorConfig(TaskConfigModel):
    prefix: str
    invent_provenance: bool = False
    narrow_citations: bool = False
    widen_citations: bool = False
    invalid_output: bool = False


class _MirrorInput(BaseModel):
    records: list[dict[str, Any]]


class _MirrorOutput(BaseModel):
    records: list[dict[str, Any]]


def test_public_task_interface_imports_without_catalog_bootstrap() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from memseek.derive import TaskConfigModel, TaskContext, TaskResult, register_task",
        ],
        check=True,
    )


def test_llm_task_requires_an_explicit_output_schema() -> None:
    with pytest.raises(ValidationError, match="valid dictionary"):
        LLMTaskConfig.model_validate(
            {
                "prompt": "Return JSON.",
                "output_schema": "record_drafts",
            }
        )


async def _mirror(context: TaskContext, value: Any, config: TaskConfigModel) -> Any:
    assert not hasattr(context, "pool")
    assert not hasattr(context, "connection")
    assert context.entity == "person:1"
    assert isinstance(value, _MirrorInput)
    assert isinstance(config, _MirrorConfig)
    typed_value = value
    typed_config = config
    source = typed_value.records[0]
    if typed_config.invalid_output:
        return {"unexpected": True}
    source_text = source.get("text", source.get("content", {}).get("text"))
    citation = source.get("id", source.get("citations", [None])[0])
    output = _MirrorOutput(
        records=[
            {
                "text": f"{typed_config.prefix}{source_text}",
                "citations": [citation],
            }
        ]
    )
    if typed_config.invent_provenance:
        return TaskResult(output, source_ids=frozenset({uuid4()}))
    if typed_config.narrow_citations:
        return TaskResult(output, citation_ids=frozenset())
    if typed_config.widen_citations:
        return TaskResult(output, citation_ids=frozenset({uuid4()}))
    return output


def _register_mirror() -> None:
    try:
        task_adapter("test_mirror")
    except KeyError:
        register_task(
            "test_mirror",
            implementation_hash="1" * 64,
            config_model=_MirrorConfig,
            input_type=_MirrorInput,
            output_type=_MirrorOutput,
            handler=_mirror,
        )


def _definition(*, invent: bool = False, narrow: bool = False) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {
            "name": "task_test",
            "sources": {
                "evidence": {
                    "kind": "changes",
                    "collections": ["main"],
                    "types": ["event"],
                }
            },
            "tasks": [
                {
                    "id": "result",
                    "use": "test_mirror",
                    "input": {"records": "{{evidence.records}}"},
                    "with": {
                        "prefix": "copy: ",
                        "invent_provenance": invent,
                        "narrow_citations": narrow,
                    },
                }
            ],
            "emit": {
                "from": "{{result.records}}",
                "collection": "main",
                "type": "observation",
            },
        }
    )


def _execution(definition: PipelineDefinition, settings: Settings) -> _Execution:
    now = datetime.now(UTC)
    basis = EvaluationBasis(
        mode="changes",
        from_seq=0,
        through_seq=0,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(),
    )
    return _Execution(
        workspace="task-test",
        build_sha=settings.memseek_build_sha,
        definition=definition,
        config_hash=definition.definition_hash,
        basis=basis,
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


async def test_installed_task_receives_typed_input_and_emits_without_storage_access(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    definition = _definition()
    execution = _execution(definition, settings)
    catalog = load_definition_catalog(settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "typed evidence"}}]},
            frozenset({source_id}),
        ),
    }

    await _execute_tasks(
        execution,
        db_pool,
        settings,
        catalog,
        variables=variables,
        citations={"evidence": frozenset({source_id})},
    )

    assert execution.output == [
        {
            "text": "copy: typed evidence",
            "citations": [str(source_id)],
        }
    ]
    assert execution.final_visible_ids == frozenset({source_id})
    assert execution.task_trace
    assert execution.task_trace[0]["use"] == "test_mirror"


async def test_atom_extraction_emits_only_cited_bounded_atoms(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    definition = catalog.derivations["atom_extraction"]
    execution = _execution(definition, settings)
    transcript_id = uuid4()
    execution.visible_ids.add(transcript_id)
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "new_transcripts": ProvenanceValue(
            {
                "records": [
                    {
                        "id": str(transcript_id),
                        "content": {"text": "Maya committed to send the proposal on Friday."},
                    }
                ],
                "rendered": ProvenanceValue(
                    f'<records untrusted="true">[id={transcript_id}] '
                    "Maya committed to send the proposal on Friday.</records>",
                    frozenset({transcript_id}),
                    pre_escaped=True,
                ),
            },
            frozenset({transcript_id}),
        ),
    }
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"text":"Maya committed to send the proposal on Friday.",'
            '"citations":["'
            + str(transcript_id)
            + '"],"content":{"kind":"commitment","confidence":0.95}}]}'
        )
    )

    await _execute_tasks(
        execution,
        db_pool,
        settings,
        catalog,
        variables=variables,
        citations={"new_transcripts": frozenset({transcript_id})},
    )

    assert definition.model == "cheap"
    assert definition.limits.max_llm_calls == 2
    assert definition.emit.collection == "atoms"
    assert execution.output == [
        {
            "text": "Maya committed to send the proposal on Friday.",
            "citations": [str(transcript_id)],
            "content": {"kind": "commitment", "confidence": 0.95},
        }
    ]
    assert execution.final_source_ids == frozenset({transcript_id})
    assert execution.final_visible_ids == frozenset({transcript_id})
    assert len(fake.completion_calls) == 1
    assert fake.completion_calls[0].output_schema == definition.tasks[0].config["output_schema"]


async def test_installed_task_cannot_invent_provenance(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    definition = _definition(invent=True)
    execution = _execution(definition, settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "evidence"}}]},
            frozenset({source_id}),
        ),
    }

    with pytest.raises(DerivationError, match="invented provenance"):
        await _execute_tasks(
            execution,
            db_pool,
            settings,
            load_definition_catalog(settings),
            variables=variables,
            citations={"evidence": frozenset({source_id})},
        )


async def test_task_result_can_narrow_direct_citation_authority(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    definition = _definition(narrow=True)
    execution = _execution(definition, settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "evidence"}}]},
            frozenset({source_id}),
        ),
    }

    await _execute_tasks(
        execution,
        db_pool,
        settings,
        load_definition_catalog(settings),
        variables=variables,
        citations={"evidence": frozenset({source_id})},
    )

    assert str(source_id) in str(execution.output)
    assert execution.final_visible_ids == frozenset()
    with pytest.raises(DerivationError, match="was not available"):
        compile_candidate_set(
            execution.output,
            emit=definition.emit,
            basis=execution.basis,
            visible=execution.final_visible_ids,
            settings=settings,
            catalog=load_definition_catalog(settings),
        )


async def test_task_result_cannot_widen_citation_authority(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    definition = _definition()
    task = definition.tasks[0]
    definition = definition.model_copy(
        update={
            "tasks": (task.model_copy(update={"config": {**task.config, "widen_citations": True}}),)
        }
    )
    execution = _execution(definition, settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "evidence"}}]},
            frozenset({source_id}),
        ),
    }

    with pytest.raises(DerivationError, match="widened citation authority"):
        await _execute_tasks(
            execution,
            db_pool,
            settings,
            load_definition_catalog(settings),
            variables=variables,
            citations={"evidence": frozenset({source_id})},
        )


async def test_ordered_tasks_compose_typed_values_and_provenance(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    definition = _definition()
    first = definition.tasks[0].model_copy(update={"id": "first"})
    second = definition.tasks[0].model_copy(
        update={
            "id": "second",
            "input": {"records": "{{first.records}}"},
            "config": {"prefix": "again: "},
        }
    )
    definition = definition.model_copy(
        update={
            "tasks": (first, second),
            "emit": definition.emit.model_copy(update={"from_": "{{second.records}}"}),
        }
    )
    execution = _execution(definition, settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "typed evidence"}}]},
            frozenset({source_id}),
        ),
    }

    await _execute_tasks(
        execution,
        db_pool,
        settings,
        load_definition_catalog(settings),
        variables=variables,
        citations={"evidence": frozenset({source_id})},
    )

    assert execution.output[0]["text"] == "again: copy: typed evidence"
    assert execution.final_visible_ids == frozenset({source_id})
    assert [item["task"] for item in execution.task_trace or []] == ["first", "second"]


async def test_shipped_llm_search_llm_pipeline_preserves_order_and_authority(
    settings: Settings,
    db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_definition_catalog(settings)
    definition = catalog.derivations["reflection"]
    execution = _execution(definition, settings)
    source_id = uuid4()
    hit_id = uuid4()
    execution.visible_ids.add(source_id)
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "recent_memories": ProvenanceValue(
            {
                "records": [{"id": str(source_id), "content": {"text": "evidence"}}],
                "rendered": ProvenanceValue(
                    f'<records untrusted="true">[id={source_id}] evidence</records>',
                    frozenset({source_id}),
                    pre_escaped=True,
                ),
            },
            frozenset({source_id}),
        ),
    }
    retrieved = ProvenanceValue(
        [
            {
                "item": "What persists?",
                "hits": [{"id": str(hit_id), "text": "Persistent evidence"}],
                "rendered": f'<records untrusted="true">[id={hit_id}] evidence</records>',
                "omitted_count": 0,
                "truncated": False,
            }
        ],
        frozenset({hit_id}),
    )

    async def retrieve(*args: Any, **kwargs: Any) -> ProvenanceValue:
        del args, kwargs
        execution.visible_ids.add(hit_id)
        return retrieved

    monkeypatch.setattr(runner_module, "_retrieve", retrieve)
    fake.reset()
    fake.enqueue(
        Completion('{"questions":"not-a-list"}'),
        Completion('{"questions":["What persists?","What changed?","What matters?"]}'),
        Completion(
            '{"records":[{"text":"A durable insight.","citations":["'
            + str(hit_id)
            + '"],"content":{"kind":"reflection"}}]}'
        ),
    )

    await _execute_tasks(
        execution,
        db_pool,
        settings,
        catalog,
        variables=variables,
        citations={"recent_memories": frozenset({source_id})},
    )

    assert [item["task"] for item in execution.task_trace or []] == [
        "qs",
        "evidence_by_question",
        "result",
    ]
    assert execution.final_visible_ids == frozenset({hit_id})
    assert execution.final_source_ids == frozenset({source_id, hit_id})
    assert execution.output[0]["citations"] == [str(hit_id)]
    assert len(fake.completion_calls) == 3
    assert [call.output_mode for call in fake.completion_calls] == [
        "json_schema",
        "json_schema",
        "json_schema",
    ]
    assert [call.output_schema_name for call in fake.completion_calls] == [
        "reflection_qs",
        "reflection_qs",
        "reflection_result",
    ]
    assert fake.completion_calls[0].output_schema == definition.tasks[0].config["output_schema"]
    assert fake.completion_calls[-1].output_schema == definition.tasks[-1].config["output_schema"]
    assert all(call["requested_output_mode"] == "json_schema" for call in execution.model_calls)
    assert all(call["output_mode"] == "json_schema" for call in execution.model_calls)
    assert all(len(str(call["output_schema_sha256"])) == 64 for call in execution.model_calls)


async def test_task_adapter_rejects_invalid_typed_input_and_output(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    _register_mirror()
    catalog = load_definition_catalog(settings)
    source_id = uuid4()
    variables = {
        "entity": "person:1",
        "run": {"now": "2026-01-01T00:00:00Z", "checkpoint": 1, "source_ids": []},
        "evidence": ProvenanceValue(
            {"records": [{"id": str(source_id), "content": {"text": "evidence"}}]},
            frozenset({source_id}),
        ),
    }
    definition = _definition()
    task = definition.tasks[0]
    bad_input = definition.model_copy(
        update={"tasks": (task.model_copy(update={"input": {"records": "bad"}}),)}
    )
    with pytest.raises(DerivationError, match="Task 'result' config"):
        await _execute_tasks(
            _execution(bad_input, settings),
            db_pool,
            settings,
            catalog,
            variables=dict(variables),
            citations={"evidence": frozenset({source_id})},
        )

    bad_output = definition.model_copy(
        update={
            "tasks": (task.model_copy(update={"config": {**task.config, "invalid_output": True}}),)
        }
    )
    with pytest.raises(DerivationError, match="Task 'result' output"):
        await _execute_tasks(
            _execution(bad_output, settings),
            db_pool,
            settings,
            catalog,
            variables=dict(variables),
            citations={"evidence": frozenset({source_id})},
        )


def test_task_registry_requires_async_versioned_implementations() -> None:
    def sync_handler(context: TaskContext, value: Any, config: TaskConfigModel) -> Any:
        del context, value, config
        return None

    with pytest.raises(TypeError, match="must be async"):
        register_task(
            "test_sync",
            implementation_hash="2" * 64,
            config_model=_MirrorConfig,
            handler=sync_handler,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="64 lower-case hex"):
        register_task(
            "test_hash",
            implementation_hash="not-a-hash",
            config_model=_MirrorConfig,
            handler=_mirror,
        )

    class LooseConfig(BaseModel):
        value: str

    with pytest.raises(TypeError, match="inherit TaskConfigModel"):
        register_task(
            "test_loose",
            implementation_hash="3" * 64,
            config_model=cast(type[TaskConfigModel], LooseConfig),
            handler=_mirror,
        )
