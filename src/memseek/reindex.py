"""Bounded, canonical projection rebuild planning.

Reindex never mutates records.  It snapshots canonical ready identities under
the workspace mutation lock and emits ordinary claim-fenced projection jobs;
the worker remains the only component that performs external I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.locks import acquire_workspace_lock
from memseek.projections import ProjectionTarget, _enqueue_projection_tx

_BATCH = 500


class ReindexError(ValueError):
    """Invalid or unsafe reindex request."""


@dataclass(frozen=True, slots=True)
class ReindexResult:
    workspace: str
    mode: str
    target_count: int
    enqueued_jobs: int

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "mode": self.mode,
            "target_count": self.target_count,
            "enqueued_jobs": self.enqueued_jobs,
        }


async def reindex(
    pool: DatabasePool,
    *,
    workspace: str,
    settings: Settings,
    catalog: DefinitionCatalog,
    since_seq: int | None = None,
    reset: bool = False,
    confirm: bool = False,
) -> ReindexResult:
    """Queue a deterministic projection rebuild for one workspace."""

    # Both callers — the HTTP route and the CLI — surface these verbatim, so the
    # wording names the arguments rather than one transport's flags.
    if (reset and since_seq is not None) or (not reset and since_seq is None):
        raise ReindexError("choose exactly one of since_seq or reset")
    if since_seq is not None and since_seq < 0:
        raise ReindexError("since_seq must be non-negative")
    if reset and not confirm and "test" not in settings.database_url.rsplit("/", 1)[-1].lower():
        raise ReindexError("reset requires confirm (--yes on the CLI) outside a test database")
    del catalog  # The worker resolves immutable collection bindings at execution time.

    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace, exclusive=True)
        exists = await (
            await conn.execute(
                "select exists (select 1 from workspace where id = %s) as present", (workspace,)
            )
        ).fetchone()
        if not exists or not exists["present"]:
            raise ReindexError(f"workspace does not exist: {workspace}")

        if reset:
            result = await conn.execute(
                """
                select id, collection
                from record
                where workspace = %s and enriched_at is not null
                order by seq
                """,
                (workspace,),
            )
        else:
            result = await conn.execute(
                """
                with touched as (
                  select distinct entity, collection, key, status
                  from record
                  where workspace = %s and seq >= %s and key is not null
                ), ids as (
                  select row.id, row.collection
                  from record row
                  where row.workspace = %s
                    and row.enriched_at is not null
                    and row.seq >= %s
                  union
                  select row.id, row.collection
                  from record row
                  join touched on touched.entity = row.entity
                              and touched.collection = row.collection
                              and touched.key = row.key
                              and touched.status = row.status
                  where row.workspace = %s
                    and row.enriched_at is not null
                    and row.seq < %s
                    and row.id = (
                      select prior.id
                      from record prior
                      where prior.workspace = row.workspace
                        and prior.entity = row.entity
                        and prior.collection = row.collection
                        and prior.key = row.key
                        and prior.status = row.status
                        and prior.enriched_at is not null
                        and prior.seq < %s
                      order by prior.seq desc
                      limit 1
                    )
                )
                select distinct id, collection from ids order by id
                """,
                (workspace, since_seq, workspace, since_seq, workspace, since_seq, since_seq),
            )
        rows = await result.fetchall()
        targets = tuple(
            ProjectionTarget(UUID(str(row["id"])), str(row["collection"])) for row in rows
        )
        jobs = 0
        for offset in range(0, len(targets), _BATCH):
            if await _enqueue_projection_tx(
                conn,
                workspace=workspace,
                kind="index_upsert",
                targets=targets[offset : offset + _BATCH],
            ):
                jobs += 1

    return ReindexResult(
        workspace=workspace,
        mode="reset" if reset else "incremental",
        target_count=len(targets),
        enqueued_jobs=jobs,
    )


__all__ = ["ReindexError", "ReindexResult", "reindex"]
