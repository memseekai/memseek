"""Applying a processor to records that already exist.

Ordinary enrichment only ever reaches a record on its way in.  That makes
improving enrichment and keeping history mutually exclusive: a new processor has
to be bound to a collection version, and a new version starts empty.  A backfill
is the explicit, bounded way out — it names the target rather than deriving it
from bindings, which is what lets it reach a frozen version whose YAML must not
change.

What it deliberately does not do:

* overwrite anything.  Annotation names stay write-once; a row that already holds
  the annotation is skipped, so a backfill cannot rewrite history.
* run unbounded.  Every request carries a row budget, every claim does bounded
  work, and progress is durable so a restart resumes instead of restarting.
* bypass validation.  The rows go through the same computation, schema
  validation, and run-audit path as enrichment at ingest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.locks import acquire_workspace_lock

type BackfillState = Literal["queued", "running", "done", "cancelled", "failed"]

_LIVE_STATES = ("queued", "running")


class BackfillError(ValueError):
    """An invalid backfill request."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Backfill:
    """One durable backfill handle."""

    id: UUID
    workspace: str
    collection: str
    collection_version: int
    processor: str
    cursor_seq: int
    scanned: int
    annotated: int
    max_rows: int | None
    state: BackfillState
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @property
    def live(self) -> bool:
        return self.state in _LIVE_STATES

    def as_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "workspace": self.workspace,
            "collection": self.collection,
            "version": self.collection_version,
            "processor": self.processor,
            "state": self.state,
            "cursor_seq": self.cursor_seq,
            "scanned": self.scanned,
            "annotated": self.annotated,
            "max_rows": self.max_rows,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


