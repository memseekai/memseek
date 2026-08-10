"""Scope-hashed change feed and monotonic consumer cursors."""

from __future__ import annotations

from typing import Any, Literal, LiteralString

from pydantic import Field, field_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import sha256_canonical
from memseek.views.shared import FrozenQueryModel, bound_page, record_version, split_names


class DeltaQuery(FrozenQueryModel):
    """Validated ``GET /delta`` parameters; ``entity='*'`` spans all entities."""

    consumer: str = Field(min_length=1, max_length=128)
    entity: str = Field(default="*", min_length=1, max_length=255)
    collections: tuple[str, ...] | None = None
    status: Literal["active", "draft", "all"] = "active"
    include_system: bool = False
    limit: int = Field(default=500, ge=1, le=500)

    normalize_collections = field_validator("collections", mode="before")(split_names)


class CursorRequest(FrozenQueryModel):
    """Validated ``POST /cursor`` body."""

    consumer: str = Field(min_length=1, max_length=128)
    entity: str = Field(default="*", min_length=1, max_length=255)
    position: int = Field(ge=0, le=2**63 - 1)
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    force: bool = False


class CursorScopeMismatch(Exception):
    """The stored cursor was created under different visibility filters."""

    def __init__(self, detail: str) -> None:
        self.code = "cursor_scope_mismatch"
        self.detail = detail
        super().__init__(detail)


class CursorRegression(Exception):
    """A non-forced update tried to move a cursor backward."""

    def __init__(self, detail: str) -> None:
        self.code = "cursor_regression"
        self.detail = detail
        super().__init__(detail)


def delta_scope_hash(
    *,
    entity: str,
    collections: tuple[str, ...] | None,
    status: str,
    include_system: bool,
) -> str:
    """Canonicalize the visibility filters into one stable scope identity."""

    return sha256_canonical(
        {
            "entity": entity,
            "collections": sorted(set(collections)) if collections is not None else None,
            "status": status,
            "include_system": include_system,
        }
    )


async def fetch_delta(
    pool: DatabasePool,
    *,
    workspace: str,
    query: DeltaQuery,
    settings: Settings,
) -> dict[str, Any]:
    """Read matching rows in ascending ``seq`` without advancing the cursor.

    Unready rows and tombstones are included because a cache consumer needs
    both to stay coherent.  A stored cursor with a different scope hash fails
    instead of silently changing what the position means.
    """

    scope_hash = delta_scope_hash(
        entity=query.entity,
        collections=query.collections,
        status=query.status,
        include_system=query.include_system,
    )
    clauses: list[LiteralString] = ["workspace = %s", "seq > %s"]
    async with pool.connection() as conn:
        result = await conn.execute(
            "select position, scope_hash from cursor"
            " where workspace = %s and consumer = %s and entity = %s",
            (workspace, query.consumer, query.entity),
        )
        cursor_row = await result.fetchone()
        if cursor_row is not None and cursor_row["scope_hash"] != scope_hash:
            raise CursorScopeMismatch(
                f"consumer {query.consumer!r} stores a cursor for a different scope; "
                "use a different consumer name or reset explicitly with force=true"
            )
        position = int(cursor_row["position"]) if cursor_row is not None else 0
        params: list[Any] = [workspace, position]
        if query.entity != "*":
            clauses.append("entity = %s")
            params.append(query.entity)
        if query.collections is not None:
            clauses.append("collection = any(%s::text[])")
            params.append(list(query.collections))
        if query.status != "all":
            clauses.append("status = %s")
            params.append(query.status)
        if not query.include_system:
            clauses.append("collection <> '_system'")
        params.append(query.limit + 1)
        rows_result = await conn.execute(
            f"""
            select id, seq, collection, collection_version, collection_hash, entity,
                   key, type, status, content, run_id, derived_from, depth,
                   enriched_at, enrichment_error, occurred_at, created_at
            from record
            where {" and ".join(clauses)}
            order by seq
            limit %s
            """,
            params,
        )
        rows = await rows_result.fetchall()
    page = bound_page(
        [record_version(row, include_entity=True) for row in rows],
        limit=query.limit,
        max_bytes=settings.max_response_bytes,
        envelope={"records": [], "next_cursor": None, "scope_hash": scope_hash, "truncated": False},
        items_field="records",
        cursor_field="next_cursor",
    )
    next_cursor = page.items[-1]["seq"] if page.items else position
    return {
        "records": list(page.items),
        "next_cursor": next_cursor,
        "scope_hash": scope_hash,
        "truncated": page.truncated,
    }


async def upsert_cursor(
    pool: DatabasePool,
    *,
    workspace: str,
    request: CursorRequest,
) -> dict[str, Any]:
    """Monotonically advance one scope-bound consumer cursor.

    Cursor updates are independent of record reads.  A forced update is the
    explicit reset path: it sets the supplied position and new scope hash.
    """

    if request.force:
        update = "position = excluded.position, scope_hash = excluded.scope_hash"
        guard = ""
    else:
        update = "position = excluded.position"
        guard = (
            " where cursor.scope_hash = excluded.scope_hash"
            " and cursor.position <= excluded.position"
        )
    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            f"""
            insert into cursor (workspace, consumer, entity, position, scope_hash)
            values (%s, %s, %s, %s, %s)
            on conflict (workspace, consumer, entity)
            do update set {update}, updated_at = now(){guard}
            returning consumer, entity, position, scope_hash, updated_at
            """,
            (workspace, request.consumer, request.entity, request.position, request.scope_hash),
        )
        row = await result.fetchone()
        if row is None:
            stored = await conn.execute(
                "select position, scope_hash from cursor"
                " where workspace = %s and consumer = %s and entity = %s",
                (workspace, request.consumer, request.entity),
            )
            stored_row = await stored.fetchone()
            if stored_row is None:
                raise RuntimeError("cursor row disappeared during a conditional upsert")
            if stored_row["scope_hash"] != request.scope_hash:
                raise CursorScopeMismatch(
                    "cursor scope changed; resend with force=true to reset explicitly"
                )
            raise CursorRegression(
                f"cursor position {int(stored_row['position'])} cannot move backward "
                f"to {request.position} without force=true"
            )
    return {
        "consumer": row["consumer"],
        "entity": row["entity"],
        "position": int(row["position"]),
        "scope_hash": row["scope_hash"],
        "updated_at": row["updated_at"].isoformat(),
    }


__all__ = [
    "CursorRegression",
    "CursorRequest",
    "CursorScopeMismatch",
    "DeltaQuery",
    "delta_scope_hash",
    "fetch_delta",
    "upsert_cursor",
]
