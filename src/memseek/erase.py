"""Transactional provenance erasure and projection invalidation.

Erasure is the one destructive canonical operation.  It takes the exclusive
workspace mutation lock, computes a bounded recursive descendant closure, fences
active derive jobs, removes the closure, and leaves a durable index-delete job
plus a content-free audit record behind.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memseek.canonical_records import CanonicalRecordWrite, insert_canonical_record_tx
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION
from memseek.locks import acquire_entity_locks, acquire_workspace_lock
from memseek.logging import log_event

_MAX_ERASURE_ROWS = 10_000
_AUDIT_ENTITY = "_audit"
LOGGER = logging.getLogger(__name__)


class ErasureRequest(BaseModel):
    """Exactly one entity or explicit record-ID erasure selector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: str | None = Field(default=None, min_length=1, max_length=255)
    record_ids: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> ErasureRequest:
        if (self.entity is None) == (self.record_ids is None):
            raise ValueError("exactly one of entity or record_ids is required")
        if self.entity == "*" or (self.entity is not None and not self.entity.strip()):
            raise ValueError("entity must be non-blank and cannot be '*'")
        if self.record_ids is not None and len(set(self.record_ids)) != len(self.record_ids):
            raise ValueError("record_ids must not contain duplicates")
        return self


class ErasureError(RuntimeError):
    """Expected erasure failure mapped to a stable HTTP response."""

    def __init__(self, code: str, detail: str, *, status: int = 409) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _DoomedRecord:
    id: UUID
    collection: str
    entity: str
    key: str | None
    status: str
    run_id: UUID | None


@dataclass(frozen=True, slots=True)
class ErasureResult:
    erasure_record_id: UUID
    deleted: int
    affected_entities: int
    index_delete_job_id: UUID

    def as_json(self) -> dict[str, Any]:
        return {
            "erasure_record_id": str(self.erasure_record_id),
            "deleted_count": self.deleted,
            "affected_entity_count": self.affected_entities,
            "index_delete_job_id": str(self.index_delete_job_id),
        }


def _digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


async def _seed_rows(
    conn: Any,
    *,
    workspace: str,
    request: ErasureRequest,
) -> tuple[UUID, ...]:
    if request.entity is not None:
        result = await conn.execute(
            "select id from record where workspace = %s and entity = %s order by id",
            (workspace, request.entity),
        )
        rows = await result.fetchall()
        if not rows:
            raise ErasureError("entity_not_found", "entity has no records", status=404)
        return tuple(cast(UUID, row["id"]) for row in rows)

    assert request.record_ids is not None
    result = await conn.execute(
        """
        select id, collection, type, run_id
        from record
        where workspace = %s and id = any(%s::uuid[])
        """,
        (workspace, list(request.record_ids)),
    )
    rows = await result.fetchall()
    found = {cast(UUID, row["id"]): row for row in rows}
    missing = [item for item in request.record_ids if item not in found]
    if missing:
        raise ErasureError("record_not_found", f"record does not exist: {missing[0]}", status=404)
    for row in rows:
        if row["collection"] == "_system" and row["type"] in {"run", "erasure"}:
            raise ErasureError(
                "invalid_seed",
                "a _system run or erasure record cannot be erased directly",
                status=422,
            )
    expanded = set(request.record_ids)
    expanded.update(cast(UUID, row["run_id"]) for row in rows if row["run_id"] is not None)
    return tuple(sorted(expanded, key=str))


async def _closure(
    conn: Any, *, workspace: str, seeds: Sequence[UUID]
) -> tuple[_DoomedRecord, ...]:
    result = await conn.execute(
        """
        with recursive doomed(id) as (
          select unnest(%s::uuid[])
          union
          select child.id
          from record child
          join doomed parent on child.derived_from @> array[parent.id]::uuid[]
          where child.workspace = %s
        )
        select row.id, row.collection, row.entity, row.key, row.status, row.run_id
        from record row
        join doomed on doomed.id = row.id
        where row.workspace = %s
        order by row.id
        limit %s
        """,
        (list(seeds), workspace, workspace, _MAX_ERASURE_ROWS + 1),
    )
    rows = await result.fetchall()
    if len(rows) > _MAX_ERASURE_ROWS:
        raise ErasureError(
            "erasure_too_large",
            f"erasure closure exceeds {_MAX_ERASURE_ROWS} records",
        )
    return tuple(
        _DoomedRecord(
            id=cast(UUID, row["id"]),
            collection=str(row["collection"]),
            entity=str(row["entity"]),
            key=cast(str | None, row["key"]),
            status=str(row["status"]),
            run_id=cast(UUID | None, row["run_id"]),
        )
        for row in rows
    )


