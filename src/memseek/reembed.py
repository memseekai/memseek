"""Changing the embedding model without losing vector recall.

Embeddings from two different models are not comparable, so a deployment declares
which space its vectors belong to and vector search only reads that space.  That
makes the embedding model the one piece of a catalog that used to be frozen for
the life of a workspace: replacing it in place would silently compare vectors
that mean different things, and there was nowhere to put replacements while the
originals still served reads.

The path here is prepare, verify, cut over, and — if needed — roll back:

1. ``reembed`` embeds existing records into a *staged* space, in bounded passes,
   while the active space keeps serving every read unchanged.
2. ``coverage`` reports how much of the corpus the staged space holds, so cutover
   happens on a fact rather than a guess.
3. ``cutover_space`` promotes the staged space in bounded transactions, staging
   the outgoing vectors as it goes so the previous space remains complete.
4. Because the outgoing space is staged, a cutover is reversible by cutting over
   back to it.

Nothing here mutates record content, and no vector is ever discarded.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.llm.runtime import embed
from memseek.locks import acquire_workspace_lock

_SPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CUTOVER_BATCH = 1_000


class ReembedError(ValueError):
    """An invalid or unsafe re-embed request."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class SpaceCoverage:
    """How complete a staged space is for one workspace."""

    workspace: str
    space: str
    embedded_records: int
    staged: int
    remaining: int

    @property
    def complete(self) -> bool:
        return self.remaining == 0 and self.embedded_records > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "space": self.space,
            "embedded_records": self.embedded_records,
            "staged": self.staged,
            "remaining": self.remaining,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class ReembedResult:
    workspace: str
    space: str
    embedded: int
    failed: int
    coverage: SpaceCoverage

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "space": self.space,
            "embedded": self.embedded,
            "failed": self.failed,
            "coverage": self.coverage.as_json(),
        }


@dataclass(frozen=True, slots=True)
class CutoverResult:
    workspace: str
    space: str
    previous_space: str | None
    promoted: int
    staged_previous: int

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "space": self.space,
            "previous_space": self.previous_space,
            "promoted": self.promoted,
            "staged_previous": self.staged_previous,
            "next_step": (
                f"set embedding.space to {self.space!r} in conf/models.yaml so new "
                "records and vector search use the promoted space"
            ),
        }


def _validate_space(space: str) -> str:
    if not _SPACE_RE.fullmatch(space):
        raise ReembedError("space", f"invalid embedding space identifier {space!r}")
    return space


