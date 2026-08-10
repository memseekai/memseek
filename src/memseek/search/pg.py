"""PostgreSQL candidate generation over the canonical record table.

PostgreSQL is canonical, so durable projection writes and deletes are no-ops.
Candidate ordering here is a recall optimization: the core engine reloads the
returned IDs, rechecks every predicate, recomputes exact signals, and applies
the canonical rank or ``order_by`` before anything is returned to a caller.
"""

from __future__ import annotations

from typing import Any, ClassVar, LiteralString
from uuid import UUID

from memseek.config import Settings
from memseek.search.registry import CandidateHit, CandidateQuery, SearchCapability
from memseek.search.scope import field_value_expression, pushdown_predicate, scope_conditions


class PostgresSearchBackend:
    """Candidate channels backed by the canonical HNSW/tsvector/seq indexes."""

    NAME: ClassVar[str] = "pg"
    CAPS: ClassVar[frozenset[SearchCapability]] = frozenset(
        {"vector", "text", "recent", "structured"}
    )

    async def candidates(
        self,
        cfg: Settings,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        qvec: list[float] | None,
    ) -> list[CandidateHit]:
        del cfg
        source = query.source
        cap = source.candidates
        if source.mode == "structured":
            ids = await self._structured_channel(conn, workspace, query, cap)
            return [CandidateHit(id=item, channel="structured") for item in ids]
        channels: list[tuple[str, list[UUID]]] = []
        if source.mode in {"vector", "hybrid"}:
            if qvec is None:
                raise ValueError("vector candidate generation requires a query embedding")
            channels.append(
                ("vector", await self._vector_channel(conn, workspace, query, qvec, cap))
            )
        if source.mode in {"text", "hybrid"}:
            channels.append(("text", await self._text_channel(conn, workspace, query, cap)))
        if source.mode in {"recent", "hybrid"}:
            channels.append(("recent", await self._recent_channel(conn, workspace, query, cap)))
        return _round_robin_union(channels, cap)

    async def upsert(self, cfg: Settings, rows: list[dict[str, Any]]) -> None:
        del cfg, rows

    async def delete(self, cfg: Settings, workspace: str, rows: list[dict[str, Any]]) -> None:
        del cfg, workspace, rows

    async def _vector_channel(
        self,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        qvec: list[float],
        cap: int,
    ) -> list[UUID]:
        clauses, params = scope_conditions(query.source, workspace)
        clauses.append("row.embedding is not null")
        if query.embedding_space is None:
            raise ValueError("vector candidate generation requires a resolved embedding space")
        clauses.append("row.embedding_space = %s")
        params.append(query.embedding_space)
        vector_text = "[" + ",".join(map(str, qvec)) + "]"
        result = await conn.execute(
            f"""
            select row.id from record row
            where {" and ".join(clauses)}
            order by row.embedding <=> %s::vector, row.seq desc
            limit %s
            """,
            [*params, vector_text, cap],
        )
        return [item["id"] for item in await result.fetchall()]

    async def _text_channel(
        self,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        cap: int,
    ) -> list[UUID]:
        clauses, params = scope_conditions(query.source, workspace)
        clauses.append(
            "websearch_to_tsquery('english', %s) @@ to_tsvector('english', row.content->>'text')"
        )
        params.append(query.query)
        result = await conn.execute(
            f"""
            select row.id from record row
            where {" and ".join(clauses)}
            order by ts_rank_cd(
                to_tsvector('english', row.content->>'text'),
                websearch_to_tsquery('english', %s)
            ) desc, row.seq desc
            limit %s
            """,
            [*params, query.query, cap],
        )
        return [item["id"] for item in await result.fetchall()]

    async def _recent_channel(
        self,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        cap: int,
    ) -> list[UUID]:
        clauses, params = scope_conditions(query.source, workspace)
        result = await conn.execute(
            f"""
            select row.id from record row
            where {" and ".join(clauses)}
            order by row.occurred_at desc, row.seq desc
            limit %s
            """,
            [*params, cap],
        )
        return [item["id"] for item in await result.fetchall()]

    async def _structured_channel(
        self,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        cap: int,
    ) -> list[UUID]:
        source = query.source
        clauses, params = scope_conditions(source, workspace)
        for name, predicate in source.where.items():
            versions = query.field_versions.get(name)
            if versions is None:
                continue
            for operator, operand in predicate.items():
                pushed = pushdown_predicate(versions, operator, operand)
                if pushed is not None:
                    clause, clause_params = pushed
                    clauses.append(clause)
                    params.extend(clause_params)
        order_terms: list[LiteralString] = []
        for order in source.order_by:
            versions = query.field_versions.get(order.field)
            if versions is None:
                continue
            value_sql, value_params = field_value_expression(versions)
            direction: LiteralString = "desc" if order.direction == "desc" else "asc"
            order_terms.append(f"{value_sql} {direction} nulls last")
            params.extend(value_params)
        order_terms.append("row.seq")
        result = await conn.execute(
            f"""
            select row.id from record row
            where {" and ".join(clauses)}
            order by {", ".join(order_terms)}
            limit %s
            """,
            [*params, cap],
        )
        return [item["id"] for item in await result.fetchall()]


def _round_robin_union(channels: list[tuple[str, list[UUID]]], cap: int) -> list[CandidateHit]:
    """Deterministically interleave channel lists, deduplicated, up to ``cap``."""

    seen: set[UUID] = set()
    hits: list[CandidateHit] = []
    for index in range(max((len(ids) for _, ids in channels), default=0)):
        for channel, ids in channels:
            if index >= len(ids):
                continue
            candidate = ids[index]
            if candidate in seen:
                continue
            seen.add(candidate)
            hits.append(CandidateHit(id=candidate, channel=channel))
            if len(hits) >= cap:
                return hits
    return hits
