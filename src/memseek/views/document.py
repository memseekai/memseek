"""Current keyed document, per-key version history, and freshness reporting."""

from __future__ import annotations

from typing import Any, Literal, LiteralString

from pydantic import Field, field_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.freshness import compute_freshness, request_revalidation
from memseek.views.shared import (
    FrozenQueryModel,
    belief_view,
    bound_page,
    json_size,
    record_version,
    retraction_view,
    split_names,
)


class DocumentQuery(FrozenQueryModel):
    """Validated ``GET /document`` parameters.

    ``max_staleness`` is validated and forwarded to the M5 revalidation seam;
    M2 never enqueues a derivation from a read.
    """

    entity: str = Field(min_length=1, max_length=255)
    collections: tuple[str, ...] | None = None
    status: Literal["active", "draft"] = "active"
    max_staleness: float | None = Field(default=None, ge=0)

    normalize_collections = field_validator("collections", mode="before")(split_names)


class HistoryQuery(FrozenQueryModel):
    """Validated ``GET /document/history`` parameters.

    Collection is required because key identity is collection-scoped.
    """

    entity: str = Field(min_length=1, max_length=255)
    collection: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=100, ge=1, le=100)
    before_seq: int | None = Field(default=None, ge=1)


class DocumentTooLarge(Exception):
    """The complete current-state document exceeds a configured bound."""

    def __init__(self, detail: str) -> None:
        self.code = "document_too_large"
        self.detail = detail
        super().__init__(detail)


async def build_document(
    pool: DatabasePool,
    *,
    workspace: str,
    query: DocumentQuery,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Assemble latest-per-key current state, retractions, and freshness.

    Current keyed state is read-visible immediately after its insert
    transaction; readiness governs search and trigger visibility only, so no
    ``enriched_at`` filter appears here.  The document is never partial: an
    over-limit current-state set fails with a narrowing instruction instead.
    """

    clauses: list[LiteralString] = [
        "workspace = %s",
        "entity = %s",
        "key is not null",
        "status = %s",
    ]
    params: list[Any] = [workspace, query.entity, query.status]
    if query.collections is not None:
        clauses.append("collection = any(%s::text[])")
        params.append(list(query.collections))
    params.append(settings.max_document_records + 1)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            select distinct on (collection, key)
                   id, seq, collection, collection_version, collection_hash, key,
                   type, status, content, run_id, derived_from, depth,
                   enriched_at, enrichment_error, occurred_at, created_at
            from record
            where {" and ".join(clauses)}
            order by collection, key, seq desc
            limit %s
            """,
            params,
        )
        rows = await result.fetchall()
        if len(rows) > settings.max_document_records:
            raise DocumentTooLarge(
                "current keyed state exceeds MAX_DOCUMENT_RECORDS="
                f"{settings.max_document_records}; narrow the collection scope"
            )
        freshness = await compute_freshness(
            conn,
            workspace=workspace,
            entity=query.entity,
            catalog=catalog,
        )
        freshness = await request_revalidation(
            conn,
            workspace=workspace,
            entity=query.entity,
            catalog=catalog,
            freshness=freshness,
            max_staleness=query.max_staleness,
        )
    beliefs = [belief_view(row) for row in rows if not row["content"].get("tombstone", False)]
    retractions = [retraction_view(row) for row in rows if row["content"].get("tombstone", False)]
    document = {
        "entity": query.entity,
        "status": query.status,
        "beliefs": beliefs,
        "retractions": retractions,
        "freshness": [entry.as_json() for entry in freshness],
    }
    if json_size(document) > settings.max_response_bytes:
        raise DocumentTooLarge(
            "serialized document exceeds MAX_RESPONSE_BYTES="
            f"{settings.max_response_bytes}; narrow the collection scope"
        )
    return document


async def fetch_history(
    pool: DatabasePool,
    *,
    workspace: str,
    query: HistoryQuery,
    settings: Settings,
) -> dict[str, Any]:
    """Return every version of one collection-scoped key, newest first.

    Active rows, drafts, tombstones, and promotion copies all appear because
    history is the audit view of the keyed lane.
    """

    clauses: list[LiteralString] = [
        "workspace = %s",
        "entity = %s",
        "collection = %s",
        "key = %s",
    ]
    params: list[Any] = [workspace, query.entity, query.collection, query.key]
    if query.before_seq is not None:
        clauses.append("seq < %s")
        params.append(query.before_seq)
    params.append(query.limit + 1)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            select id, seq, collection, collection_version, collection_hash, key,
                   type, status, content, run_id, derived_from, depth,
                   enriched_at, enrichment_error, occurred_at, created_at
            from record
            where {" and ".join(clauses)}
            order by seq desc
            limit %s
            """,
            params,
        )
        rows = await result.fetchall()
    page = bound_page(
        [record_version(row) for row in rows],
        limit=query.limit,
        max_bytes=settings.max_response_bytes,
        envelope={"versions": [], "next_before_seq": None, "truncated": False},
        items_field="versions",
        cursor_field="next_before_seq",
    )
    next_before_seq = page.items[-1]["seq"] if page.items and not page.exhausted else None
    return {
        "versions": list(page.items),
        "next_before_seq": next_before_seq,
        "truncated": page.truncated,
    }


__all__ = ["DocumentQuery", "DocumentTooLarge", "HistoryQuery", "build_document", "fetch_history"]
