"""Expanded run reads: paginated summaries and the atomic run review surface.

`GET /runs` is how an operator discovers a draft skill or profile run to
inspect; `GET /runs/{id}` returns that run with its output rows in the exact
order recorded by `content.output_ids`.  Both endpoints serve persisted run
content only and never re-render prompts or expose model responses.
"""

from __future__ import annotations

from typing import Any, Literal, LiteralString, cast
from uuid import UUID

from pydantic import Field, field_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.views.shared import (
    FrozenQueryModel,
    ResponseTooLarge,
    bound_page,
    json_size,
    record_version,
    timestamp,
)

_SUMMARY_CONTENT_KEYS = (
    "operation",
    "status",
    "job_id",
    "trigger_reasons",
    "wm_before",
    "high_seq",
    "source_kind",
    "config_hash",
    "contract_hash",
    "definition_refs",
    "completed_at",
    "ms",
    "error_kind",
)


class RunsQuery(FrozenQueryModel):
    """Strict `GET /runs` filters; `processor` matches the public name."""

    entity: str | None = Field(default=None, max_length=255)
    processor: str | None = Field(default=None, max_length=128)
    operation: Literal["annotate", "derive", "materialize", "promote"] | None = None
    source: Literal["changes", "snapshot"] | None = None
    status: Literal["ok", "noop", "failed"] | None = None
    limit: int = Field(default=100, ge=1, le=100)
    before_seq: int | None = Field(default=None, ge=1)

    @field_validator("entity")
    @classmethod
    def reject_wildcard(cls, value: str | None) -> str | None:
        if value == "*":
            raise ValueError("entity cannot be '*'")
        return value


class RunOutputsQuery(FrozenQueryModel):
    """Strict `GET /runs/{id}` output paging parameters."""

    output_offset: int = Field(default=0, ge=0)
    output_limit: int = Field(default=50, ge=1, le=50)


def _run_name(content: dict[str, Any]) -> str | None:
    """The public processor/derivation or artifact identity of one run."""

    for field in ("processor", "derivation", "artifact"):
        value = content.get(field)
        if isinstance(value, str):
            return value
    return None


def _output_count(content: dict[str, Any]) -> int:
    output_ids = content.get("output_ids")
    if isinstance(output_ids, list):
        return len(output_ids)
    # Annotation runs record exactly one target instead of output rows.
    return 1 if content.get("target_record_id") else 0


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = row["content"]
    summary: dict[str, Any] = {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "entity": row["entity"],
        "processor": _run_name(content),
        "output_count": _output_count(content),
        "created_at": timestamp(row["created_at"]),
    }
    for key in _SUMMARY_CONTENT_KEYS:
        summary[key] = content.get(key)
    return summary


async def fetch_runs(
    pool: DatabasePool,
    *,
    workspace: str,
    query: RunsQuery,
    settings: Settings,
) -> dict[str, Any]:
    """Return run summaries newest-first with byte-bounded pagination."""

    clauses = ["workspace = %s", "collection = '_system'", "type = 'run'"]
    params: list[Any] = [workspace]
    if query.entity is not None:
        clauses.append("entity = %s")
        params.append(query.entity)
    if query.processor is not None:
        clauses.append(
            "coalesce(content->>'processor', content->>'derivation', content->>'artifact') = %s"
        )
        params.append(query.processor)
    if query.operation is not None:
        clauses.append("content->>'operation' = %s")
        params.append(query.operation)
    if query.source is not None:
        clauses.append("content->>'source_kind' = %s")
        params.append(query.source)
    if query.status is not None:
        clauses.append("content->>'status' = %s")
        params.append(query.status)
    if query.before_seq is not None:
        clauses.append("seq < %s")
        params.append(query.before_seq)
    params.append(query.limit + 1)
    async with pool.connection() as conn:
        result = await conn.execute(
            cast(
                LiteralString,
                f"""
                select id, seq, entity, content, created_at
                from record
                where {" and ".join(clauses)}
                order by seq desc
                limit %s
                """,
            ),
            params,
        )
        rows = await result.fetchall()
    page = bound_page(
        [_run_summary(row) for row in rows],
        limit=query.limit,
        max_bytes=settings.max_response_bytes,
        envelope={"runs": [], "next_before_seq": None, "truncated": False},
        items_field="runs",
        cursor_field="next_before_seq",
    )
    next_before_seq = None if page.exhausted else page.items[-1]["seq"] if page.items else None
    return {
        "runs": list(page.items),
        "next_before_seq": next_before_seq,
        "truncated": page.truncated,
    }


