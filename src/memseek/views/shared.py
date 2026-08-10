"""Shared shaping, pagination, and byte-bounding helpers for canonical reads.

Read views serve persisted canonical values only.  Shapers convert one
database row into the exact JSON-safe response item, and ``bound_page``
guarantees every paginated response fits ``MAX_RESPONSE_BYTES`` by stopping
at the last complete emitted item.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from memseek.render import COMPACT_CONTENT_CHARS, truncate_middle

# One row above the schema's bigint sequence range; used only as a worst-case
# placeholder while sizing the response envelope before the cursor is known.
_CURSOR_PLACEHOLDER = 2**63 - 1


class FrozenQueryModel(BaseModel):
    """Strict, immutable base for read-view query and body parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be non-blank")
        return value


def split_names(value: Any) -> Any:
    """Turn one comma-separated query value into a tuple of names."""

    if isinstance(value, str):
        parts = tuple(part.strip() for part in value.split(","))
        if any(not part for part in parts):
            raise ValueError("comma-separated names must be non-blank")
        return parts
    return value


class ResponseTooLarge(Exception):
    """One complete response cannot fit the configured byte bound."""

    def __init__(self, detail: str) -> None:
        self.code = "response_too_large"
        self.detail = detail
        super().__init__(detail)


def json_size(value: Any) -> int:
    """Measure ``value`` with the exact serializer options of the HTTP layer."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class BoundedPage:
    """A byte-bounded prefix of shaped rows plus its continuation facts."""

    items: tuple[dict[str, Any], ...]
    truncated: bool
    exhausted: bool


def bound_page(
    items: list[dict[str, Any]],
    *,
    limit: int,
    max_bytes: int,
    envelope: dict[str, Any],
    items_field: str,
    cursor_field: str,
) -> BoundedPage:
    """Emit the longest complete prefix of up to ``limit`` shaped items.

    ``items`` may contain one extra row beyond ``limit``; its presence only
    proves that more matching rows exist.  ``envelope`` is the response shape
    without items; the cursor field is sized with a worst-case placeholder so
    the final serialized response can never exceed ``max_bytes``.
    """

    exhausted = len(items) <= limit
    page = items[:limit]
    overhead_probe = dict(envelope)
    overhead_probe[items_field] = []
    overhead_probe[cursor_field] = _CURSOR_PLACEHOLDER
    overhead_probe["truncated"] = True
    budget = max_bytes - json_size(overhead_probe)
    emitted: list[dict[str, Any]] = []
    used = 0
    for item in page:
        size = json_size(item) + (1 if emitted else 0)
        if used + size > budget:
            if not emitted:
                raise ResponseTooLarge(
                    "one response item exceeds MAX_RESPONSE_BYTES; raise the bound"
                )
            return BoundedPage(items=tuple(emitted), truncated=True, exhausted=False)
        emitted.append(item)
        used += size
    return BoundedPage(items=tuple(emitted), truncated=False, exhausted=exhausted)


def timestamp(value: Any) -> str:
    """Serialize one canonical timestamptz column."""

    if not isinstance(value, datetime):
        raise TypeError("canonical read timestamp is invalid")
    return value.isoformat()


def _optional_timestamp(value: Any) -> str | None:
    return None if value is None else timestamp(value)


def _uuid(value: Any) -> str | None:
    return None if value is None else str(value)


def _tombstone(content: Mapping[str, Any]) -> bool:
    return bool(content.get("tombstone", False))


def citations(row: Mapping[str, Any]) -> list[str]:
    """Direct provenance excluding the record's own run parent."""

    run_id = row["run_id"]
    return [str(parent) for parent in row["derived_from"] if parent != run_id]


def record_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """The full canonical dereference row; the raw vector stays internal."""

    content = row["content"]
    return {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "collection": row["collection"],
        "collection_version": int(row["collection_version"]),
        "collection_hash": row["collection_hash"],
        "entity": row["entity"],
        "key": row["key"],
        "type": row["type"],
        "status": row["status"],
        "content": content,
        "tombstone": _tombstone(content),
        "embedding_space": row["embedding_space"],
        "scores": row["scores"],
        "annotations": row["annotations"],
        "annotation_meta": row["annotation_meta"],
        "enrichment_meta": row["enrichment_meta"],
        "enrichment_error": row["enrichment_error"],
        "ready": row["enriched_at"] is not None,
        "enriched_at": _optional_timestamp(row["enriched_at"]),
        "run_id": _uuid(row["run_id"]),
        "depth": int(row["depth"]),
        "derived_from": [str(parent) for parent in row["derived_from"]],
        "dedupe_key": row["dedupe_key"],
        "occurred_at": timestamp(row["occurred_at"]),
        "created_at": timestamp(row["created_at"]),
        "last_accessed": timestamp(row["last_accessed"]),
    }


def record_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """One compact timeline row using the 500-character content profile."""

    content = row["content"]
    return {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "collection": row["collection"],
        "key": row["key"],
        "type": row["type"],
        "status": row["status"],
        "ready": row["enriched_at"] is not None,
        "tombstone": _tombstone(content),
        "run_id": _uuid(row["run_id"]),
        "text": truncate_middle(str(content["text"]), COMPACT_CONTENT_CHARS),
        "occurred_at": timestamp(row["occurred_at"]),
        "created_at": timestamp(row["created_at"]),
    }


def record_version(row: Mapping[str, Any], *, include_entity: bool = False) -> dict[str, Any]:
    """One complete history/delta version row with direct citations."""

    content = row["content"]
    version: dict[str, Any] = {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "collection": row["collection"],
        "collection_version": int(row["collection_version"]),
        "collection_hash": row["collection_hash"],
        "key": row["key"],
        "type": row["type"],
        "status": row["status"],
        "content": content,
        "tombstone": _tombstone(content),
        "ready": row["enriched_at"] is not None,
        "run_id": _uuid(row["run_id"]),
        "citations": citations(row),
        "depth": int(row["depth"]),
        "enrichment_error": row["enrichment_error"],
        "occurred_at": timestamp(row["occurred_at"]),
        "created_at": timestamp(row["created_at"]),
    }
    if include_entity:
        version["entity"] = row["entity"]
    return version


def belief_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One current keyed belief for ``GET /document``."""

    content = row["content"]
    return {
        "collection": row["collection"],
        "collection_version": int(row["collection_version"]),
        "collection_hash": row["collection_hash"],
        "key": row["key"],
        "type": row["type"],
        "text": str(content["text"]),
        "id": str(row["id"]),
        "run_id": _uuid(row["run_id"]),
        "citations": citations(row),
        "status": row["status"],
        "depth": int(row["depth"]),
        "ready": row["enriched_at"] is not None,
        "enrichment_error": row["enrichment_error"],
        "occurred_at": timestamp(row["occurred_at"]),
        "created_at": timestamp(row["created_at"]),
    }


def retraction_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One tombstone-current key so a client can invalidate its cache."""

    return {
        "collection": row["collection"],
        "key": row["key"],
        "id": str(row["id"]),
        "seq": int(row["seq"]),
    }


__all__ = [
    "BoundedPage",
    "FrozenQueryModel",
    "ResponseTooLarge",
    "belief_view",
    "bound_page",
    "citations",
    "json_size",
    "record_detail",
    "record_summary",
    "record_version",
    "retraction_view",
    "split_names",
    "timestamp",
]
