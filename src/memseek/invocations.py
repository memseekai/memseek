"""Durable Computer invocation lifecycle and replayable event journal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memseek.computers import ComputerExecutionError, ComputerResult, execute_agent, execute_program
from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.definitions.base import split_exact_reference
from memseek.evidence_spine import persist_completion_memory_tx
from memseek.locks import acquire_workspace_lock
from memseek.materialization import MaterializationError, build_agent_materialization

TERMINAL_INVOCATION_STATUSES = frozenset({"succeeded", "failed", "cancelled", "context_exhausted"})
_MAX_PROVIDER_EVENTS = 512
_MAX_PROVIDER_EVENT_BYTES = 65_536


class InvocationError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _exact_reference(value: str, label: str) -> str:
    try:
        split_exact_reference(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an exact name@version reference") from exc
    return value


class ProgramExecutor(_StrictModel):
    kind: Literal["program"]
    program: str

    @field_validator("program")
    @classmethod
    def exact_program(cls, value: str) -> str:
        return _exact_reference(value, "program")


class AgentExecutor(_StrictModel):
    kind: Literal["agent"]
    agent: str
    context_policy: str

    @field_validator("agent")
    @classmethod
    def exact_agent(cls, value: str) -> str:
        return _exact_reference(value, "agent")

    @field_validator("context_policy")
    @classmethod
    def exact_policy(cls, value: str) -> str:
        return _exact_reference(value, "context_policy")


InvocationExecutor = Annotated[ProgramExecutor | AgentExecutor, Field(discriminator="kind")]


class InvocationTask(_StrictModel):
    kind: Literal["answer", "task", "compute"]
    prompt: str | None = Field(default=None, min_length=1, max_length=32_768)
    input: Any = None

    @model_validator(mode="after")
    def validate_task(self) -> InvocationTask:
        if self.kind in {"answer", "task"} and self.prompt is None:
            raise ValueError(f"{self.kind} task requires prompt")
        if self.kind == "compute" and self.input is None:
            raise ValueError("compute task requires input")
        return self


class SessionSelection(_StrictModel):
    mode: Literal["new", "resume", "fork"] = "new"
    session_id: UUID | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> SessionSelection:
        if self.mode == "new" and self.session_id is not None:
            raise ValueError("new session forbids session_id")
        if self.mode != "new" and self.session_id is None:
            raise ValueError(f"{self.mode} session requires session_id")
        return self


class InvocationCreate(_StrictModel):
    entity: str = Field(min_length=1, max_length=255)
    computer: str
    executor: InvocationExecutor
    task: InvocationTask
    session: SessionSelection = Field(default_factory=SessionSelection)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("entity")
    @classmethod
    def valid_entity(cls, value: str) -> str:
        if not value.strip() or value == "*":
            raise ValueError("entity must be non-blank and cannot be '*'")
        return value

    @field_validator("computer")
    @classmethod
    def exact_computer(cls, value: str) -> str:
        return _exact_reference(value, "computer")


class InvocationTurn(_StrictModel):
    prompt: str = Field(min_length=1, max_length=32_768)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _definition_refs(request: InvocationCreate, catalog: DefinitionCatalog) -> dict[str, Any]:
    try:
        computer = catalog.resolve_computer(request.computer)
        if isinstance(request.executor, ProgramExecutor):
            executor = catalog.resolve_program(request.executor.program)
            refs: dict[str, Any] = {
                "computer": {
                    "ref": request.computer,
                    "hash": computer.definition_hash,
                },
                "executor": {
                    "kind": "program",
                    "ref": request.executor.program,
                    "hash": executor.definition_hash,
                },
            }
        else:
            executor = catalog.resolve_agent(request.executor.agent)
            policy = catalog.resolve_context_policy(request.executor.context_policy)
            if request.computer not in executor.computers:
                raise InvocationError(
                    "computer_capability", "Agent is not allowed to use this Computer"
                )
            refs = {
                "computer": {"ref": request.computer, "hash": computer.definition_hash},
                "executor": {
                    "kind": "agent",
                    "ref": request.executor.agent,
                    "hash": executor.definition_hash,
                },
                "context_policy": {
                    "ref": request.executor.context_policy,
                    "hash": policy.definition_hash,
                },
            }
    except InvocationError:
        raise
    except (KeyError, ValueError) as exc:
        raise InvocationError("reference", str(exc)) from exc
    return refs


async def _append_event_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    invocation_id: UUID,
    kind: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    locked = await conn.execute(
        "select id from invocation where id = %s and workspace = %s for update",
        (invocation_id, workspace),
    )
    if await locked.fetchone() is None:
        raise InvocationError("not_found", "invocation not found", status=404)
    result = await conn.execute(
        "select coalesce(max(ordinal), 0) + 1 as ordinal from invocation_event "
        "where invocation_id = %s",
        (invocation_id,),
    )
    row = await result.fetchone()
    assert row is not None
    ordinal = int(row["ordinal"])
    event_id = uuid4()
    value = dict(payload)
    digest = hashlib.sha256(_canonical_json(value).encode()).hexdigest()
    inserted = await conn.execute(
        """
        insert into invocation_event
          (invocation_id, ordinal, id, workspace, kind, payload, payload_sha256)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning created_at
        """,
        (invocation_id, ordinal, event_id, workspace, kind, Jsonb(value), digest),
    )
    created = await inserted.fetchone()
    assert created is not None
    return {
        "id": str(event_id),
        "ordinal": ordinal,
        "kind": kind,
        "payload": value,
        "payload_sha256": digest,
        "created_at": created["created_at"].isoformat(),
    }


async def create_invocation(
    pool: DatabasePool,
    *,
    workspace: str,
    request: InvocationCreate,
    catalog: DefinitionCatalog,
) -> dict[str, Any]:
    refs = _definition_refs(request, catalog)
    computer = catalog.resolve_computer(request.computer)
    invocation_id = uuid4()
    session_id = uuid4()
    executor_ref = (
        request.executor.program
        if isinstance(request.executor, ProgramExecutor)
        else request.executor.agent
    )
    policy_ref = (
        request.executor.context_policy if isinstance(request.executor, AgentExecutor) else None
    )
    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        if request.idempotency_key is not None:
            existing = await conn.execute(
                "select id from invocation where workspace = %s and idempotency_key = %s",
                (workspace, request.idempotency_key),
            )
            row = await existing.fetchone()
            if row is not None:
                return await _read_invocation_tx(conn, workspace=workspace, invocation_id=row["id"])

        parent_id: UUID | None = None
        if request.session.mode != "new":
            parent = await conn.execute(
                "select * from computer_session where id = %s and workspace = %s for update",
                (request.session.session_id, workspace),
            )
            parent_row = await parent.fetchone()
            if parent_row is None or parent_row["entity"] != request.entity:
                raise InvocationError("session_not_found", "session not found", status=404)
            if request.session.mode == "resume":
                if parent_row["status"] != "active":
                    raise InvocationError("session_inactive", "session is not active", status=409)
                if (
                    parent_row["computer_ref"] != request.computer
                    or dict(parent_row["definition_refs"]) != refs
                ):
                    raise InvocationError(
                        "session_version_conflict",
                        "resume requires the session's pinned definitions; use fork",
                        status=409,
                    )
                session_id = parent_row["id"]
            else:
                parent_id = parent_row["id"]

        if request.session.mode != "resume":
            await conn.execute(
                """
                insert into computer_session
                  (id, workspace, entity, computer_ref, computer_hash, provider,
                   definition_refs, parent_session_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    workspace,
                    request.entity,
                    request.computer,
                    computer.definition_hash,
                    computer.provider,
                    Jsonb(refs),
                    parent_id,
                ),
            )
        try:
            await conn.execute(
                """
                insert into invocation
                  (id, workspace, entity, session_id, executor_kind, executor_ref,
                   context_policy_ref, definition_refs, task, idempotency_key)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    invocation_id,
                    workspace,
                    request.entity,
                    session_id,
                    request.executor.kind,
                    executor_ref,
                    policy_ref,
                    Jsonb(refs),
                    Jsonb(request.task.model_dump(mode="json")),
                    request.idempotency_key,
                ),
            )
        except errors.UniqueViolation as exc:
            raise InvocationError(
                "idempotency_conflict", "idempotency key conflict", status=409
            ) from exc
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="queued",
            payload={"session_id": str(session_id), "definition_refs": refs},
        )
        await conn.execute(
            """
            insert into job (workspace, kind, entity, payload, dedupe_key)
            values (%s, 'invocation', %s, %s, %s)
            """,
            (
                workspace,
                request.entity,
                Jsonb({"invocation_id": str(invocation_id)}),
                f"invocation:{invocation_id}",
            ),
        )
        return await _read_invocation_tx(conn, workspace=workspace, invocation_id=invocation_id)


async def _read_invocation_tx(
    conn: DatabaseConnection, *, workspace: str, invocation_id: UUID
) -> dict[str, Any]:
    result = await conn.execute(
        """
        select i.*, s.computer_ref, s.provider, s.parent_session_id,
               coalesce((select max(e.ordinal) from invocation_event e
                         where e.invocation_id = i.id), 0) as event_cursor
        from invocation i
        join computer_session s on s.id = i.session_id
        where i.id = %s and i.workspace = %s
        """,
        (invocation_id, workspace),
    )
    row = await result.fetchone()
    if row is None:
        raise InvocationError("not_found", "invocation not found", status=404)
    return {
        "invocation_id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "parent_session_id": (
            str(row["parent_session_id"]) if row["parent_session_id"] is not None else None
        ),
        "entity": row["entity"],
        "computer": row["computer_ref"],
        "provider": row["provider"],
        "executor": {"kind": row["executor_kind"], "ref": row["executor_ref"]},
        "context_policy": row["context_policy_ref"],
        "definition_refs": row["definition_refs"],
        "task": row["task"],
        "status": row["status"],
        "event_cursor": int(row["event_cursor"]),
        "result": row["result"],
        "error": (
            None
            if row["error_kind"] is None
            else {"kind": row["error_kind"], "detail": row["error"]}
        ),
        "created_at": row["created_at"].isoformat(),
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }


async def read_invocation(
    pool: DatabasePool, *, workspace: str, invocation_id: UUID
) -> dict[str, Any]:
    async with pool.connection() as conn:
        return await _read_invocation_tx(conn, workspace=workspace, invocation_id=invocation_id)


async def read_invocation_events(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    after: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    if after < 0 or not 1 <= limit <= 500:
        raise InvocationError("request", "invalid event cursor or limit")
    async with pool.connection() as conn:
        exists = await conn.execute(
            "select 1 from invocation where id = %s and workspace = %s",
            (invocation_id, workspace),
        )
        if await exists.fetchone() is None:
            raise InvocationError("not_found", "invocation not found", status=404)
        result = await conn.execute(
            """
            select id, ordinal, kind, payload, payload_sha256, created_at
            from invocation_event
            where invocation_id = %s and workspace = %s and ordinal > %s
            order by ordinal
            limit %s
            """,
            (invocation_id, workspace, after, limit),
        )
        rows = await result.fetchall()
    events = [
        {
            "id": str(row["id"]),
            "ordinal": int(row["ordinal"]),
            "kind": row["kind"],
            "payload": row["payload"],
            "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    return {"events": events, "cursor": events[-1]["ordinal"] if events else after}


async def execute_invocation(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    catalog: DefinitionCatalog,
    settings: Settings,
    final_attempt: bool = False,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            "select invocation.*, s.parent_session_id "
            "from invocation join computer_session s on s.id = invocation.session_id "
            "where invocation.id = %s and invocation.workspace = %s for update of invocation",
            (invocation_id, workspace),
        )
        row = await result.fetchone()
        if row is None:
            raise InvocationError("not_found", "invocation not found", status=404)
        if row["status"] in TERMINAL_INVOCATION_STATUSES:
            return
        if row["status"] not in {"queued", "awaiting_input"}:
            raise InvocationError(
                "state", f"invocation cannot run from {row['status']}", status=409
            )
        await conn.execute(
            "update invocation set status = 'running', started_at = coalesce(started_at, now()), "
            "updated_at = now() where id = %s",
            (invocation_id,),
        )
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="started",
            payload={"executor": row["executor_ref"]},
        )
        invocation = dict(row)
        turns_result = await conn.execute(
            "select payload from invocation_event "
            "where invocation_id = %s and kind = 'user_turn' order by ordinal",
            (invocation_id,),
        )
        invocation["turns"] = [dict(event["payload"]) for event in await turns_result.fetchall()]

    parent_run_key = (
        str(invocation["parent_session_id"])
        if invocation["parent_session_id"] is not None
        else None
    )

    try:
        task = dict(invocation["task"])
        computer_ref = str(invocation["definition_refs"]["computer"]["ref"])
        if invocation["executor_kind"] == "program":
            provider_result = await execute_program(
                settings=settings,
                catalog=catalog,
                workspace=workspace,
                entity=str(invocation["entity"]),
                run_key=str(invocation["session_id"]),
                task_id=str(invocation_id),
                computer_ref=computer_ref,
                program_ref=str(invocation["executor_ref"]),
                input_value=task.get("input"),
                source_ids=frozenset(),
                citation_ids=frozenset(),
                output_path="/outbox/final-result.json",
                max_output_bytes=10_485_760,
                mode="invocation",
                parent_run_key=parent_run_key,
            )
        else:
            try:
                materialized = await build_agent_materialization(
                    pool,
                    workspace=workspace,
                    entity=str(invocation["entity"]),
                    computer_ref=computer_ref,
                    agent_ref=str(invocation["executor_ref"]),
                    catalog=catalog,
                    settings=settings,
                )
            except MaterializationError as exc:
                raise InvocationError(exc.code, exc.detail) from exc
            context_ids = materialized.citation_ids
            agent = catalog.resolve_agent(str(invocation["executor_ref"]))
            provider_result = await execute_agent(
                settings=settings,
                catalog=catalog,
                workspace=workspace,
                entity=str(invocation["entity"]),
                run_key=str(invocation["session_id"]),
                task_id=str(invocation_id),
                computer_ref=computer_ref,
                agent_ref=str(invocation["executor_ref"]),
                context_policy_ref=str(invocation["context_policy_ref"]),
                input_value={
                    "kind": task.get("kind"),
                    "prompt": task.get("prompt"),
                    "turns": invocation["turns"],
                },
                source_ids=context_ids,
                citation_ids=context_ids,
                output_path="/outbox/final-result.json",
                output_schema={"type": "object"},
                context_files=materialized.context_files,
                materialization=materialized.descriptor_json(),
                toolset=materialized.toolset_json(),
                max_output_bytes=agent.limits.max_output_bytes,
                max_steps=agent.limits.max_steps,
                mode="invocation",
                parent_run_key=parent_run_key,
            )
        await _complete_invocation(
            pool,
            workspace,
            invocation_id,
            provider_result,
            catalog=catalog,
            settings=settings,
        )
    except ComputerExecutionError as exc:
        if exc.code == "context_exhausted":
            await _context_exhausted_invocation(pool, workspace, invocation_id, exc.detail)
            raise InvocationError(exc.code, exc.detail, status=409) from exc
        if exc.code in {"transport", "provider"} and not final_attempt:
            await _requeue_interrupted_invocation(
                pool, workspace, invocation_id, error_kind=exc.code
            )
            raise
        await _fail_invocation(pool, workspace, invocation_id, exc.code, exc.detail)
        raise InvocationError(exc.code, exc.detail) from exc
    except InvocationError as exc:
        await _fail_invocation(pool, workspace, invocation_id, exc.code, exc.detail)
        raise
    except Exception as exc:
        if not final_attempt:
            await _requeue_interrupted_invocation(
                pool, workspace, invocation_id, error_kind=type(exc).__name__
            )
            raise
        await _fail_invocation(
            pool, workspace, invocation_id, type(exc).__name__, "execution failed"
        )
        raise


async def _context_exhausted_invocation(
    pool: DatabasePool,
    workspace: str,
    invocation_id: UUID,
    detail: str,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        changed = await conn.execute(
            """
            update invocation
            set status = 'context_exhausted', error_kind = 'context_exhausted', error = %s,
                completed_at = now(), updated_at = now()
            where id = %s and workspace = %s and status <> 'cancelled'
            """,
            (detail[:1_000], invocation_id, workspace),
        )
        if changed.rowcount == 0:
            return
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="context_exhausted",
            payload={"protected_context": True},
        )


async def _requeue_interrupted_invocation(
    pool: DatabasePool,
    workspace: str,
    invocation_id: UUID,
    *,
    error_kind: str,
) -> None:
    """Return a transiently interrupted run to the queue without losing history."""

    async with pool.connection() as conn, conn.transaction():
        changed = await conn.execute(
            "update invocation set status = 'queued', updated_at = now() "
            "where id = %s and workspace = %s and status = 'running' returning id",
            (invocation_id, workspace),
        )
        if await changed.fetchone() is None:
            return
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="execution_interrupted",
            payload={"error_kind": error_kind, "retryable": True},
        )


async def _complete_invocation(
    pool: DatabasePool,
    workspace: str,
    invocation_id: UUID,
    result: ComputerResult,
    *,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> None:
    public_receipt, provider_events, outbox = _provider_journal(result.receipt)
    payload = {
        "value": result.value,
        "citation_ids": [str(value) for value in sorted(result.citation_ids, key=str)],
        "receipt": public_receipt,
        "steps": result.steps,
    }
    async with pool.connection() as conn, conn.transaction():
        current = await conn.execute(
            "select invocation.status, invocation.task, invocation.entity, s.computer_ref "
            "from invocation join computer_session s on s.id = invocation.session_id "
            "where invocation.id = %s and invocation.workspace = %s for update of invocation",
            (invocation_id, workspace),
        )
        row = await current.fetchone()
        if row is None:
            raise InvocationError("not_found", "invocation not found", status=404)
        if row["status"] == "cancelled":
            return
        if result.awaiting_input:
            if outbox:
                raise InvocationError("writeback", "awaiting-input result cannot write back")
            await conn.execute(
                "update invocation set status = 'awaiting_input', result = %s, updated_at = now() "
                "where id = %s",
                (Jsonb(payload), invocation_id),
            )
            for kind, event_payload in provider_events:
                await _append_event_tx(
                    conn,
                    workspace=workspace,
                    invocation_id=invocation_id,
                    kind=kind,
                    payload=event_payload,
                )
            await _append_event_tx(
                conn,
                workspace=workspace,
                invocation_id=invocation_id,
                kind="awaiting_input",
                payload={"value": result.value, "steps": result.steps},
            )
            return
        writeback = await _ingest_outbox_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            entity=str(row["entity"]),
            computer_ref=str(row["computer_ref"]),
            outbox=outbox,
            visible_citations=result.citation_ids,
            catalog=catalog,
            settings=settings,
        )
        await conn.execute(
            """
            update invocation
            set status = 'succeeded', result = %s, completed_at = now(), updated_at = now(),
                error_kind = null, error = null
            where id = %s
            """,
            (Jsonb(payload), invocation_id),
        )
        for kind, event_payload in provider_events:
            await _append_event_tx(
                conn,
                workspace=workspace,
                invocation_id=invocation_id,
                kind=kind,
                payload=event_payload,
            )
        if writeback:
            await _append_event_tx(
                conn,
                workspace=workspace,
                invocation_id=invocation_id,
                kind="writeback_ingested",
                payload=writeback,
            )
        event = await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="completed",
            payload={
                "citation_ids": payload["citation_ids"],
                "receipt": public_receipt,
                "steps": result.steps,
            },
        )
        encoded_value = _canonical_json(result.value).encode()
        output_sha256 = hashlib.sha256(encoded_value).hexdigest()
        await conn.execute(
            """
            insert into invocation_artifact
              (id, workspace, invocation_id, path, sha256, size_bytes, mime_type,
               source_event_id, preserved)
            values (%s, %s, %s, '/outbox/final-result.json', %s, %s,
                    'application/json', %s, true)
            on conflict (invocation_id, path, sha256) do nothing
            """,
            (
                uuid4(),
                workspace,
                invocation_id,
                output_sha256,
                len(encoded_value),
                UUID(event["id"]),
            ),
        )
        await persist_completion_memory_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            event_id=UUID(event["id"]),
            end_ordinal=int(event["ordinal"]),
            task=dict(row.get("task") or {}),
            result=payload,
        )


def _provider_journal(
    receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Turn a bounded provider receipt into replayable first-class events."""

    public = dict(receipt)
    raw_events = public.pop("events", [])
    raw_outbox = public.pop("outbox", [])
    candidates: list[tuple[str, Any]] = []
    for value in public.get("commands", ()):
        candidates.append(("command", value))
    for value in public.get("files", ()):
        candidates.append(("file_change", value))
    if isinstance(raw_events, list):
        for value in raw_events:
            if isinstance(value, Mapping):
                candidates.append((str(value.get("kind", "provider_event")), value.get("payload")))
    if len(candidates) > _MAX_PROVIDER_EVENTS:
        raise InvocationError("provider_receipt", "provider returned too many journal events")
    events: list[tuple[str, dict[str, Any]]] = []
    for kind, value in candidates:
        if not kind or len(kind) > 64 or not isinstance(value, Mapping):
            raise InvocationError("provider_receipt", "provider returned an invalid journal event")
        payload = dict(value)
        if len(_canonical_json(payload).encode()) > _MAX_PROVIDER_EVENT_BYTES:
            raise InvocationError("provider_receipt", "provider journal event exceeds byte limit")
        events.append((kind, payload))
    if len(_canonical_json(public).encode()) > 1_048_576:
        raise InvocationError("provider_receipt", "provider receipt exceeds byte limit")
    if not isinstance(raw_outbox, list) or len(raw_outbox) > 100:
        raise InvocationError("provider_receipt", "provider returned an invalid outbox")
    outbox: list[dict[str, Any]] = []
    for item in raw_outbox:
        if not isinstance(item, Mapping):
            raise InvocationError("provider_receipt", "provider returned an invalid outbox file")
        value = dict(item)
        content = value.get("content")
        if not isinstance(content, str) or len(content.encode()) > 1_048_576:
            raise InvocationError("provider_receipt", "provider outbox file exceeds byte limit")
        encoded = content.encode()
        if (
            value.get("bytes") != len(encoded)
            or value.get("sha256") != hashlib.sha256(encoded).hexdigest()
        ):
            raise InvocationError("provider_receipt", "provider outbox hash or size mismatch")
        outbox.append(value)
    return public, events, outbox


async def _ingest_outbox_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    invocation_id: UUID,
    entity: str,
    computer_ref: str,
    outbox: list[dict[str, Any]],
    visible_citations: frozenset[UUID],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    if not outbox:
        return {}
    from memseek.records import PublicRecordInput, RecordValidationError, insert_records_tx

    computer = catalog.resolve_computer(computer_ref)
    declarations = [
        item for item in computer.writeback if item.type in {"observations", "maintained_state"}
    ]
    records: list[PublicRecordInput] = []
    paths: list[str] = []
    for file in outbox:
        path = file.get("path")
        file_type = file.get("type")
        content = file.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise InvocationError("writeback", "invalid outbox file")
        declaration = next(
            (
                item
                for item in declarations
                if item.type == file_type
                and (path == item.path or path.startswith(f"{item.path}/"))
            ),
            None,
        )
        if declaration is None or declaration.collection is None or declaration.record_type is None:
            raise InvocationError("writeback", f"undeclared outbox file {path!r}")
        try:
            if file_type == "observations":
                documents = [json.loads(line) for line in content.splitlines() if line.strip()]
            else:
                documents = [json.loads(content)]
        except json.JSONDecodeError as exc:
            raise InvocationError("writeback", f"invalid JSON in {path!r}") from exc
        collection, version = split_exact_reference(declaration.collection)
        for index, document in enumerate(documents):
            if not isinstance(document, dict) or set(document) - {
                "text",
                "content",
                "citations",
                "key",
            }:
                raise InvocationError("writeback", f"invalid candidate in {path!r}")
            raw_citations = document.get("citations")
            if not isinstance(raw_citations, list) or not raw_citations:
                raise InvocationError("writeback", "writeback candidates require citations")
            try:
                citations = tuple(UUID(str(value)) for value in raw_citations)
            except (TypeError, ValueError) as exc:
                raise InvocationError("writeback", "writeback citations must be UUIDs") from exc
            if not set(citations) <= visible_citations:
                raise InvocationError("writeback", "writeback widened citation authority")
            records.append(
                PublicRecordInput(
                    entity=entity,
                    collection=collection,
                    collection_version=int(version),
                    type=declaration.record_type,
                    key=document.get("key"),
                    text=document.get("text"),
                    content=document.get("content") or {},
                    status="draft" if declaration.review else "active",
                    dedupe_key=f"invocation:{invocation_id}:{path}:{index}",
                    derived_from=citations,
                )
            )
            paths.append(path)
    if not records:
        return {}
    try:
        result = await insert_records_tx(
            conn,
            workspace=workspace,
            records=tuple(records),
            catalog=catalog,
            settings=settings,
        )
    except RecordValidationError as exc:
        raise InvocationError("writeback", f"invalid writeback record: {exc}") from exc
    return {
        "paths": sorted(set(paths)),
        "inserted_ids": [str(item.id) for item in result.inserted],
        "duplicate_ids": [str(item.id) for item in result.duplicates],
        "draft_count": sum(1 for record in records if record.status == "draft"),
    }


async def _fail_invocation(
    pool: DatabasePool,
    workspace: str,
    invocation_id: UUID,
    error_kind: str,
    detail: str,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        changed = await conn.execute(
            """
            update invocation
            set status = 'failed', error_kind = %s, error = %s,
                completed_at = now(), updated_at = now()
            where id = %s and workspace = %s and status <> 'cancelled'
            """,
            (error_kind, detail[:1_000], invocation_id, workspace),
        )
        if changed.rowcount == 0:
            return
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="failed",
            payload={"error_kind": error_kind},
        )


async def cancel_invocation(
    pool: DatabasePool, *, workspace: str, invocation_id: UUID
) -> dict[str, Any]:
    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            "select status from invocation where id = %s and workspace = %s for update",
            (invocation_id, workspace),
        )
        row = await result.fetchone()
        if row is None:
            raise InvocationError("not_found", "invocation not found", status=404)
        if row["status"] not in TERMINAL_INVOCATION_STATUSES:
            await conn.execute(
                "update invocation set status = 'cancelled', completed_at = now(), "
                "updated_at = now() where id = %s",
                (invocation_id,),
            )
            await _append_event_tx(
                conn,
                workspace=workspace,
                invocation_id=invocation_id,
                kind="cancelled",
                payload={},
            )
        return await _read_invocation_tx(conn, workspace=workspace, invocation_id=invocation_id)


