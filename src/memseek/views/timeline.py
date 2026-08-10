"""Compact newest-first entity timeline."""

from __future__ import annotations

from typing import Any, Literal, LiteralString

from pydantic import Field, field_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.views.shared import FrozenQueryModel, bound_page, record_summary, split_names


class TimelineQuery(FrozenQueryModel):
    """Validated ``GET /timeline`` parameters."""

    entity: str = Field(min_length=1, max_length=255)
    collections: tuple[str, ...] | None = None
    types: tuple[str, ...] | None = None
    status: Literal["active", "draft", "all"] = "active"
    limit: int = Field(default=100, ge=1, le=100)
    before_seq: int | None = Field(default=None, ge=1)
    include_system: bool = False

    normalize_collections = field_validator("collections", mode="before")(split_names)
    normalize_types = field_validator("types", mode="before")(split_names)


async def fetch_timeline(
    pool: DatabasePool,
    *,
    workspace: str,
    query: TimelineQuery,
    settings: Settings,
) -> dict[str, Any]:
    """Return compact rows newest-first by ``seq`` with resumable pagination."""

    clauses: list[LiteralString] = ["workspace = %s", "entity = %s"]
    params: list[Any] = [workspace, query.entity]
    if query.collections is not None:
        clauses.append("collection = any(%s::text[])")
        params.append(list(query.collections))
    if query.types is not None:
        clauses.append("type = any(%s::text[])")
        params.append(list(query.types))
    if query.status != "all":
        clauses.append("status = %s")
        params.append(query.status)
    if not query.include_system:
        clauses.append("collection <> '_system'")
    if query.before_seq is not None:
        clauses.append("seq < %s")
        params.append(query.before_seq)
    params.append(query.limit + 1)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            select id, seq, collection, key, type, status, content, enriched_at,
                   run_id, occurred_at, created_at
            from record
            where {" and ".join(clauses)}
            order by seq desc
            limit %s
            """,
            params,
        )
        rows = await result.fetchall()
    page = bound_page(
        [record_summary(row) for row in rows],
        limit=query.limit,
        max_bytes=settings.max_response_bytes,
        envelope={"records": [], "next_before_seq": None, "truncated": False},
        items_field="records",
        cursor_field="next_before_seq",
    )
    next_before_seq = page.items[-1]["seq"] if page.items and not page.exhausted else None
    return {
        "records": list(page.items),
        "next_before_seq": next_before_seq,
        "truncated": page.truncated,
    }


__all__ = ["TimelineQuery", "fetch_timeline"]
