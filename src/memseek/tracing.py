"""Spans the worker exports: traced Agent runs and derivation jobs.

For a traced Agent run the worker mints the trace and root span ids, hands the
harness a W3C ``TRACEPARENT`` naming them, and exports the root span itself
once the run has ended, so the span exists and carries the outcome even when
the harness was killed. The harness exports everything below it.

Every derivation job becomes a trace too, built from its run audit once the
audit is written, so the trace can never disagree with what Memseek recorded.
Its trace id is the run id, which lets a later job link to the jobs whose
records it read without storing anything new. Spans carry identities and
timings only, never content.

A handful of spans per job does not earn an SDK; this writes OTLP/JSON directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from memseek.agent_runs import AgentRun
from memseek.config import Settings
from memseek.logging import log_event

_EXPORT_TIMEOUT_S = 5.0
_LOGGER = logging.getLogger(__name__)
_PENDING: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class HarnessTelemetry:
    """What a tracing harness needs to export under the run's root span."""

    traceparent: str
    endpoint: str
    token: str
    content: str

    def environment(self) -> dict[str, str]:
        env = {
            "TRACEPARENT": self.traceparent,
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": self.endpoint,
            "MEMSEEK_TRACE_CONTENT": self.content,
        }
        if self.token:
            env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = f"authorization=Bearer {self.token}"
        return env


def new_trace_ids() -> tuple[str, str]:
    """A fresh trace id and root span id, lowercase hex as OTLP/JSON encodes them."""

    return secrets.token_hex(16), secrets.token_hex(8)


def traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def derive_trace_ids(run_id: UUID) -> tuple[str, str]:
    """A derivation run's trace id and root span id, both read off its run id."""

    return run_id.hex, run_id.hex[16:]


def _attribute(key: str, value: str | int | float) -> dict[str, Any]:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": str(value)}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"doubleValue": value}}


def _nanos(run_time: Any) -> str:
    return str(int(run_time.timestamp() * 1_000_000_000))


def _entity_hash(entity: str) -> str:
    return hashlib.sha256(entity.encode()).hexdigest()[:16]


def _link(trace_id: str, span_id: str, kind: str) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "attributes": [_attribute("memseek.link", kind)],
    }


def _request(service: str, spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", service)]},
                "scopeSpans": [{"scope": {"name": "memseek"}, "spans": list(spans)}],
            }
        ]
    }


def root_span_payload(run: AgentRun) -> dict[str, Any]:
    """The run's root span as an OTLP/JSON export request."""

    agent_name = str(run.agent["ref"]).split("@", 1)[0]
    attributes: dict[str, str | int | float] = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": agent_name,
        "gen_ai.request.model": str(run.agent.get("model", "")),
        "memseek.agent_run.id": str(run.agent_run_id),
        "memseek.workspace": run.workspace,
        "memseek.entity_sha256": _entity_hash(run.entity),
        "memseek.mode": run.mode,
        "memseek.agent.ref": str(run.agent["ref"]),
        "memseek.agent.hash": str(run.agent.get("hash", "")),
    }
    for key in ("derivation", "config_hash", "task_id", "invocation_id"):
        if run.origin.get(key) is not None:
            attributes[f"memseek.origin.{key}"] = str(run.origin[key])
    if run.error_kind is not None:
        attributes["error.type"] = run.error_kind
    span: dict[str, Any] = {
        "traceId": run.trace_id,
        "spanId": run.root_span_id,
        "name": f"invoke_agent {agent_name}",
        "kind": 1,
        "startTimeUnixNano": _nanos(run.started_at),
        "endTimeUnixNano": _nanos(run.ended_at),
        "attributes": [_attribute(key, value) for key, value in attributes.items()],
        "status": {"code": 1} if run.status == "ok" else {"code": 2, "message": run.error_kind},
    }
    if run.parent is not None:
        span["links"] = [_link(*run.parent, "derivation")]
    return _request("memseek-worker", [span])


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_spans_payload(
    audit: Mapping[str, Any],
    *,
    run_id: UUID,
    workspace: str,
    entity: str,
    agent_runs: Iterable[AgentRun] = (),
    upstream_run_ids: Iterable[UUID] = (),
) -> dict[str, Any] | None:
    """One derivation run as a trace: the job's span with a span per Task.

    Links, not parents, join it to other traces: the Agent runs its Tasks
    started, the traces a ``trace`` Task read, and the runs that wrote the
    records it read. Each of those stays a whole trace of its own.
    """

    started = _parse_time(audit.get("started_at"))
    completed = _parse_time(audit.get("completed_at"))
    if started is None or completed is None:
        return None
    trace_id, root_id = derive_trace_ids(run_id)
    derivation = str(audit.get("processor", ""))
    status = str(audit.get("status", ""))
    usage = audit.get("usage") or {}
    basis = audit.get("basis") or {}
    attributes: dict[str, str | int | float] = {
        "memseek.run.id": str(run_id),
        "memseek.run.status": status,
        "memseek.derivation": derivation,
        "memseek.derivation.config_hash": str(audit.get("config_hash", "")),
        "memseek.workspace": workspace,
        "memseek.entity_sha256": _entity_hash(entity),
        "memseek.trigger.reasons": ",".join(audit.get("trigger_reasons") or ()),
        "memseek.input.count": len(basis.get("input_ids") or ()),
        "memseek.output.count": len(audit.get("output_ids") or ()),
        "memseek.model_calls": len(audit.get("model_calls") or ()),
        "gen_ai.usage.input_tokens": int(usage.get("prompt_tokens") or 0),
        "gen_ai.usage.output_tokens": int(usage.get("completion_tokens") or 0),
    }
    if audit.get("job_id"):
        attributes["memseek.job.id"] = str(audit["job_id"])
    if usage.get("judgments"):
        attributes["memseek.judgments"] = int(usage["judgments"])
    if audit.get("trace_reads"):
        attributes["memseek.trace_reads"] = len(audit["trace_reads"])
    if audit.get("error_kind"):
        attributes["error.type"] = str(audit["error_kind"])
    root: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": root_id,
        "name": f"derive {derivation}",
        "kind": 1,
        "startTimeUnixNano": _nanos(started),
        "endTimeUnixNano": _nanos(completed),
        "attributes": [_attribute(key, value) for key, value in attributes.items()],
        "status": (
            {"code": 2, "message": str(audit["error_kind"])} if status == "failed" else {"code": 1}
        ),
    }
    upstream = sorted({item for item in upstream_run_ids if item != run_id}, key=str)
    if upstream:
        root["links"] = [_link(*derive_trace_ids(item), "input") for item in upstream[:32]]

    agent_links: dict[str, list[dict[str, Any]]] = {}
    for run in agent_runs:
        task_id = str(run.origin.get("task_id", ""))
        agent_links.setdefault(task_id, []).append(
            _link(run.trace_id, run.root_span_id, "agent_run")
        )
    read_links: dict[str, list[dict[str, Any]]] = {}
    for receipt in audit.get("trace_reads") or ():
        if receipt.get("outcome") == "read" and receipt.get("root_span_id"):
            read_links.setdefault(str(receipt.get("task", "")), []).append(
                _link(str(receipt["trace_id"]), str(receipt["root_span_id"]), "trace_read")
            )

    spans = [root]
    offset = timedelta()
    for entry in audit.get("task_trace") or ():
        task = str(entry.get("task", ""))
        took = timedelta(milliseconds=int(entry.get("ms") or 0))
        if isinstance(entry.get("start_ms"), int):
            offset = timedelta(milliseconds=entry["start_ms"])
        begin = started + offset
        span: dict[str, Any] = {
            "traceId": trace_id,
            "spanId": hashlib.sha256(f"{run_id}:{task}".encode()).hexdigest()[:16],
            "parentSpanId": root_id,
            "name": f"task {task}",
            "kind": 1,
            "startTimeUnixNano": _nanos(begin),
            "endTimeUnixNano": _nanos(begin + took),
            "attributes": [
                _attribute("memseek.task.id", task),
                _attribute("memseek.task.use", str(entry.get("use", ""))),
                _attribute("memseek.task.sources", len(entry.get("source_ids") or ())),
                _attribute("memseek.task.citations", len(entry.get("citation_ids") or ())),
                _attribute("memseek.task.model_calls", int(entry.get("model_calls") or 0)),
            ],
            "status": {"code": 1},
        }
        links = [*agent_links.get(task, ()), *read_links.get(task, ())]
        if links:
            span["links"] = links
        spans.append(span)
        offset += took
    return _request("memseek-worker", spans)