class RunNotFound(Exception):
    """The ID does not resolve to a workspace-owned `_system/run` row."""

    def __init__(self, detail: str) -> None:
        self.code = "run_not_found"
        self.detail = detail
        super().__init__(detail)


_OUTPUT_COLUMNS = """
    id, seq, collection, collection_version, collection_hash, entity, key,
    type, status, content, enrichment_error, enriched_at, run_id, depth,
    derived_from, occurred_at, created_at
"""


async def fetch_run(
    pool: DatabasePool,
    *,
    workspace: str,
    run_id: UUID,
    query: RunOutputsQuery,
    settings: Settings,
) -> dict[str, Any]:
    """Return one expanded run row plus its ordered, paged output rows.

    Output order is exactly `content.output_ids`; rows removed by explicit
    erasure are reported in `missing_output_ids` instead of silently
    disappearing from the atomic review surface.
    """

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select id, seq, entity, content, depth, derived_from, created_at
            from record
            where workspace = %s and id = %s and collection = '_system' and type = 'run'
            """,
            (workspace, run_id),
        )
        run_row = await result.fetchone()
        if run_row is None:
            raise RunNotFound("run does not exist")
        content: dict[str, Any] = run_row["content"]
        raw_output_ids = content.get("output_ids")
        output_ids = [
            UUID(item) for item in (raw_output_ids if isinstance(raw_output_ids, list) else [])
        ]
        window = output_ids[query.output_offset : query.output_offset + query.output_limit]
        outputs_by_id: dict[UUID, dict[str, Any]] = {}
        if window:
            result = await conn.execute(
                f"select {_OUTPUT_COLUMNS} from record where workspace = %s and id = any(%s::uuid[])",
                (workspace, window),
            )
            outputs_by_id = {row["id"]: row for row in await result.fetchall()}

    run_detail = {
        "id": str(run_row["id"]),
        "seq": int(run_row["seq"]),
        "entity": run_row["entity"],
        "content": content,
        "depth": int(run_row["depth"]),
        "derived_from": [str(parent) for parent in run_row["derived_from"]],
        "created_at": timestamp(run_row["created_at"]),
    }
    envelope: dict[str, Any] = {
        "run": run_detail,
        "outputs": [],
        "missing_output_ids": [str(item) for item in window if item not in outputs_by_id],
        "output_count": len(output_ids),
        "next_output_offset": None,
        "truncated": False,
    }
    budget = settings.max_response_bytes - json_size(
        {**envelope, "next_output_offset": len(output_ids), "truncated": True}
    )
    if budget < 0:
        raise ResponseTooLarge("run content exceeds MAX_RESPONSE_BYTES; raise the bound")
    emitted: list[dict[str, Any]] = []
    used = 0
    truncated = False
    consumed = 0
    for index, output_id in enumerate(window):
        row = outputs_by_id.get(output_id)
        if row is None:
            consumed = index + 1
            continue
        item = record_version(row, include_entity=True)
        size = json_size(item) + (1 if emitted else 0)
        if used + size > budget:
            truncated = True
            break
        emitted.append(item)
        used += size
        consumed = index + 1
    next_offset = query.output_offset + consumed
    envelope["outputs"] = emitted
    envelope["missing_output_ids"] = [
        str(item) for item in window[:consumed] if item not in outputs_by_id
    ]
    envelope["next_output_offset"] = next_offset if next_offset < len(output_ids) else None
    envelope["truncated"] = truncated
    return envelope


__all__ = [
    "RunNotFound",
    "RunOutputsQuery",
    "RunsQuery",
    "fetch_run",
    "fetch_runs",
]
