"""Artifact uses: the correlation handle between a render and a later outcome.

An artifact use records that one exact rendering was prepared for external use
and that delayed feedback may name it.  It is deliberately not an invocation:
it does not claim a model ran, and it has nowhere to put a prompt, a response,
a tool call, a token count, or a span.  Execution observability belongs to an
OpenTelemetry backend; this module owns only the identities and hashes needed
to attribute a selected outcome to the maintained knowledge that produced it.

Feedback about a use becomes an ordinary ``learning_signals`` record through the
public record path, so dedupe, schema validation, provenance, and erasure keep
their normal semantics.  Nothing here promotes anything: a signal is evidence,
and a candidate remains draft until an explicit promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memseek.artifacts import persist_artifact_snapshot, resolve_named_artifact
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog

LEARNING_SIGNALS_COLLECTION = "learning_signals"

SIGNAL_KINDS = (
    "thumbs_up",
    "thumbs_down",
    "correction",
    "task_success",
    "task_failure",
    "exception",
    "evaluation",
)
SIGNAL_SOURCES = ("end_user", "operator", "evaluator", "application")

SignalKind = Literal[
    "thumbs_up",
    "thumbs_down",
    "correction",
    "task_success",
    "task_failure",
    "exception",
    "evaluation",
]
SignalSource = Literal["end_user", "operator", "evaluator", "application"]

_MAX_SIGNAL_TEXT_CHARS = 4_096
_MAX_DEDUPE_KEY_CHARS = 256
_DEDUPE_PREFIX = "feedback:"


class ArtifactUseNotFound(Exception):
    """No live artifact use with that identity exists in this workspace."""

    def __init__(self, detail: str) -> None:
        self.code = "artifact_use_not_found"
        self.detail = detail
        self.status = 404
        super().__init__(detail)


class ArtifactUseError(ValueError):
    """An artifact-use or feedback request is invalid or no longer accepted."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


class ArtifactUseRequest(BaseModel):
    """Bind one artifact render for external use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameters: dict[str, Any] = Field(default_factory=dict)
    snapshot: bool = False


class ExecutionRef(BaseModel):
    """An informational pointer into an external execution or trace backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=256)
    url: str | None = Field(default=None, max_length=1_024)


class FeedbackRequest(BaseModel):
    """One selected outcome worth changing future knowledge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SignalKind
    source: SignalSource
    score: float | None = Field(default=None, ge=0, le=1)
    label: str | None = Field(default=None, min_length=1, max_length=128)
    comment: str | None = Field(default=None, min_length=1)
    expected: str | None = Field(default=None, min_length=1)
    actual_excerpt: str | None = Field(default=None, min_length=1)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)
    execution_refs: tuple[ExecutionRef, ...] = ()

    @field_validator("comment", "expected", "actual_excerpt")
    @classmethod
    def strip_evidence(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def validate_refs(self) -> FeedbackRequest:
        if len(self.execution_refs) > 8:
            raise ValueError("execution_refs cannot exceed 8 entries")
        return self


@dataclass(frozen=True, slots=True)
class ArtifactUse:
    """One persisted correlation handle."""

    id: UUID
    workspace: str
    artifact_name: str
    artifact_version: int
    definition_hash: str
    render_sha256: str
    learning_target: dict[str, Any] | None
    snapshot_id: UUID | None
    created_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)

    def artifact_identity(self) -> dict[str, Any]:
        return {
            "name": self.artifact_name,
            "version": self.artifact_version,
            "definition_hash": self.definition_hash,
        }

    def as_json(self) -> dict[str, Any]:
        """The metadata read surface; never a render, a trace, or user content."""

        return {
            "id": str(self.id),
            "artifact": self.artifact_identity(),
            "render_sha256": self.render_sha256,
            "learning_target": self.learning_target,
            "snapshot_id": None if self.snapshot_id is None else str(self.snapshot_id),
            "telemetry": telemetry_attributes(self),
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "expired": self.expired,
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def telemetry_attributes(use: ArtifactUse) -> dict[str, str | int]:
    """The reserved, backend-neutral OpenTelemetry attributes for one use.

    Every value is a bounded scalar identity or hash.  No prompt text, record
    content, model output, customer identifier, or input-record list is ever
    included, so these attributes are safe on a root span in any backend.  The
    snapshot attribute is omitted rather than sent as null when absent.
    """

    attributes: dict[str, str | int] = {
        "memseek.use.id": str(use.id),
        "memseek.artifact.name": use.artifact_name,
        "memseek.artifact.version": use.artifact_version,
        "memseek.artifact.definition_hash": use.definition_hash,
        "memseek.artifact.render_sha256": use.render_sha256,
    }
    if use.snapshot_id is not None:
        attributes["memseek.artifact.snapshot_id"] = str(use.snapshot_id)
    return attributes


def _use_from_row(row: dict[str, Any]) -> ArtifactUse:
    return ArtifactUse(
        id=row["id"],
        workspace=row["workspace"],
        artifact_name=row["artifact_name"],
        artifact_version=int(row["artifact_version"]),
        definition_hash=row["definition_hash"],
        render_sha256=row["render_sha256"],
        learning_target=row["learning_target"],
        snapshot_id=row["snapshot_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


_USE_COLUMNS = """
    id, workspace, artifact_name, artifact_version, definition_hash, render_sha256,
    learning_target, snapshot_id, created_at, expires_at
