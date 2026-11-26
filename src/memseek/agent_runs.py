"""One opted-in Agent execution, as the runtime observed it.

An ``AgentRun`` is built once, by ``execute_agent``, whichever way the run
ends. Its trace lives in the deployment's trace store; this value carries only
what correlates the run with that trace and says how the run ended.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from memseek.canonical_records import CanonicalRecordWrite
from memseek.materialization import MANIFEST_PATH

if TYPE_CHECKING:
    from memseek.definitions.models import CollectionDefinition

RunStatus = Literal["ok", "failed"]
TraceContent = Literal["full", "metadata"]

AGENT_RUNS_COLLECTION = "agent_runs"
AGENT_RUNS_REF = f"{AGENT_RUNS_COLLECTION}@1"
AGENT_RUNS_SEARCH_PROFILE = "pg_default"
RUN_TYPES: Mapping[RunStatus, str] = {"ok": "run_ok", "failed": "run_failed"}
_NAME = "^[a-z][a-z0-9_]{0,63}$"
_TEXT = {"type": ["string", "null"], "maxLength": 512}
_STRING = {"type": "string", "maxLength": 512}


@dataclass(frozen=True, slots=True)
class AgentRun:
    agent_run_id: UUID
    trace_id: str
    root_span_id: str
    workspace: str
    entity: str
    mode: Literal["derivation", "invocation"]
    origin: Mapping[str, Any]
    agent: Mapping[str, Any]
    skills: tuple[Mapping[str, Any], ...]
    content: TraceContent
    started_at: datetime
    ended_at: datetime
    status: RunStatus
    error_kind: str | None = None
    metrics: Mapping[str, Any] | None = None
    # The derivation run's trace and span, linked from this run's root span.
    # Not part of the run record.
    parent: tuple[str, str] | None = None

    def ended_as(self, error_kind: str) -> AgentRun:
        """The same run, failed with the kind its caller settled on."""

        return replace(self, status="failed", error_kind=error_kind)


def mounted_skills(
    materialization: Mapping[str, Any] | None, context_files: Mapping[str, str]
) -> tuple[Mapping[str, Any], ...]:
    """The skills mounted for a run, with the artifact identity each rendered from."""

    if materialization is None:
        return ()
    manifests: dict[str, Mapping[str, Any]] = {}
    if MANIFEST_PATH in context_files:
        try:
            sources = json.loads(context_files[MANIFEST_PATH]).get("sources", ())
        except ValueError, AttributeError:
            sources = ()
        manifests = {
            item["path"]: item for item in sources if isinstance(item, Mapping) and "path" in item
        }
    skills: list[Mapping[str, Any]] = []
    for item in materialization.get("files", ()):
        if item.get("role") != "skill":
            continue
        path = item["path"]
        artifact = manifests.get(path, {}).get("artifact", {})
        body = context_files.get(path, "")
        skills.append(
            {
                "name": item["skill"]["name"],
                "artifact": artifact.get("name"),
                "version": artifact.get("version"),
                "hash": artifact.get("hash"),
                "rendered_sha256": manifests.get(path, {}).get("rendered_sha256")
                or hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
        )
    return tuple(skills)


def collection_document(search_profile: str = AGENT_RUNS_SEARCH_PROFILE) -> dict[str, Any]:
    """The built-in ``agent_runs`` collection of every catalog with a traced Agent.

    The runtime writes every row; the public record path refuses this
    collection. No processor is required, so a row is ready when written.
    """

    return {
        "name": AGENT_RUNS_COLLECTION,
        "version": 1,
        "active": True,
        "mode": "event",
        "schema": {
            "type": "object",
            "required": [
                "text",
                "agent_run_id",
                "status",
                "agent",
                "origin",
                "started_at",
                "ended_at",
            ],
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 512},
                "agent_run_id": {"type": "string", "format": "uuid"},
                "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "status": {"type": "string", "enum": ["ok", "failed"]},
                "error_kind": {"type": "string", "pattern": _NAME},
                "agent": {
                    "type": "object",
                    "required": ["ref", "hash", "model"],
                    "properties": {
                        "ref": {"type": "string", "maxLength": 128},
                        "hash": {"type": "string", "maxLength": 128},
                        "harness": _TEXT,
                        "model": {"type": "string", "maxLength": 128},
                    },
                    "additionalProperties": False,
                },
                "origin": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["derivation", "invocation"]},
                        "derivation": _STRING,
                        "config_hash": _STRING,
                        "job_id": _STRING,
                        "task_id": _STRING,
                        "invocation_id": _STRING,
                        "session_id": _STRING,
                    },
                    "additionalProperties": False,
                },
                "skills": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "maxLength": 128},
                            "artifact": _TEXT,
                            "version": {"type": ["integer", "null"]},
                            "hash": _TEXT,
                            "rendered_sha256": _TEXT,
                        },
                        "additionalProperties": False,
                    },
                },
                "metrics": {
                    "type": "object",
                    "additionalProperties": {"type": ["number", "null"]},
                    "maxProperties": 32,
                },
                "started_at": {"type": "string", "format": "date-time"},
                "ended_at": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": False,
        },
        "fields": {
            "agent": {
                "path": "content.agent.ref",
                "type": "string",
                "filter": True,
                "project": True,
            },
            "origin": {
                "path": "content.origin.kind",
                "type": "string",
                "filter": True,
                "project": True,
            },
            "derivation": {
                "path": "content.origin.derivation",
                "type": "string",
                "filter": True,
                "project": True,
            },
            "error_kind": {
                "path": "content.error_kind",
                "type": "string",
                "filter": True,
                "project": True,
            },
        },
        "required_processors": [],
        "search_profile": search_profile,
    }


def run_text(run: AgentRun) -> str:
    """One deterministic line saying which Agent ran, how it ended, and for whom."""

    agent = str(run.agent["ref"]).split("@", 1)[0]
    outcome = "succeeded" if run.status == "ok" else f"failed ({run.error_kind})"
    seconds = max(0, round((run.ended_at - run.started_at).total_seconds()))
    origin = run.origin.get("derivation") or run.origin.get("invocation_id") or run.mode
    where = f"derivation {origin}" if run.mode == "derivation" else f"invocation {origin}"
    return f"Agent {agent} run {outcome} after {seconds} s in {where}"[:512]


def run_content(run: AgentRun, *, readable_trace: bool = True) -> dict[str, Any]:
    content: dict[str, Any] = {
        "text": run_text(run),
        "agent_run_id": str(run.agent_run_id),
        "status": run.status,
        "agent": {key: run.agent.get(key) for key in ("ref", "hash", "harness", "model")},
        "origin": {key: value for key, value in run.origin.items() if value is not None},
        "skills": [dict(skill) for skill in run.skills[:64]],
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat(),
    }
    if readable_trace:
        content["trace_id"] = run.trace_id
    if run.error_kind is not None:
        content["error_kind"] = run.error_kind
    if run.metrics:
        content["metrics"] = {
            key: value
            for key, value in list(run.metrics.items())[:32]
            if value is None or (isinstance(value, int | float) and not isinstance(value, bool))
        }
    return content


def run_record(
    run: AgentRun,
    collection: CollectionDefinition,
    *,
    run_id: UUID | None = None,
    derived_from: tuple[UUID, ...] = (),
    readable_trace: bool = True,
) -> CanonicalRecordWrite:
    """The run's record, at depth 0.

    Its edge to the run that hosted it is a control edge, like a run's edge to
    its predecessor: it keeps erasure reaching the record but adds no
    abstraction level, so evidence learned from it can feed the next run's
    skills without the learning loop exhausting MAX_DERIVATION_DEPTH.

    Without ``readable_trace`` the record names no trace, so nothing can be
    read from a trace whose sources its provenance does not reach.
    """

    return CanonicalRecordWrite(
        workspace=run.workspace,
        collection=collection.name,
        collection_version=collection.version,
        collection_hash=collection.contract_hash,
        entity=run.entity,
        type=RUN_TYPES[run.status],
        content=run_content(run, readable_trace=readable_trace),
        ready=True,
        run_id=run_id,
        depth=0,
        derived_from=derived_from,
        dedupe_key=f"agent_run:{run.agent_run_id}",
        occurred_at=run.ended_at,
    )


__all__ = [
    "AGENT_RUNS_COLLECTION",
    "AGENT_RUNS_REF",
    "AGENT_RUNS_SEARCH_PROFILE",
    "RUN_TYPES",
    "AgentRun",
    "RunStatus",
    "TraceContent",
    "collection_document",
    "mounted_skills",
    "run_content",
    "run_record",
    "run_text",
]