async def continue_invocation(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    turn: InvocationTurn,
) -> dict[str, Any]:
    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            "select status, entity from invocation where id = %s and workspace = %s for update",
            (invocation_id, workspace),
        )
        row = await result.fetchone()
        if row is None:
            raise InvocationError("not_found", "invocation not found", status=404)
        if row["status"] != "awaiting_input":
            raise InvocationError("state", "invocation is not awaiting input", status=409)
        await _append_event_tx(
            conn,
            workspace=workspace,
            invocation_id=invocation_id,
            kind="user_turn",
            payload={"prompt": turn.prompt},
        )
        await conn.execute(
            "update invocation set status = 'queued', updated_at = now() where id = %s",
            (invocation_id,),
        )
        await conn.execute(
            "insert into job (workspace, kind, entity, payload, dedupe_key) "
            "values (%s, 'invocation', %s, %s, %s)",
            (
                workspace,
                row["entity"],
                Jsonb({"invocation_id": str(invocation_id)}),
                f"invocation:{invocation_id}:turn:{uuid4()}",
            ),
        )
        return await _read_invocation_tx(conn, workspace=workspace, invocation_id=invocation_id)


async def list_invocation_artifacts(
    pool: DatabasePool, *, workspace: str, invocation_id: UUID
) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select id, path, sha256, size_bytes, mime_type, preserved, created_at
            from invocation_artifact
            where workspace = %s and invocation_id = %s
            order by path, created_at
            """,
            (workspace, invocation_id),
        )
        rows = await result.fetchall()
    return {
        "artifacts": [
            {
                "id": str(row["id"]),
                "path": row["path"],
                "sha256": row["sha256"],
                "size_bytes": int(row["size_bytes"]),
                "mime_type": row["mime_type"],
                "preserved": bool(row["preserved"]),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


async def read_invocation_artifact(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    artifact_id: UUID,
) -> dict[str, Any]:
    """Return one workspace-scoped preserved artifact descriptor.

    ``storage_uri`` is intentionally not returned from this public boundary. A
    provider-specific object address is an implementation detail and must never
    become an ambient storage credential or cross-workspace lookup handle.
    """

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select artifact.id, artifact.path, artifact.sha256, artifact.size_bytes,
                   artifact.mime_type, artifact.preserved, artifact.storage_uri,
                   artifact.created_at, invocation.result
            from invocation_artifact artifact
            join invocation on invocation.id = artifact.invocation_id
            where artifact.workspace = %s and artifact.invocation_id = %s and artifact.id = %s
            """,
            (workspace, invocation_id, artifact_id),
        )
        row = await result.fetchone()
    if row is None:
        raise InvocationError("not_found", "invocation artifact not found", status=404)
    descriptor = {
        "id": str(row["id"]),
        "path": row["path"],
        "sha256": row["sha256"],
        "size_bytes": int(row["size_bytes"]),
        "mime_type": row["mime_type"],
        "preserved": bool(row["preserved"]),
        "created_at": row["created_at"].isoformat(),
    }
    if row["storage_uri"] is None and row["path"] == "/outbox/final-result.json":
        invocation_result = dict(row["result"] or {})
        descriptor["content"] = invocation_result.get("value")
    return descriptor


__all__ = [
    "TERMINAL_INVOCATION_STATUSES",
    "InvocationCreate",
    "InvocationError",
    "InvocationTurn",
    "cancel_invocation",
    "continue_invocation",
    "create_invocation",
    "execute_invocation",
    "list_invocation_artifacts",
    "read_invocation",
    "read_invocation_artifact",
    "read_invocation_events",
]