async def _erase_tx(
    conn: Any,
    *,
    workspace: str,
    request: ErasureRequest,
    settings: Settings,
    catalog: DefinitionCatalog | None = None,
) -> ErasureResult:
    """Erase one bounded provenance closure inside a locked transaction."""

    seeds = await _seed_rows(conn, workspace=workspace, request=request)
    doomed = await _closure(conn, workspace=workspace, seeds=seeds)
    if not doomed:
        raise ErasureError(
            "record_not_found", "no records matched the erasure selector", status=404
        )

    entities = tuple(sorted({row.entity for row in doomed}))
    job_result = await conn.execute(
        """
        select id
        from job
        where workspace = %s
          and kind = 'derive'
          and entity = any(%s::text[])
          and done_at is null
          and dead_at is null
        order by id
        for update
        """,
        (workspace, list(entities)),
    )
    active_job_ids = [cast(UUID, row["id"]) for row in await job_result.fetchall()]
    await acquire_entity_locks(conn, workspace, entities)
    if active_job_ids:
        await conn.execute(
            """
            update job
            set dead_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = 'erased', last_error = 'invalidated by erasure'
            where workspace = %s and id = any(%s::uuid[])
            """,
            (workspace, active_job_ids),
        )

    # Lock the exact closure rows before deleting so the captured projection
    # payload and keyed refresh identities are transactionally consistent.
    locked_result = await conn.execute(
        """
        select id, collection, entity, key, status, run_id
        from record
        where workspace = %s and id = any(%s::uuid[])
        order by id
        for update
        """,
        (workspace, [row.id for row in doomed]),
    )
    locked = tuple(
        _DoomedRecord(
            id=cast(UUID, row["id"]),
            collection=str(row["collection"]),
            entity=str(row["entity"]),
            key=cast(str | None, row["key"]),
            status=str(row["status"]),
            run_id=cast(UUID | None, row["run_id"]),
        )
        for row in await locked_result.fetchall()
    )
    if len(locked) != len(doomed):
        raise ErasureError("erasure_changed", "erasure closure changed before deletion")

    from memseek.projections import ProjectionTarget, _enqueue_projection_tx

    delete_job_id = await _enqueue_projection_tx(
        conn,
        workspace=workspace,
        kind="index_delete",
        targets=tuple(ProjectionTarget(row.id, row.collection) for row in locked),
    )
    if delete_job_id is None:
        raise RuntimeError("erasure projection delete job was not created")
    await conn.execute(
        "delete from record where workspace = %s and id = any(%s::uuid[])",
        (workspace, [row.id for row in locked]),
    )

    keyed = tuple(
        {
            "id": row.id,
            "collection": row.collection,
            "entity": row.entity,
            "key": row.key,
            "status": row.status,
        }
        for row in locked
        if row.key is not None
    )
    if keyed:
        from memseek.projections import refresh_current_projection_tx

        await refresh_current_projection_tx(conn, workspace=workspace, records=keyed)

    erasure_id = uuid4()
    collection_counts = Counter(row.collection for row in locked)
    content = {
        "text": f"erased {len(locked)} record(s)",
        "schema_version": 1,
        "operation": "erase",
        "status": "ok",
        "seed_count": len(seeds),
        "deleted_count": len(locked),
        "affected_entities": len(entities),
        "record_ids_sha256": _digest(str(row.id) for row in locked),
        "collections_sha256": _digest(
            f"{name}:{collection_counts[name]}" for name in sorted(collection_counts)
        ),
        "index_delete_job_id": str(delete_job_id),
    }
    await insert_canonical_record_tx(
        conn,
        CanonicalRecordWrite(
            id=erasure_id,
            workspace=workspace,
            collection="_system",
            collection_version=SYSTEM_COLLECTION_VERSION,
            collection_hash=SYSTEM_COLLECTION_HASH,
            entity=_AUDIT_ENTITY,
            type="erasure",
            content=content,
            ready=True,
            depth=0,
        ),
        settings,
    )
    from memseek.projections import on_records_ready_tx

    await on_records_ready_tx(
        conn,
        workspace=workspace,
        records=({"id": erasure_id, "collection": "_system"},),
        catalog=catalog,
    )
    if catalog is not None:
        from memseek.triggers import evaluate_entity_triggers_tx

        for entity in entities:
            await evaluate_entity_triggers_tx(
                conn,
                workspace=workspace,
                entity=entity,
                catalog=catalog,
            )

    return ErasureResult(
        erasure_record_id=erasure_id,
        deleted=len(locked),
        affected_entities=len(entities),
        index_delete_job_id=delete_job_id,
    )


