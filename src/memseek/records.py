"""Canonical validation and transactional insertion for public records.

PostgreSQL is the source of truth for both deduplication and provenance.  This
module deliberately performs no provider calls: public ingest either commits
the complete canonical batch and its ready-transition outbox effects, or it
commits nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memseek import __version__
from memseek.canonical_records import (
    CanonicalRecordInvariantError,
    CanonicalRecordWrite,
    insert_canonical_record_tx,
)
from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import CollectionDefinition, DefinitionCatalog
from memseek.definitions.models import DeclaredField
from memseek.locks import acquire_entity_locks, acquire_workspace_lock
from memseek.templates import TemplateError, render_template

_PUBLIC_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_RESERVED_PUBLIC_TYPES = frozenset({"run", "erasure"})


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicRecordInput(_FrozenModel):
    """One untrusted public-record request before collection resolution."""

    entity: str
    collection: str = "main"
    collection_version: int | None = Field(default=None, ge=1)
    type: str
    key: str | None = None
    text: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    status: Literal["active", "draft"] = "active"
    dedupe_key: str | None = None
    derived_from: tuple[UUID, ...] = ()
    tombstone: bool = False
    occurred_at: datetime | None = None
    scores: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include a timezone")
        return value


class RecordBatchRequest(_FrozenModel):
    """Atomic public-ingest payload; the deployment limit is checked by the service."""

    records: tuple[PublicRecordInput, ...] = Field(min_length=1)


class RecordWrite(_FrozenModel):
    """A committed insert or exact duplicate.

    Only ``index``, ``id``, and ``ready`` belong in the HTTP response.  The
    excluded fields carry enough identity for in-transaction projection hooks
    without requiring those hooks to trust request payloads.
    """

    index: int
    id: UUID
    ready: bool
    collection: str = Field(exclude=True)
    entity: str = Field(exclude=True)
    key: str | None = Field(exclude=True)
    status: Literal["active", "draft"] = Field(exclude=True)


class RecordBatchResult(_FrozenModel):
    """Partition of a committed batch into new rows and exact retries."""

    inserted: tuple[RecordWrite, ...]
    duplicates: tuple[RecordWrite, ...]


class RecordValidationError(ValueError):
    """Expected public-ingest failure with a stable API machine code."""

    def __init__(self, code: str, detail: str, *, index: int | None = None) -> None:
        self.code = code
        self.detail = detail
        self.index = index
        prefix = f"records[{index}]: " if index is not None else ""
        super().__init__(prefix + detail)

    def at(self, index: int) -> RecordValidationError:
        if self.index is not None:
            return self
        return type(self)(self.code, self.detail, index=index)


class DedupeConflict(RecordValidationError):
    """A dedupe key already names a different immutable payload."""

    def __init__(self, detail: str, *, index: int | None = None) -> None:
        super().__init__("dedupe_conflict", detail, index=index)

    def at(self, index: int) -> DedupeConflict:
        if self.index is not None:
            return self
        return type(self)(self.detail, index=index)


class ReadyTransitionHook(Protocol):
    """Narrow seam for the atomic ready-transition projection outbox."""

    def __call__(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        records: tuple[RecordWrite, ...],
        catalog: DefinitionCatalog,
    ) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    index: int
    request: PublicRecordInput
    definition: CollectionDefinition
    content: dict[str, Any]
    parents: tuple[UUID, ...]
    scores: dict[str, float]
    annotations: dict[str, Any]
    projected_scores: dict[str, float]
    explicit_score_names: frozenset[str]
    explicit_annotation_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Parent:
    id: UUID
    workspace: str
    seq: int
    depth: int


def _canonical_json(value: Any, *, code: str, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecordValidationError(code, f"{label} must be finite JSON: {exc}") from exc


def _validate_name(value: str, label: str) -> None:
    if not _PUBLIC_NAME_RE.fullmatch(value):
        raise RecordValidationError("name", f"{label} must match {_PUBLIC_NAME_RE.pattern}")


def _lookup_path(value: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _datetime_matches(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _scalar_matches(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if kind == "datetime":
        return _datetime_matches(value)
    return False


def _field_matches(field: DeclaredField, value: Any) -> bool:
    if field.is_array:
        return isinstance(value, list) and all(
            _scalar_matches(field.scalar_type, item) for item in value
        )
    return _scalar_matches(field.scalar_type, value)


def _validate_declared_fields(
    definition: CollectionDefinition,
    content: Mapping[str, Any],
    annotations: Mapping[str, Any],
) -> None:
    root = {"content": content, "annotations": annotations}
    for name, field in definition.fields.items():
        present, value = _lookup_path(root, field.path)
        if present and not _field_matches(field, value):
            rendered_type = f"array[{field.scalar_type}]" if field.is_array else field.scalar_type
            raise RecordValidationError(
                "field_type",
                f"declared field {name!r} at {field.path!r} must be {rendered_type}",
            )


def _validate_json_schema(definition: CollectionDefinition, content: dict[str, Any]) -> None:
    validator = Draft202012Validator(
        definition.content_schema,
        format_checker=FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(content),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path)
    location = f"content.{path}" if path else "content"
    raise RecordValidationError("content_schema", f"{location}: {error.message}")


def _processor_output_hash(output: Any) -> str:
    return hashlib.sha256(
        _canonical_json(output, code="annotation_json", label="annotation output")
    ).hexdigest()


def _extract_numeric_path(output: Any, path: str, *, processor: str) -> float:
    present, value = _lookup_path(output, path) if isinstance(output, Mapping) else (False, None)
    if (
        not present
        or not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise RecordValidationError(
            "annotation_score",
            f"client annotation {processor!r} score path {path!r} is not a finite number",
        )
    return float(value)


def _validate_client_outputs(
    item: PublicRecordInput,
    definition: CollectionDefinition,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> tuple[dict[str, float], dict[str, Any], dict[str, float]]:
    scores: dict[str, float] = {}
    annotations: dict[str, Any] = {}
    projected_scores: dict[str, float] = {}

    for name, raw_value in item.scores.items():
        scorer = catalog.processors.get(name)
        if scorer is None or scorer.kind != "score" or scorer.source != "client":
            raise RecordValidationError(
                "client_scorer",
                f"score {name!r} is not a configured client score processor",
            )
        if definition.name not in scorer.input.collections or (
            scorer.input.types and item.type not in scorer.input.types
        ):
            raise RecordValidationError(
                "client_scorer_scope",
                f"score {name!r} does not accept {definition.name}/{item.type}",
            )
        if not _scalar_matches("number", raw_value):
            raise RecordValidationError(
                "client_scorer",
                f"score {name!r} must be a finite number",
            )
        assert scorer.scale is not None
        low, high = scorer.scale
        value = min(high, max(low, float(raw_value)))
        scores[name] = value
        annotations[name] = {"value": value}

    for name, output in item.annotations.items():
        if name in annotations:
            raise RecordValidationError(
                "client_annotation",
                f"processor {name!r} was supplied as both a score and annotation",
            )
        processor = catalog.processors.get(name)
        if processor is None or processor.kind != "json" or processor.source != "client":
            raise RecordValidationError(
                "client_annotation",
                f"annotation {name!r} is not a configured client json processor",
            )
        if definition.name not in processor.input.collections or (
            processor.input.types and item.type not in processor.input.types
        ):
            raise RecordValidationError(
                "client_annotation_scope",
                f"annotation {name!r} does not accept {definition.name}/{item.type}",
            )
        validator = Draft202012Validator(
            processor.effective_output_schema,
            format_checker=FormatChecker(),
        )
        error = next(iter(validator.iter_errors(output)), None)
        if error is not None:
            raise RecordValidationError(
                "client_annotation_schema",
                f"annotation {name!r}: {error.message}",
            )
        encoded = _canonical_json(output, code="annotation_json", label=f"annotation {name!r}")
        if len(encoded) > settings.max_annotation_bytes:
            raise RecordValidationError(
                "annotation_too_large",
                f"annotation {name!r} exceeds MAX_ANNOTATION_BYTES",
            )
        annotations[name] = output
        for score_name, path in processor.score_fields.items():
            projected_scores[score_name] = _extract_numeric_path(
                output,
                path,
                processor=name,
            )

    missing_required_clients = sorted(
        name
        for name in definition.required_processors
        if not item.tombstone
        and (processor := catalog.processors.get(name)) is not None
        and processor.kind == "score"
        and processor.source == "client"
        and name not in item.scores
    )
    if missing_required_clients:
        raise RecordValidationError(
            "required_client_output",
            "required client scorer output is missing: " + ", ".join(missing_required_clients),
        )

    return scores, annotations, projected_scores


def _prepare_record(
    item: PublicRecordInput,
    *,
    index: int,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> _PreparedRecord:
    try:
        if item.collection.startswith("_"):
            raise RecordValidationError(
                "reserved_collection", "public collections may not begin with '_'"
            )
        _validate_name(item.collection, "collection")
        _validate_name(item.type, "type")
        if item.type in _RESERVED_PUBLIC_TYPES:
            raise RecordValidationError(
                "reserved_type", f"public records may not use type {item.type!r}"
            )
        if not item.entity or item.entity == "*" or len(item.entity) > 255:
            raise RecordValidationError(
                "entity", "entity must be non-empty, other than '*', and at most 255 characters"
            )
        if item.key is not None and len(item.key) > 128:
            raise RecordValidationError("key", "key must be at most 128 characters")
        if item.dedupe_key is not None and len(item.dedupe_key) > 256:
            raise RecordValidationError("dedupe_key", "dedupe_key must be at most 256 characters")

        try:
            definition = catalog.resolve_collection(item.collection, item.collection_version)
        except (KeyError, ValueError) as exc:
            raise RecordValidationError("collection", str(exc)) from exc

        if definition.mode == "event" and item.key is not None:
            raise RecordValidationError(
                "record_mode", f"event collection {definition.name!r} forbids keys"
            )
        if definition.mode == "keyed" and item.key is None:
            raise RecordValidationError(
                "record_mode", f"keyed collection {definition.name!r} requires a key"
            )

        if len(item.derived_from) != len(set(item.derived_from)):
            raise RecordValidationError("duplicate_parent", "derived_from contains duplicate IDs")
        if len(item.derived_from) > settings.max_derived_from:
            raise RecordValidationError("too_many_parents", "derived_from exceeds MAX_DERIVED_FROM")
        parents = tuple(sorted(item.derived_from, key=str))

        content = dict(item.content)
        if "tombstone" in content:
            raise RecordValidationError("reserved_content", "content.tombstone is system-owned")
        if item.tombstone:
            if item.key is None:
                raise RecordValidationError("tombstone", "a tombstone requires a key")
            if not parents:
                raise RecordValidationError("tombstone", "a tombstone requires at least one parent")
            if item.text not in (None, ""):
                raise RecordValidationError("tombstone", "a tombstone requires empty text")
            if set(content) - {"text"}:
                raise RecordValidationError(
                    "tombstone", "a tombstone cannot carry additional content"
                )
            if "text" in content and content["text"] != "":
                raise RecordValidationError("text_mismatch", "content.text must be empty")
            text = ""
            content["text"] = text
            content["tombstone"] = True
        else:
            if item.text is None:
                if definition.text_projection is None:
                    raise RecordValidationError(
                        "text_required",
                        f"collection {definition.name!r} requires top-level text",
                    )
                try:
                    text = render_template(definition.text_projection, content)
                except TemplateError as exc:
                    raise RecordValidationError("text_projection", str(exc)) from exc
            else:
                text = item.text
            if "text" in content and content["text"] != text:
                raise RecordValidationError(
                    "text_mismatch", "supplied content.text must equal resolved top-level text"
                )
            content["text"] = text

        if len(text) > settings.max_text_chars:
            raise RecordValidationError("text_too_large", "text exceeds MAX_TEXT_CHARS")

        scores, annotations, projected_scores = _validate_client_outputs(
            item,
            definition,
            catalog,
            settings,
        )
        # A tombstone has one engine-owned canonical content shape regardless
        # of the prior record's user schema. Its key and parent continuity are
        # validated above; applying a normal page/profile schema here would
        # make a valid retraction impossible for schemas with required fields.
        if not item.tombstone:
            _validate_declared_fields(definition, content, annotations)
            _validate_json_schema(definition, content)
        return _PreparedRecord(
            index=index,
            request=item,
            definition=definition,
            content=content,
            parents=parents,
            scores=scores,
            annotations=annotations,
            projected_scores=projected_scores,
            explicit_score_names=frozenset(item.scores),
            explicit_annotation_names=frozenset(item.annotations),
        )
    except RecordValidationError as exc:
        raise exc.at(index) from exc


async def _load_parents(
    conn: DatabaseConnection,
    *,
    workspace: str,
    parent_ids: set[UUID],
) -> dict[UUID, _Parent]:
    if not parent_ids:
        return {}
    result = await conn.execute(
        """
        select id, workspace, seq, depth
        from record
        where id = any(%s::uuid[])
        """,
        (list(parent_ids),),
    )
    rows = await result.fetchall()
    parents = {
        row["id"]: _Parent(
            id=row["id"],
            workspace=str(row["workspace"]),
            seq=int(row["seq"]),
            depth=int(row["depth"]),
        )
        for row in rows
    }
    missing = sorted(parent_ids - parents.keys(), key=str)
    if missing:
        raise RecordValidationError("parent_not_found", f"parent does not exist: {missing[0]}")
    foreign = sorted(
        (parent.id for parent in parents.values() if parent.workspace != workspace),
        key=str,
    )
    if foreign:
        raise RecordValidationError(
            "parent_workspace", f"parent belongs to another workspace: {foreign[0]}"
        )
    return parents


async def _validate_parent_ancestry(
    conn: DatabaseConnection,
    *,
    workspace: str,
    parent_ids: set[UUID],
) -> None:
    """Reject a corrupt reachable lineage rather than extending it.

    A newly generated public ID cannot itself be an ancestor.  The recursive
    check additionally proves that reachable existing edges preserve both
    workspace ownership and strict sequence order, which implies acyclicity.
    """

    if not parent_ids:
        return
    result = await conn.execute(
        """
        with recursive ancestry(id, workspace, seq, derived_from, path, cycle, bad_order) as (
          select record.id,
                 record.workspace,
                 record.seq,
                 record.derived_from,
                 array[record.id],
                 false,
                 false
          from record
          where record.id = any(%s::uuid[])
          union all
          select parent.id,
                 parent.workspace,
                 parent.seq,
                 parent.derived_from,
                 ancestry.path || parent.id,
                 parent.id = any(ancestry.path),
                 ancestry.bad_order or parent.seq >= ancestry.seq
          from ancestry
          cross join lateral unnest(ancestry.derived_from) edge(id)
          join record parent on parent.id = edge.id
          where not ancestry.cycle
        )
        select
          coalesce(bool_or(cycle), false) as has_cycle,
          coalesce(bool_or(bad_order), false) as has_bad_order,
          coalesce(bool_or(workspace <> %s), false) as has_foreign_workspace
        from ancestry
        """,
        (list(parent_ids), workspace),
    )
    row = await result.fetchone()
    if row is None:
        return
    if row["has_foreign_workspace"]:
        raise RecordValidationError(
            "parent_workspace", "reachable parent lineage crosses workspaces"
        )
    if row["has_cycle"]:
        raise RecordValidationError("parent_cycle", "parent lineage contains a cycle")
    if row["has_bad_order"]:
        raise RecordValidationError(
            "parent_order", "parent lineage does not preserve strict sequence order"
        )


async def _current_keyed_id(
    conn: DatabaseConnection,
    *,
    workspace: str,
    item: _PreparedRecord,
) -> UUID | None:
    if item.request.key is None:
        return None
    result = await conn.execute(
        """
        select id
        from record
        where workspace = %s
          and entity = %s
          and collection = %s
          and key = %s
          and status = %s
        order by seq desc
        limit 1
        """,
        (
            workspace,
            item.request.entity,
            item.definition.name,
            item.request.key,
            item.request.status,
        ),
    )
    row = await result.fetchone()
    return row["id"] if row is not None else None


def _record_depth(
    item: _PreparedRecord,
    parents: Mapping[UUID, _Parent],
    current_keyed_id: UUID | None,
    settings: Settings,
) -> int:
    if not item.parents:
        return 0
    contributions = []
    for parent_id in item.parents:
        parent = parents[parent_id]
        continuity = item.request.key is not None and parent.id == current_keyed_id
        contributions.append(parent.depth if continuity else parent.depth + 1)
    depth = max(contributions)
    if depth > settings.max_derivation_depth:
        raise RecordValidationError(
            "depth_limit",
            f"record depth {depth} exceeds MAX_DERIVATION_DEPTH={settings.max_derivation_depth}",
            index=item.index,
        )
    return depth


def _client_annotation_meta(
    item: _PreparedRecord,
    *,
    record_id: UUID,
    completed_at: datetime,
    run_ids: Mapping[str, UUID],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in sorted(item.annotations):
        output = item.annotations[name]
        entry = {
            "processor": name,
            "processor_config_hash": catalog.processor_config_hashes[name],
            "source": "client",
            "source_record_id": str(record_id),
            "run_id": str(run_ids[name]),
            "output_hash": _processor_output_hash(output),
            "completed_at": completed_at.isoformat(),
        }
        if (
            len(_canonical_json(entry, code="annotation_json", label=f"metadata {name!r}"))
            > settings.max_annotation_bytes
        ):
            raise RecordValidationError(
                "annotation_too_large",
                f"annotation metadata {name!r} exceeds MAX_ANNOTATION_BYTES",
                index=item.index,
            )
        metadata[name] = entry
    return metadata


def _client_run_content(
    *,
    processor: str,
    processor_hash: str,
    target_id: UUID,
    completed_at: datetime,
    settings: Settings,
) -> dict[str, Any]:
    timestamp = completed_at.isoformat().replace("+00:00", "Z")
    return {
        "text": f"annotation {processor} ok",
        "schema_version": 1,
        "engine_version": f"{__version__}+{settings.memseek_build_sha}",
        "operation": "annotate",
        "processor": processor,
        "status": "ok",
        "source": "client",
        "target_record_id": str(target_id),
        "annotation_key": processor,
        "call_batch_id": None,
        "config_hash": processor_hash,
        "definition_refs": [
            {
                "kind": "processor",
                "name": processor,
                "version": None,
                "hash": processor_hash,
            }
        ],
        "model_calls": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "warnings": [],
        "started_at": timestamp,
        "completed_at": timestamp,
        "ms": 0,
        "error_kind": None,
        "error": None,
    }


async def _insert_client_runs(
    conn: DatabaseConnection,
    *,
    workspace: str,
    item: _PreparedRecord,
    target_id: UUID,
    target_depth: int,
    completed_at: datetime,
    run_ids: Mapping[str, UUID],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> tuple[RecordWrite, ...]:
    if not run_ids:
        return ()
    # Lazy import keeps the canonical system collection constants shared with
    # enrichment without creating an import cycle at module load time.
    from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION

    runs: list[RecordWrite] = []
    for processor in sorted(run_ids):
        processor_hash = catalog.processor_config_hashes[processor]
        content = _client_run_content(
            processor=processor,
            processor_hash=processor_hash,
            target_id=target_id,
            completed_at=completed_at,
            settings=settings,
        )
        try:
            inserted = await insert_canonical_record_tx(
                conn,
                CanonicalRecordWrite(
                    id=run_ids[processor],
                    workspace=workspace,
                    collection="_system",
                    collection_version=SYSTEM_COLLECTION_VERSION,
                    collection_hash=SYSTEM_COLLECTION_HASH,
                    entity=item.request.entity,
                    type="run",
                    content=content,
                    ready=True,
                    depth=target_depth,
                    derived_from=(target_id,),
                ),
                settings,
            )
        except CanonicalRecordInvariantError as exc:
            code = "run_json" if exc.code == "finite_json" else exc.code
            detail = (
                "client annotation run exceeds MAX_RUN_CONTENT_BYTES"
                if exc.code == "run_too_large"
                else exc.detail
            )
            raise RecordValidationError(code, detail, index=item.index) from exc
        if inserted is None:
            raise RuntimeError("canonical client annotation run insert returned no row")
        runs.append(
            RecordWrite(
                index=item.index,
                id=inserted.id,
                ready=inserted.ready,
                collection="_system",
                entity=item.request.entity,
                key=None,
                status="active",
            )
        )
    return tuple(runs)


def _value_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_json(left, code="integrity", label="stored value") == _canonical_json(
            right,
            code="integrity",
            label="requested value",
        )
    except RecordValidationError:
        return False


def _dedupe_matches(
    row: Mapping[str, Any],
    *,
    item: _PreparedRecord,
    scores: Mapping[str, float],
    annotations: Mapping[str, Any],
) -> bool:
    request = item.request
    immutable_equal = (
        row["entity"] == request.entity
        and row["collection"] == item.definition.name
        and row["collection_version"] == item.definition.version
        and row["collection_hash"] == item.definition.contract_hash
        and row["type"] == request.type
        and row["key"] == request.key
        and row["status"] == request.status
        and _value_equal(row["content"], item.content)
        and tuple(sorted(row["derived_from"], key=str)) == item.parents
    )
    if not immutable_equal:
        return False
    if request.occurred_at is not None and row["occurred_at"] != request.occurred_at:
        return False
    stored_scores = row["scores"]
    stored_annotations = row["annotations"]
    for name in item.explicit_score_names:
        if stored_scores.get(name) != scores[name] or not _value_equal(
            stored_annotations.get(name), annotations[name]
        ):
            return False
    for name in item.explicit_annotation_names:
        if not _value_equal(stored_annotations.get(name), annotations[name]):
            return False
    return all(stored_scores.get(name) == value for name, value in item.projected_scores.items())


async def _fetch_dedupe_row(
    conn: DatabaseConnection,
    *,
    workspace: str,
    dedupe_key: str,
) -> Mapping[str, Any]:
    result = await conn.execute(
        """
        select id,
               seq,
               entity,
               collection,
               collection_version,
               collection_hash,
               type,
               key,
               status,
               content,
               scores,
               annotations,
               derived_from,
               occurred_at,
               enriched_at
        from record
        where workspace = %s and dedupe_key = %s
        """,
        (workspace, dedupe_key),
    )
    row = await result.fetchone()
    if row is None:
        raise RuntimeError("dedupe conflict row disappeared while workspace mutation lock was held")
    return row


async def _insert_one(
    conn: DatabaseConnection,
    *,
    workspace: str,
    item: _PreparedRecord,
    parents: Mapping[UUID, _Parent],
    completed_at: datetime,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> tuple[RecordWrite, bool, tuple[RecordWrite, ...]]:
    request = item.request
    record_id = uuid4()
    current_keyed_id = await _current_keyed_id(conn, workspace=workspace, item=item)
    depth = _record_depth(item, parents, current_keyed_id, settings)
    ready = request.tombstone or not item.definition.required_processors
    annotations = dict(item.annotations)
    scores = {**item.scores, **item.projected_scores}
    run_ids = {name: uuid4() for name in sorted(item.annotations)}
    metadata = _client_annotation_meta(
        item,
        record_id=record_id,
        completed_at=completed_at,
        run_ids=run_ids,
        catalog=catalog,
        settings=settings,
    )
    try:
        inserted = await insert_canonical_record_tx(
            conn,
            CanonicalRecordWrite(
                id=record_id,
                workspace=workspace,
                collection=item.definition.name,
                collection_version=item.definition.version,
                collection_hash=item.definition.contract_hash,
                entity=request.entity,
                key=request.key,
                type=request.type,
                status=request.status,
                content=item.content,
                scores=scores,
                annotations=annotations,
                annotation_meta=metadata,
                ready=ready,
                depth=depth,
                derived_from=item.parents,
                dedupe_key=request.dedupe_key,
                occurred_at=request.occurred_at,
            ),
            settings,
            dedupe_conflict="return_none",
        )
    except CanonicalRecordInvariantError as exc:
        code = "content_json" if exc.code == "finite_json" else exc.code
        raise RecordValidationError(code, exc.detail, index=item.index) from exc
    if inserted is not None:
        child_seq = inserted.seq
        if any(parents[parent_id].seq >= child_seq for parent_id in item.parents):
            raise RecordValidationError(
                "parent_order",
                "every public parent must be older than its child",
                index=item.index,
            )
        write = RecordWrite(
            index=item.index,
            id=inserted.id,
            ready=inserted.ready,
            collection=item.definition.name,
            entity=request.entity,
            key=request.key,
            status=request.status,
        )
        client_runs = await _insert_client_runs(
            conn,
            workspace=workspace,
            item=item,
            target_id=write.id,
            target_depth=depth,
            completed_at=completed_at,
            run_ids=run_ids,
            catalog=catalog,
            settings=settings,
        )
        return write, True, client_runs

    if request.dedupe_key is None:
        raise RuntimeError("record insert unexpectedly returned no row without a dedupe key")
    existing = await _fetch_dedupe_row(
        conn,
        workspace=workspace,
        dedupe_key=request.dedupe_key,
    )
    if not _dedupe_matches(
        existing,
        item=item,
        scores=scores,
        annotations=annotations,
    ):
        raise DedupeConflict(
            f"dedupe key {request.dedupe_key!r} already names a different record",
            index=item.index,
        )
    return (
        RecordWrite(
            index=item.index,
            id=existing["id"],
            ready=existing["enriched_at"] is not None,
            collection=item.definition.name,
            entity=request.entity,
            key=request.key,
            status=request.status,
        ),
        False,
        (),
    )


async def _refresh_current_projection(
    conn: DatabaseConnection,
    *,
    workspace: str,
    records: tuple[RecordWrite, ...],
) -> None:
    from memseek.projections import refresh_current_projection_tx

    await refresh_current_projection_tx(
        conn,
        workspace=workspace,
        records=tuple(
            {
                "id": record.id,
                "collection": record.collection,
                "entity": record.entity,
                "key": record.key,
                "status": record.status,
            }
            for record in records
        ),
    )


async def _on_records_ready(
    conn: DatabaseConnection,
    *,
    workspace: str,
    records: tuple[RecordWrite, ...],
    catalog: DefinitionCatalog,
) -> None:
    from memseek.projections import on_records_ready_tx

    await on_records_ready_tx(
        conn,
        workspace=workspace,
        records=tuple({"id": record.id, "collection": record.collection} for record in records),
        catalog=catalog,
    )


async def insert_records_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    records: Sequence[PublicRecordInput],
    catalog: DefinitionCatalog,
    settings: Settings,
    on_ready: ReadyTransitionHook | None = None,
) -> RecordBatchResult:
    """Validate and insert one public batch inside the caller's transaction.

    The caller owns commit/rollback.  Workspace, entity, parent, canonical row,
    readiness, and projection-outbox effects all occur in this one transaction.
    """

    if not records:
        raise RecordValidationError("empty_batch", "records must contain at least one item")
    if len(records) > settings.max_batch:
        raise RecordValidationError(
            "batch_too_large",
            f"records exceeds MAX_BATCH={settings.max_batch}",
        )
    prepared = tuple(
        _prepare_record(item, index=index, catalog=catalog, settings=settings)
        for index, item in enumerate(records)
    )

    await acquire_workspace_lock(conn, workspace, exclusive=False)
    workspace_result = await conn.execute(
        "select exists(select 1 from workspace where id = %s) as present",
        (workspace,),
    )
    workspace_row = await workspace_result.fetchone()
    if workspace_row is None or not workspace_row["present"]:
        raise RecordValidationError("workspace_not_found", "workspace does not exist")

    await acquire_entity_locks(
        conn,
        workspace,
        (item.request.entity for item in prepared if item.request.key is not None),
    )
    parent_ids = {parent_id for item in prepared for parent_id in item.parents}
    parents = await _load_parents(conn, workspace=workspace, parent_ids=parent_ids)
    await _validate_parent_ancestry(conn, workspace=workspace, parent_ids=parent_ids)
    time_result = await conn.execute("select now() as completed_at")
    time_row = await time_result.fetchone()
    if time_row is None:
        raise RuntimeError("PostgreSQL did not return the transaction timestamp")
    completed_at = time_row["completed_at"]

    inserted: list[RecordWrite] = []
    duplicates: list[RecordWrite] = []
    ready_control_rows: list[RecordWrite] = []
    for item in prepared:
        write, is_insert, client_runs = await _insert_one(
            conn,
            workspace=workspace,
            item=item,
            parents=parents,
            completed_at=completed_at,
            catalog=catalog,
            settings=settings,
        )
        (inserted if is_insert else duplicates).append(write)
        ready_control_rows.extend(client_runs)

    awaiting_keyed = tuple(write for write in inserted if write.key is not None and not write.ready)
    if awaiting_keyed:
        await _refresh_current_projection(conn, workspace=workspace, records=awaiting_keyed)
    newly_ready = (
        *(write for write in inserted if write.ready),
        *ready_control_rows,
    )
    if newly_ready:
        hook = on_ready or _on_records_ready
        await hook(
            conn,
            workspace=workspace,
            records=newly_ready,
            catalog=catalog,
        )
    return RecordBatchResult(inserted=tuple(inserted), duplicates=tuple(duplicates))


async def insert_public_records(
    pool: DatabasePool,
    *,
    workspace: str,
    request: RecordBatchRequest,
    catalog: DefinitionCatalog,
    settings: Settings,
    on_ready: ReadyTransitionHook | None = None,
) -> RecordBatchResult:
    """Own the atomic transaction used by the public ``POST /records`` service."""

    async with pool.connection() as conn, conn.transaction():
        return await insert_records_tx(
            conn,
            workspace=workspace,
            records=request.records,
            catalog=catalog,
            settings=settings,
            on_ready=on_ready,
        )


__all__ = [
    "DedupeConflict",
    "PublicRecordInput",
    "ReadyTransitionHook",
    "RecordBatchRequest",
    "RecordBatchResult",
    "RecordValidationError",
    "RecordWrite",
    "insert_public_records",
    "insert_records_tx",
]
