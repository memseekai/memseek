"""Bounded workspace entity discovery for operational clients."""

from __future__ import annotations

from typing import Any, LiteralString

from pydantic import Field

from memseek.db import DatabasePool
from memseek.views.shared import FrozenQueryModel, timestamp


class EntitiesQuery(FrozenQueryModel):
    """List real entities with recent non-system activity first."""

    q: str | None = Field(default=None, min_length=1, max_length=255)
    limit: int = Field(default=100, ge=1, le=100)


async def fetch_entities(
    pool: DatabasePool,
    *,
    workspace: str,
    query: EntitiesQuery,
) -> dict[str, Any]:
    """Return a small, searchable index without exposing system run entities."""

    clauses: list[LiteralString] = ["workspace = %s", "collection <> '_system'"]
    params: list[Any] = [workspace]
    if query.q is not None:
        clauses.append("entity ilike %s")
        params.append(f"%{query.q}%")
    params.append(query.limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            select entity, count(*) as record_count, max(seq) as last_seq,
                   max(created_at) as last_seen
            from record
            where {" and ".join(clauses)}
            group by entity
            order by max(seq) desc, entity asc
            limit %s
            """,
            params,
        )
        rows = await result.fetchall()
    return {
        "entities": [
            {
                "entity": row["entity"],
                "record_count": int(row["record_count"]),
                "last_seq": int(row["last_seq"]),
                "last_seen": timestamp(row["last_seen"]),
            }
            for row in rows
        ]
    }


__all__ = ["EntitiesQuery", "fetch_entities"]