async def coverage(pool: DatabasePool, *, workspace: str, space: str) -> SpaceCoverage:
    """Report how many embedded records the staged space still lacks."""

    _validate_space(space)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select
              count(*) filter (where record.embedding is not null) as embedded,
              count(*) filter (
                where record.embedding is not null and staged.record_id is not null
              ) as staged,
              count(*) filter (
                where record.embedding is not null and staged.record_id is null
                  and record.embedding_space is distinct from %s
              ) as remaining
            from record
            left join record_embedding staged
              on staged.record_id = record.id and staged.space = %s
            where record.workspace = %s and record.collection <> '_system'
            """,
            (space, space, workspace),
        )
        row = await result.fetchone()
    assert row is not None
    return SpaceCoverage(
        workspace=workspace,
        space=space,
        embedded_records=int(row["embedded"]),
        staged=int(row["staged"]),
        remaining=int(row["remaining"]),
    )


async def _pending_rows(
    conn: DatabaseConnection, *, workspace: str, space: str, limit: int
) -> list[tuple[Any, str]]:
    """Rows that carry a vector in some other space but none in the target space."""

    result = await conn.execute(
        """
        select record.id, record.content ->> 'text' as text
        from record
        left join record_embedding staged
          on staged.record_id = record.id and staged.space = %s
        where record.workspace = %s
          and record.collection <> '_system'
          and record.embedding is not null
          and record.embedding_space is distinct from %s
          and staged.record_id is null
        order by record.seq
        limit %s
        """,
        (space, workspace, space, limit),
    )
    return [(row["id"], str(row["text"] or "")) for row in await result.fetchall()]


async def reembed(
    pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    *,
    workspace: str,
    space: str,
    max_rows: int | None = None,
) -> ReembedResult:
    """Embed existing records into a staged space using the catalog's embedding model.

    The active space is untouched, so reads keep working at full recall for the
    whole pass.  A transport failure leaves the row unstaged rather than staging a
    default vector: an incomplete space must stay visibly incomplete, because
    cutover is gated on coverage.
    """

    _validate_space(space)
    embedding_model = catalog.models.embedding
    if space == embedding_model.space:
        raise ReembedError(
            "space_active",
            f"{space!r} is already the active space; stage a new space id instead",
        )
    if max_rows is not None and max_rows <= 0:
        raise ReembedError("max_rows", "max_rows must be positive when provided")

    embedded = 0
    failed = 0
    budget = max_rows
    batch_size = embedding_model.batch
    while budget is None or budget > 0:
        limit = batch_size if budget is None else min(batch_size, budget)
        async with pool.connection() as conn:
            pending = await _pending_rows(conn, workspace=workspace, space=space, limit=limit)
        if not pending:
            break
        texts = [
            text[: embedding_model.max_text_chars] if text else " " for _record_id, text in pending
        ]
        try:
            resolved = await embed(settings, catalog, texts, context=f"reembed:{space}")
        except Exception:
            # Leave the page unstaged: a partially staged space must never look
            # complete, and the next pass retries exactly these rows.
            failed += len(pending)
            break
        async with pool.connection() as conn, conn.transaction():
            for index, (record_id, _text) in enumerate(pending):
                await conn.execute(
                    """
                    insert into record_embedding (record_id, space, embedding, resolved)
                    values (%s, %s, %s, %s)
                    on conflict (record_id, space) do nothing
                    """,
                    (
                        record_id,
                        space,
                        _vector_literal(resolved.embedding.vectors[index]),
                        resolved.resolved,
                    ),
                )
        embedded += len(pending)
        if budget is not None:
            budget -= len(pending)
    return ReembedResult(
        workspace=workspace,
        space=space,
        embedded=embedded,
        failed=failed,
        coverage=await coverage(pool, workspace=workspace, space=space),
    )


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(map(str, vector)) + "]"


async def cutover_space(
    pool: DatabasePool,
    *,
    workspace: str,
    space: str,
    force: bool = False,
) -> CutoverResult:
    """Promote a staged space into ``record.embedding``.

    Refuses an incomplete space unless forced, because promoting a partial space
    silently drops the unstaged records out of vector recall.  The outgoing vector
    is staged under its own space first, so the previous space stays complete and
    the cutover can be reversed by cutting over back to it.
    """

    _validate_space(space)
    async with pool.connection() as conn:
        remaining_result = await conn.execute(
            """
            select count(*) as remaining
            from record
            left join record_embedding staged
              on staged.record_id = record.id and staged.space = %s
            where record.workspace = %s and record.collection <> '_system'
              and record.embedding is not null
              and record.embedding_space is distinct from %s
              and staged.record_id is null
            """,
            (space, workspace, space),
        )
        remaining_row = await remaining_result.fetchone()
        remaining = int(remaining_row["remaining"]) if remaining_row else 0
        previous_result = await conn.execute(
            """
            select distinct embedding_space
            from record
            where workspace = %s and embedding_space is not null and embedding_space <> %s
            limit 2
            """,
            (workspace, space),
        )
        previous_spaces = [str(row["embedding_space"]) for row in await previous_result.fetchall()]
    if remaining and not force:
        raise ReembedError(
            "incomplete_space",
            f"{remaining} record(s) have no vector staged in {space!r}; finish the "
            "re-embed or pass force to promote a partial space",
            status=409,
        )

    promoted = 0
    staged_previous = 0
    while True:
        async with pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, workspace)
            selected = await conn.execute(
                """
                select record.id, record.embedding_space, staged.embedding, staged.resolved
                from record
                join record_embedding staged
                  on staged.record_id = record.id and staged.space = %s
                where record.workspace = %s
                  and record.embedding_space is distinct from %s
                order by record.seq
                limit %s
                """,
                (space, workspace, space, _CUTOVER_BATCH),
            )
            rows = await selected.fetchall()
            if not rows:
                break
            for row in rows:
                # Stage the outgoing vector under its own space before replacing
                # it, so the previous space remains complete and reversible.
                if row["embedding_space"]:
                    outgoing = await conn.execute(
                        """
                        insert into record_embedding (record_id, space, embedding, resolved)
                        select id, embedding_space, embedding,
                               coalesce(
                                 enrichment_meta #>> '{embedding,resolved}',
                                 enrichment_meta #>> '{embedding,provider_model}',
                                 'unknown'
                               )
                        from record
                        where id = %s and embedding is not null
                        on conflict (record_id, space) do nothing
                        """,
                        (row["id"],),
                    )
                    staged_previous += int(outgoing.rowcount or 0)
                await conn.execute(
                    """
                    update record
                    set embedding = %s,
                        embedding_space = %s,
                        enrichment_meta = jsonb_set(
                          enrichment_meta,
                          '{embedding}',
                          coalesce(enrichment_meta -> 'embedding', '{}'::jsonb)
                            || %s::jsonb,
                          true
                        )
                    where id = %s
                    """,
                    (
                        row["embedding"],
                        space,
                        Jsonb({"space": space, "resolved": row["resolved"]}),
                        row["id"],
                    ),
                )
                promoted += 1
    return CutoverResult(
        workspace=workspace,
        space=space,
        previous_space=previous_spaces[0] if len(previous_spaces) == 1 else None,
        promoted=promoted,
        staged_previous=staged_previous,
    )


__all__ = [
    "CutoverResult",
    "ReembedError",
    "ReembedResult",
    "SpaceCoverage",
    "coverage",
    "cutover_space",
    "reembed",
]