async def purge_tombstoned_pages_tx(
    conn: Any,
    *,
    workspace: str,
    collection: str,
    collection_version: int,
    after_days: int,
    max_pages: int,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> ErasureResult | None:
    """Physically erase expired current tombstones and their provenance closure.

    ``created_at`` is server-controlled, unlike ``occurred_at``.  Selecting
    every historical version in each expired keyed slot means a successful
    purge removes the old page content as well as the current tombstone.
    """

    if after_days <= 0 or max_pages <= 0:
        raise ValueError("retention bounds must be positive")
    await acquire_workspace_lock(conn, workspace, exclusive=True)
    result = await conn.execute(
        """
        with current_pages as (
          select distinct on (entity, key, status)
                 entity, key, status, content, created_at
          from record
          where workspace = %s
            and collection = %s
            and collection_version = %s
            and status = 'active'
            and key is not null
          order by entity, key, status, seq desc
        ), expired_slots as (
          select entity, key, status
          from current_pages
          where content->>'tombstone' = 'true'
            and created_at <= clock_timestamp() - make_interval(days => %s)
          order by entity, key, status
          limit %s
        )
        select history.id
        from record history
        join expired_slots slot
          on slot.entity = history.entity
         and slot.key = history.key
         and slot.status = history.status
        where history.workspace = %s
          and history.collection = %s
          and history.collection_version = %s
        order by history.id
        limit %s
        """,
        (
            workspace,
            collection,
            collection_version,
            after_days,
            max_pages,
            workspace,
            collection,
            collection_version,
            _MAX_ERASURE_ROWS + 1,
        ),
    )
    records = tuple(cast(UUID, row["id"]) for row in await result.fetchall())
    if not records:
        return None
    if len(records) > _MAX_ERASURE_ROWS:
        raise ErasureError(
            "erasure_too_large",
            f"retention seed set exceeds {_MAX_ERASURE_ROWS} records",
        )
    return await _erase_tx(
        conn,
        workspace=workspace,
        request=ErasureRequest(record_ids=records),
        settings=settings,
        catalog=catalog,
    )


async def erase(
    pool: DatabasePool,
    *,
    workspace: str,
    request: ErasureRequest,
    settings: Settings,
    catalog: DefinitionCatalog | None = None,
) -> ErasureResult:
    """Erase one bounded provenance closure and enqueue projection repair."""

    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace, exclusive=True)
        result = await _erase_tx(
            conn,
            workspace=workspace,
            request=request,
            settings=settings,
            catalog=catalog,
        )
    log_event(
        LOGGER,
        "warning",
        "erasure.completed",
        workspace=workspace,
        erasure_record_id=str(result.erasure_record_id),
        deleted=result.deleted,
        affected_entities=result.affected_entities,
    )
    return result


__all__ = [
    "ErasureError",
    "ErasureRequest",
    "ErasureResult",
    "erase",
    "purge_tombstoned_pages_tx",
]