async def _export(payload: Mapping[str, Any], settings: Settings, **fields: str) -> None:
    """Send one OTLP/JSON request; a failed export is logged and never fails the work."""

    headers = {"Content-Type": "application/json"}
    if settings.trace_export_token:
        headers["Authorization"] = f"Bearer {settings.trace_export_token}"
    try:
        async with httpx.AsyncClient(timeout=_EXPORT_TIMEOUT_S) as client:
            response = await client.post(settings.trace_export_url, json=payload, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log_event(
            _LOGGER, "warning", "trace.export_failed", exception_type=type(exc).__name__, **fields
        )


def _schedule(payload: Mapping[str, Any], settings: Settings, **fields: str) -> None:
    task = asyncio.get_running_loop().create_task(_export(payload, settings, **fields))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def export_root_span(run: AgentRun, settings: Settings) -> None:
    """Send an Agent run's root span now."""

    await _export(root_span_payload(run), settings, agent_run_id=str(run.agent_run_id))


def schedule_root_span_export(run: AgentRun, settings: Settings) -> None:
    """Export without awaiting, so a cancelled run still exports and never waits."""

    if settings.trace_export_url:
        _schedule(root_span_payload(run), settings, agent_run_id=str(run.agent_run_id))


def schedule_derive_export(
    audit: Mapping[str, Any],
    settings: Settings,
    *,
    run_id: UUID,
    workspace: str,
    entity: str,
    agent_runs: Iterable[AgentRun] = (),
    upstream_run_ids: Iterable[UUID] = (),
) -> None:
    """Export a derivation run's spans once its audit is written, without waiting."""

    if not settings.trace_export_url:
        return
    payload = derive_spans_payload(
        audit,
        run_id=run_id,
        workspace=workspace,
        entity=entity,
        agent_runs=agent_runs,
        upstream_run_ids=upstream_run_ids,
    )
    if payload is not None:
        _schedule(payload, settings, run_id=str(run_id))


async def drain_exports() -> None:
    """Wait for scheduled exports, for worker shutdown and tests."""

    while _PENDING:
        await asyncio.gather(*tuple(_PENDING), return_exceptions=True)


__all__ = [
    "HarnessTelemetry",
    "derive_spans_payload",
    "derive_trace_ids",
    "drain_exports",
    "export_root_span",
    "new_trace_ids",
    "root_span_payload",
    "schedule_derive_export",
    "schedule_root_span_export",
    "traceparent",
]
