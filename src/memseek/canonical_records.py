"""Single canonical boundary for inserting immutable record rows.

Callers remain responsible for semantic preparation and lock ordering.  This
module owns the storage-shaped invariants that every public and internal row
shares, together with the sole production ``INSERT INTO record`` statement.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Never
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from memseek.config import Settings
from memseek.db import DatabaseConnection

_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SYSTEM_COLLECTION = "_system"
_SYSTEM_TYPES = frozenset({"erasure", "run"})

type RecordStatus = Literal["active", "draft"]
type DedupeConflictMode = Literal["error", "return_none"]


class CanonicalRecordInvariantError(ValueError):
    """A record row violates an invariant shared by every insertion path."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class CanonicalRecordWrite:
    """Complete immutable input to the canonical record insert boundary."""

    workspace: str
    collection: str
    collection_version: int
    collection_hash: str
    entity: str
    type: str
    content: Mapping[str, Any]
    id: UUID = field(default_factory=uuid4)
    key: str | None = None
    status: RecordStatus = "active"
    scores: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)
    annotation_meta: Mapping[str, Any] = field(default_factory=dict)
    enrichment_meta: Mapping[str, Any] = field(default_factory=dict)
    enrichment_error: str | None = None
    ready: bool = False
    run_id: UUID | None = None
    depth: int = 0
    derived_from: tuple[UUID, ...] = ()
    dedupe_key: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CanonicalRecordInsert:
    """Database identity proven by the canonical insert's RETURNING clause."""

    id: UUID
    seq: int
    ready: bool


def _fail(code: str, detail: str) -> Never:
    raise CanonicalRecordInvariantError(code, detail)