"""


async def bind_artifact_use(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    request: ArtifactUseRequest,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Render one artifact, resolve its learning target, and register the use.

    The rendered content is returned to the caller but never persisted on the
    use row.  When a snapshot is requested it is persisted from the same
    resolution, so the snapshot record and the use name one identical
    rendered-content hash.
    """

    resolution = await resolve_named_artifact(
        pool,
        workspace=workspace,
        name=name,
        parameters=request.parameters,
        catalog=catalog,
        settings=settings,
        require_snapshot=request.snapshot,
    )
    snapshot: dict[str, Any] | None = None
    if request.snapshot:
        snapshot = await persist_artifact_snapshot(
            pool,
            workspace=workspace,
            resolution=resolution,
            catalog=catalog,
            settings=settings,
        )
    artifact = resolution.artifact
    expires_at = datetime.now(UTC) + timedelta(days=settings.artifact_use_retention_days)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            insert into artifact_use (
              workspace, artifact_name, artifact_version, definition_hash,
              render_sha256, learning_target, snapshot_id, expires_at
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning {_USE_COLUMNS}
            """,
            (
                workspace,
                artifact.name,
                artifact.version,
                artifact.definition_hash,
                resolution.rendered_sha256,
                None if resolution.learning_target is None else Jsonb(resolution.learning_target),
                None if snapshot is None else UUID(snapshot["record_id"]),
                expires_at,
            ),
        )
        row = await result.fetchone()
    if row is None:
        raise ArtifactUseError(
            "artifact_use", "artifact use registration returned no row", status=500
        )
    use = _use_from_row(row)
    payload = use.as_json()
    payload["content"] = resolution.rendered
    payload["render"] = {
        "tokens": resolution.tokens,
        "truncated": resolution.truncated,
    }
    return payload


async def read_artifact_use(
    pool: DatabasePool,
    *,
    workspace: str,
    use_id: UUID,
) -> dict[str, Any]:
    """Read one use's metadata for debugging and support."""

    return (await _load_use(pool, workspace=workspace, use_id=use_id)).as_json()


async def _load_use(
    pool: DatabasePool,
    *,
    workspace: str,
    use_id: UUID,
) -> ArtifactUse:
    async with pool.connection() as conn:
        result = await conn.execute(
            f"select {_USE_COLUMNS} from artifact_use where workspace = %s and id = %s",
            (workspace, use_id),
        )
        row = await result.fetchone()
    if row is None:
        raise ArtifactUseNotFound(f"unknown artifact use {use_id}")
    return _use_from_row(row)


def _signal_text(use: ArtifactUse, request: FeedbackRequest) -> str:
    """A bounded, deterministic projection a candidate derivation can read."""

    header = f"{request.kind} from {request.source} on {use.artifact_name}@{use.artifact_version}"
    if request.label is not None:
        header = f"{header} [{request.label}]"
    if request.score is not None:
        header = f"{header} score={request.score:g}"
    lines = [header]
    if request.comment is not None:
        lines.append(f"comment: {request.comment}")
    if request.expected is not None:
        lines.append(f"expected: {request.expected}")
    if request.actual_excerpt is not None:
        lines.append(f"actual: {request.actual_excerpt}")
    return "\n".join(lines)[:_MAX_SIGNAL_TEXT_CHARS]


def _signal_entity(use: ArtifactUse) -> str:
    """Route the signal to the artifact whose knowledge should improve."""

    target = use.learning_target
    if isinstance(target, dict):
        artifact = target.get("artifact")
        if isinstance(artifact, dict) and isinstance(artifact.get("name"), str):
            return f"artifact:{artifact['name']}"
    return f"artifact:{use.artifact_name}"


def _bounded(value: str | None, limit: int) -> str | None:
    return None if value is None else value[:limit]


def _signal_content(
    use: ArtifactUse, request: FeedbackRequest, settings: Settings
) -> dict[str, Any]:
    signal: dict[str, Any] = {"kind": request.kind, "source": request.source}
    if request.label is not None:
        signal["label"] = request.label
    if request.score is not None:
        signal["score"] = request.score
    evidence: dict[str, Any] = {}
    comment = _bounded(request.comment, settings.max_feedback_comment_chars)
    expected = _bounded(request.expected, settings.max_feedback_evidence_chars)
    actual = _bounded(request.actual_excerpt, settings.max_feedback_evidence_chars)
    if comment is not None:
        evidence["comment"] = comment
    if expected is not None:
        evidence["expected"] = expected
    if actual is not None:
        evidence["actual_excerpt"] = actual
    content: dict[str, Any] = {
        "artifact_use": {
            "id": str(use.id),
            "artifact": use.artifact_identity(),
            "render_sha256": use.render_sha256,
            "snapshot_id": None if use.snapshot_id is None else str(use.snapshot_id),
            "learning_target": use.learning_target,
        },
        "signal": signal,
    }
    if evidence:
        content["evidence"] = evidence
    if request.execution_refs:
        content["execution_refs"] = [
            {key: value for key, value in ref.model_dump().items() if value is not None}
            for ref in request.execution_refs
        ]
    return content


async def submit_feedback(
    pool: DatabasePool,
    *,
    workspace: str,
    use_id: UUID,
    request: FeedbackRequest,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Turn one selected outcome into an ordinary ``learning_signals`` record.

    Provenance depends on whether an exact snapshot exists.  With a snapshot the
    signal cites it, so ordinary erasure closure reaches the signal and any
    candidate derived from it.  Without one the signal carries identities and
    hashes as metadata only: the render cannot be reconstructed after its source
    records change, and the system must not pretend otherwise.
    """

    from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

    use = await _load_use(pool, workspace=workspace, use_id=use_id)
    if use.expired:
        raise ArtifactUseError(
            "artifact_use_expired",
            f"artifact use {use_id} expired at {_iso(use.expires_at)} and cannot receive feedback",
            status=410,
        )
    try:
        catalog.resolve_collection(LEARNING_SIGNALS_COLLECTION)
    except (KeyError, ValueError) as exc:
        raise ArtifactUseError(
            "learning_signals_unavailable",
            f"workspace catalog defines no {LEARNING_SIGNALS_COLLECTION!r} collection",
        ) from exc

    dedupe_key = (
        None
        if request.dedupe_key is None
        else f"{_DEDUPE_PREFIX}{use.id}:{request.dedupe_key}"[:_MAX_DEDUPE_KEY_CHARS]
    )
    record = PublicRecordInput(
        entity=_signal_entity(use),
        collection=LEARNING_SIGNALS_COLLECTION,
        type=request.kind,
        text=_signal_text(use, request),
        content=_signal_content(use, request, settings),
        dedupe_key=dedupe_key,
        derived_from=() if use.snapshot_id is None else (use.snapshot_id,),
    )
    result = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(records=(record,)),
        catalog=catalog,
        settings=settings,
    )
    write = (result.inserted or result.duplicates)[0]
    return {
        "record_id": str(write.id),
        "ready": write.ready,
        "duplicate": not result.inserted,
        "collection": LEARNING_SIGNALS_COLLECTION,
        "entity": record.entity,
        "type": request.kind,
        "artifact_use": use.as_json(),
    }


async def purge_expired_artifact_uses(
    pool: DatabasePool,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Delete one bounded page of expired correlation handles.

    Only operational metadata is removed.  Learning signals and artifact
    snapshots are canonical records and follow their own retention and erasure
    rules, so an expired use never takes durable history with it.
    """

    current = now or datetime.now(UTC)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            delete from artifact_use
            where id in (
              select id from artifact_use
              where expires_at <= %s
              order by expires_at
              limit %s
            )
            returning id
            """,
            (current, settings.artifact_use_purge_batch),
        )
        return len(await result.fetchall())


__all__ = [
    "LEARNING_SIGNALS_COLLECTION",
    "SIGNAL_KINDS",
    "SIGNAL_SOURCES",
    "ArtifactUse",
    "ArtifactUseError",
    "ArtifactUseNotFound",
    "ArtifactUseRequest",
    "ExecutionRef",
    "FeedbackRequest",
    "bind_artifact_use",
    "purge_expired_artifact_uses",
    "read_artifact_use",
    "submit_feedback",
    "telemetry_attributes",
]