def _backfill(row: Any) -> Backfill:
    return Backfill(
        id=row["id"],
        workspace=str(row["workspace"]),
        collection=str(row["collection"]),
        collection_version=int(row["collection_version"]),
        processor=str(row["processor"]),
        cursor_seq=int(row["cursor_seq"]),
        scanned=int(row["scanned"]),
        annotated=int(row["annotated"]),
        max_rows=None if row["max_rows"] is None else int(row["max_rows"]),
        state=row["state"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


_COLUMNS = """
id, workspace, collection, collection_version, processor, cursor_seq,
scanned, annotated, max_rows, state, last_error, created_at, updated_at, completed_at
"""


def validate_target(
    catalog: DefinitionCatalog,
    *,
    collection: str,
    version: int,
    processor: str,
) -> None:
    """Reject a backfill the catalog cannot actually perform.

    The collection version must exist, the processor must exist and admit that
    collection, and it must be a processor a server can compute — a ``client``
    source has no client present during a backfill, exactly as it has none during
    derivation output.
    """

    definition = catalog.collections.get((collection, version))
    if definition is None:
        raise BackfillError("unknown_collection", f"unknown collection {collection}@{version}")
    target = catalog.processors.get(processor)
    if target is None:
        raise BackfillError("unknown_processor", f"unknown processor {processor!r}")
    if collection not in target.input.collections:
        raise BackfillError(
            "processor_scope",
            f"processor {processor!r} does not admit collection {collection!r}",
        )
    if target.source == "client":
        raise BackfillError(
            "processor_source",
            f"processor {processor!r} takes client-supplied values and cannot be backfilled",
        )


async def request_backfill(
    pool: DatabasePool,
    *,
    workspace: str,
    collection: str,
    version: int,
    processor: str,
    catalog: DefinitionCatalog,
    max_rows: int | None = None,
) -> Backfill:
    """Register a backfill and enqueue its first claim-fenced job."""

    validate_target(catalog, collection=collection, version=version, processor=processor)
    if max_rows is not None and max_rows <= 0:
        raise BackfillError("max_rows", "max_rows must be positive when provided")
    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        existing = await conn.execute(
            f"""
            select {_COLUMNS} from backfill
            where workspace = %s and collection = %s and collection_version = %s
              and processor = %s and state = any(%s::text[])
            """,
            (workspace, collection, version, processor, list(_LIVE_STATES)),
        )
        row = await existing.fetchone()
        if row is not None:
            # One live backfill per target: returning the existing handle makes a
            # duplicate request idempotent instead of racing the same rows.
            raise BackfillError(
                "backfill_exists",
                f"a backfill for {collection}@{version}/{processor} is already "
                f"{row['state']} (id {row['id']})",
                status=409,
            )
        created = await conn.execute(
            f"""
            insert into backfill (workspace, collection, collection_version, processor, max_rows)
            values (%s, %s, %s, %s, %s)
            returning {_COLUMNS}
            """,
            (workspace, collection, version, processor, max_rows),
        )
        inserted = await created.fetchone()
        assert inserted is not None
        handle = _backfill(inserted)
        await _enqueue_job(conn, handle)
    return handle


async def _enqueue_job(conn: DatabaseConnection, handle: Backfill) -> None:
    await conn.execute(
        """
        insert into job (workspace, kind, payload, dedupe_key)
        values (%s, 'annotation_backfill', %s, %s)
        on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
        """,
        (
            handle.workspace,
            Jsonb({"backfill_id": str(handle.id)}),
            f"annotation_backfill:{handle.id}",
        ),
    )


async def get_backfill(pool: DatabasePool, *, workspace: str, backfill_id: UUID) -> Backfill:
    async with pool.connection() as conn:
        result = await conn.execute(
            f"select {_COLUMNS} from backfill where id = %s and workspace = %s",
            (backfill_id, workspace),
        )
        row = await result.fetchone()
    if row is None:
        raise BackfillError("not_found", "backfill does not exist", status=404)
    return _backfill(row)


async def list_backfills(
    pool: DatabasePool, *, workspace: str, limit: int = 50
) -> tuple[Backfill, ...]:
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            select {_COLUMNS} from backfill
            where workspace = %s
            order by created_at desc
            limit %s
            """,
            (workspace, limit),
        )
        return tuple(_backfill(row) for row in await result.fetchall())


async def cancel_backfill(pool: DatabasePool, *, workspace: str, backfill_id: UUID) -> Backfill:
    """Stop a live backfill. Annotations already written are kept — they are valid."""

    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        result = await conn.execute(
            f"""
            update backfill
            set state = 'cancelled', completed_at = clock_timestamp(),
                updated_at = clock_timestamp()
            where id = %s and workspace = %s and state = any(%s::text[])
            returning {_COLUMNS}
            """,
            (backfill_id, workspace, list(_LIVE_STATES)),
        )
        row = await result.fetchone()
        if row is None:
            current = await conn.execute(
                f"select {_COLUMNS} from backfill where id = %s and workspace = %s",
                (backfill_id, workspace),
            )
            existing = await current.fetchone()
            if existing is None:
                raise BackfillError("not_found", "backfill does not exist", status=404)
            raise BackfillError(
                "not_live",
                f"backfill is already {existing['state']}",
                status=409,
            )
        # The lane rechecks state before every batch, so a claimed job stops at
        # its next boundary rather than being interrupted mid-write.
        await conn.execute(
            """
            update job set done_at = clock_timestamp()
            where workspace = %s and kind = 'annotation_backfill'
              and payload ->> 'backfill_id' = %s
              and done_at is null and dead_at is null
            """,
            (workspace, str(backfill_id)),
        )
        return _backfill(row)


async def claim_state(pool: DatabasePool, *, workspace: str, backfill_id: UUID) -> Backfill | None:
    """Move a queued backfill into ``running`` and return it, or None if not live."""

    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            f"""
            update backfill
            set state = 'running', updated_at = clock_timestamp()
            where id = %s and workspace = %s and state = any(%s::text[])
            returning {_COLUMNS}
            """,
            (backfill_id, workspace, list(_LIVE_STATES)),
        )
        row = await result.fetchone()
    return None if row is None else _backfill(row)


async def record_progress(
    pool: DatabasePool,
    *,
    backfill_id: UUID,
    cursor_seq: int,
    scanned: int,
    annotated: int,
) -> Backfill:
    """Advance a backfill's durable cursor and counters after one batch."""

    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            f"""
            update backfill
            set cursor_seq = greatest(cursor_seq, %s),
                scanned = scanned + %s,
                annotated = annotated + %s,
                updated_at = clock_timestamp()
            where id = %s
            returning {_COLUMNS}
            """,
            (cursor_seq, scanned, annotated, backfill_id),
        )
        row = await result.fetchone()
    assert row is not None
    return _backfill(row)


async def finish(
    pool: DatabasePool,
    *,
    backfill_id: UUID,
    state: BackfillState,
    error: str | None = None,
) -> None:
    """Mark a backfill terminal. Only a live backfill can be finished."""

    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            update backfill
            set state = %s, last_error = %s, completed_at = clock_timestamp(),
                updated_at = clock_timestamp()
            where id = %s and state = any(%s::text[])
            """,
            (state, error, backfill_id, list(_LIVE_STATES)),
        )


async def rewind(pool: DatabasePool, *, backfill_id: UUID) -> Backfill:
    """Reset the cursor to the beginning for a confirming sweep.

    Row selection skips records another lane holds locked, and the cursor advances
    past them, so reaching the end of a sweep does not prove the work is finished.
    Rewinding costs one filtered scan and is what lets ``done`` mean done.
    """

    async with pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            f"""
            update backfill
            set cursor_seq = 0, updated_at = clock_timestamp()
            where id = %s
            returning {_COLUMNS}
            """,
            (backfill_id,),
        )
        row = await result.fetchone()
    assert row is not None
    return _backfill(row)


__all__ = [
    "Backfill",
    "BackfillError",
    "BackfillState",
    "cancel_backfill",
    "claim_state",
    "finish",
    "get_backfill",
    "list_backfills",
    "record_progress",
    "request_backfill",
    "rewind",
    "validate_target",
]