def _finite_json(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("finite_json", f"{label} must be finite JSON: {exc}")


def _finite_object(value: Mapping[str, Any], *, label: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        _fail("json_object", f"{label} must be a JSON object")
    normalized = dict(value)
    return normalized, _finite_json(normalized, label=label)


def _validate_annotation_entries(
    value: Mapping[str, Any], *, label: str, settings: Settings
) -> None:
    for name, entry in value.items():
        if len(_finite_json(entry, label=f"{label}.{name}")) > settings.max_annotation_bytes:
            _fail(
                "annotation_too_large",
                f"{label} entry {name!r} exceeds MAX_ANNOTATION_BYTES",
            )


def _validate_namespace(record: CanonicalRecordWrite) -> None:
    if not isinstance(record.collection, str) or not isinstance(record.type, str):
        _fail("name", "collection and type must be strings")
    if record.collection == _SYSTEM_COLLECTION:
        if record.type not in _SYSTEM_TYPES:
            _fail(
                "system_type",
                f"system records must use one of {', '.join(sorted(_SYSTEM_TYPES))}",
            )
        if record.key is not None:
            _fail("system_key", "system records may not have a key")
        return
    if record.collection.startswith("_"):
        _fail("reserved_collection", "only the _system internal collection is supported")
    if not _NAME_RE.fullmatch(record.collection):
        _fail("name", f"collection must match {_NAME_RE.pattern}")
    if not _NAME_RE.fullmatch(record.type):
        _fail("name", f"type must match {_NAME_RE.pattern}")
    if record.type in _SYSTEM_TYPES:
        _fail("reserved_type", f"type {record.type!r} is reserved for _system records")


def _validate_write(record: CanonicalRecordWrite, settings: Settings) -> dict[str, Any]:
    if not isinstance(record.id, UUID):
        _fail("record_id", "record id must be a UUID")
    if not isinstance(record.workspace, str) or not record.workspace:
        _fail("workspace", "workspace must be non-empty")
    _validate_namespace(record)
    if (
        isinstance(record.collection_version, bool)
        or not isinstance(record.collection_version, int)
        or record.collection_version < 1
    ):
        _fail("collection_version", "collection_version must be a positive integer")
    if not isinstance(record.collection_hash, str) or not _HASH_RE.fullmatch(
        record.collection_hash
    ):
        _fail("collection_hash", "collection_hash must be a lowercase SHA-256 digest")
    if not isinstance(record.status, str) or record.status not in {"active", "draft"}:
        _fail("status", "status must be active or draft")
    if (
        not isinstance(record.entity, str)
        or not record.entity
        or record.entity == "*"
        or len(record.entity) > 255
    ):
        _fail("entity", "entity must be non-empty, other than '*', and at most 255 characters")
    if record.key is not None and (not isinstance(record.key, str) or len(record.key) > 128):
        _fail("key", "key must be at most 128 characters")
    if record.dedupe_key is not None and (
        not isinstance(record.dedupe_key, str) or len(record.dedupe_key) > 256
    ):
        _fail("dedupe_key", "dedupe_key must be at most 256 characters")
    if record.run_id is not None and not isinstance(record.run_id, UUID):
        _fail("run_id", "run_id must be a UUID")
    if record.enrichment_error is not None and not isinstance(record.enrichment_error, str):
        _fail("enrichment_error", "enrichment_error must be a string")
    if not isinstance(record.ready, bool):
        _fail("ready", "ready must be a boolean")
    if isinstance(record.depth, bool) or not isinstance(record.depth, int):
        _fail("depth", "depth must be an integer")
    if not 0 <= record.depth <= settings.max_derivation_depth:
        _fail(
            "depth_limit",
            f"record depth {record.depth} exceeds MAX_DERIVATION_DEPTH="
            f"{settings.max_derivation_depth}",
        )
    if any(not isinstance(parent, UUID) for parent in record.derived_from):
        _fail("parent_id", "every derived_from value must be a UUID")
    if len(record.derived_from) > settings.max_derived_from:
        _fail("too_many_parents", "derived_from exceeds MAX_DERIVED_FROM")
    if len(record.derived_from) != len(set(record.derived_from)):
        _fail("duplicate_parent", "derived_from contains duplicate IDs")
    if record.id in record.derived_from:
        _fail("parent_cycle", "a record may not cite itself as a parent")
    if record.occurred_at is not None and (
        not isinstance(record.occurred_at, datetime)
        or (record.occurred_at.tzinfo is None or record.occurred_at.utcoffset() is None)
    ):
        _fail("occurred_at", "occurred_at must include a timezone")

    content, content_json = _finite_object(record.content, label="content")
    text = content.get("text")
    if not isinstance(text, str):
        _fail("content_text", "content.text must be a string")
    if len(text) > settings.max_text_chars:
        _fail("text_too_large", "text exceeds MAX_TEXT_CHARS")
    content_limit = (
        settings.max_run_content_bytes
        if record.collection == _SYSTEM_COLLECTION and record.type == "run"
        else settings.max_content_bytes
    )
    if len(content_json) > content_limit:
        if record.collection == _SYSTEM_COLLECTION and record.type == "run":
            _fail("run_too_large", "run content exceeds MAX_RUN_CONTENT_BYTES")
        _fail("content_too_large", "content exceeds MAX_CONTENT_BYTES")

    scores, _ = _finite_object(record.scores, label="scores")
    annotations, _ = _finite_object(record.annotations, label="annotations")
    annotation_meta, _ = _finite_object(record.annotation_meta, label="annotation_meta")
    enrichment_meta, _ = _finite_object(record.enrichment_meta, label="enrichment_meta")
    _validate_annotation_entries(annotations, label="annotations", settings=settings)
    _validate_annotation_entries(annotation_meta, label="annotation_meta", settings=settings)
    _validate_annotation_entries(enrichment_meta, label="enrichment_meta", settings=settings)
    return {
        "content": content,
        "scores": scores,
        "annotations": annotations,
        "annotation_meta": annotation_meta,
        "enrichment_meta": enrichment_meta,
    }


async def insert_canonical_record_tx(
    conn: DatabaseConnection,
    record: CanonicalRecordWrite,
    settings: Settings,
    *,
    dedupe_conflict: DedupeConflictMode = "error",
) -> CanonicalRecordInsert | None:
    """Validate and insert one canonical row in the caller's transaction.

    ``return_none`` is reserved for the public exact-dedupe path.  All other
    callers require an inserted row and treat an empty RETURNING result as an
    invariant failure.
    """

    if dedupe_conflict not in {"error", "return_none"}:
        _fail("dedupe_mode", "unsupported dedupe conflict mode")
    normalized = _validate_write(record, settings)
    result = await conn.execute(
        """
        insert into record (
          id, workspace, collection, collection_version, collection_hash,
          entity, key, type, status, content, embedding, embedding_space,
          scores, annotations, annotation_meta, enrichment_meta, enrichment_error,
          enriched_at, run_id, depth, derived_from, dedupe_key, occurred_at
        )
        values (
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, null, null,
          %s, %s, %s, %s, %s,
          case when %s then now() else null end, %s, %s, %s, %s, coalesce(%s, now())
        )
        on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
        returning id, seq, enriched_at
        """,
        (
            record.id,
            record.workspace,
            record.collection,
            record.collection_version,
            record.collection_hash,
            record.entity,
            record.key,
            record.type,
            record.status,
            Jsonb(normalized["content"]),
            Jsonb(normalized["scores"]),
            Jsonb(normalized["annotations"]),
            Jsonb(normalized["annotation_meta"]),
            Jsonb(normalized["enrichment_meta"]),
            record.enrichment_error,
            record.ready,
            record.run_id,
            record.depth,
            list(record.derived_from),
            record.dedupe_key,
            record.occurred_at,
        ),
    )
    row = await result.fetchone()
    if row is None:
        if dedupe_conflict == "return_none" and record.dedupe_key is not None:
            return None
        _fail("insert_return", "canonical record insert returned no row")
    returned_id = row.get("id")
    seq = row.get("seq")
    if returned_id != record.id:
        _fail("insert_return", "canonical record insert returned an unexpected id")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        _fail("insert_return", "canonical record insert returned an invalid sequence")
    ready = row.get("enriched_at") is not None
    if ready is not record.ready:
        _fail("insert_return", "canonical record insert returned an unexpected ready state")
    return CanonicalRecordInsert(id=record.id, seq=seq, ready=ready)


__all__ = [
    "CanonicalRecordInsert",
    "CanonicalRecordInvariantError",
    "CanonicalRecordWrite",
    "insert_canonical_record_tx",
]
