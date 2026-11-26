"""Trace identity for traced Agent runs, and the one span the worker exports.

The worker mints each run's trace and root span ids, hands the harness a W3C
``TRACEPARENT`` naming them, and exports the root span itself once the run has
ended, so the span exists and carries the outcome even when the harness was
killed. The harness exports everything below it. One span per run does not
earn an SDK; this writes OTLP/JSON directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

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


def _attribute(key: str, value: str | int | float) -> dict[str, Any]:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": str(value)}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"doubleValue": value}}


def _nanos(run_time: Any) -> str:
    return str(int(run_time.timestamp() * 1_000_000_000))


def root_span_payload(run: AgentRun) -> dict[str, Any]:
    """The run's root span as an OTLP/JSON export request."""

    agent_name = str(run.agent["ref"]).split("@", 1)[0]
    attributes: dict[str, str | int | float] = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": agent_name,
        "gen_ai.request.model": str(run.agent.get("model", "")),
        "memseek.agent_run.id": str(run.agent_run_id),
        "memseek.workspace": run.workspace,
        "memseek.entity_sha256": hashlib.sha256(run.entity.encode()).hexdigest()[:16],
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
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", "memseek-worker")]},
                "scopeSpans": [{"scope": {"name": "memseek"}, "spans": [span]}],
            }
        ]
    }


async def export_root_span(run: AgentRun, settings: Settings) -> None:
    """Send the root span; a failed export is logged and never fails the run."""

    headers = {"Content-Type": "application/json"}
    if settings.trace_export_token:
        headers["Authorization"] = f"Bearer {settings.trace_export_token}"
    try:
        async with httpx.AsyncClient(timeout=_EXPORT_TIMEOUT_S) as client:
            response = await client.post(
                settings.trace_export_url, json=root_span_payload(run), headers=headers
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log_event(
            _LOGGER,
            "warning",
            "trace.export_failed",
            agent_run_id=str(run.agent_run_id),
            exception_type=type(exc).__name__,
        )


def schedule_root_span_export(run: AgentRun, settings: Settings) -> None:
    """Export without awaiting, so a cancelled run still exports and never waits."""

    if not settings.trace_export_url:
        return
    task = asyncio.get_running_loop().create_task(export_root_span(run, settings))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_exports() -> None:
    """Wait for scheduled exports, for worker shutdown and tests."""

    while _PENDING:
        await asyncio.gather(*tuple(_PENDING), return_exceptions=True)


__all__ = [
    "HarnessTelemetry",
    "drain_exports",
    "export_root_span",
    "new_trace_ids",
    "root_span_payload",
    "schedule_root_span_export",
    "traceparent",
]
