"""Readiness and durable search-projection outbox integration tests."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.jobs import claim_job, complete_job, retry_or_dead_letter_job
from memseek.models import ClaimedJob, LeaseLost
from memseek.projections import (
    ProjectionInvariantError,
    ReadyRecord,
    execute_projection_job,
    handle_projection_job,
    on_records_ready_tx,
    refresh_current_projection_tx,
)
from memseek.search.registry import (
    CandidateHit,
    CandidateQuery,
    SearchCapability,
)


class RecordingBackend:
    """Idempotent backend spy keyed by projected document ID."""

    NAME: ClassVar[str] = "pg"
    CAPS: ClassVar[frozenset[SearchCapability]] = frozenset(
        {"vector", "text", "recent", "structured"}
    )

    def __init__(self) -> None:
        self.upsert_calls: list[list[dict[str, Any]]] = []
        self.delete_calls: list[list[dict[str, Any]]] = []
        self.documents: dict[str, dict[str, Any]] = {}

    async def candidates(
        self,
        cfg: Settings,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        qvec: list[float] | None,
    ) -> list[CandidateHit]:
        del cfg, conn, workspace, query, qvec
        return []

    async def upsert(self, cfg: Settings, rows: list[dict[str, Any]]) -> None:
        del cfg
        self.upsert_calls.append(rows)
        for row in rows:
            self.documents[row["id"]] = row

    async def delete(self, cfg: Settings, workspace: str, rows: list[dict[str, Any]]) -> None:
        del cfg, workspace
        self.delete_calls.append(rows)
        for row in rows:
            self.documents.pop(row["id"], None)


class FailOnceBackend(RecordingBackend):
    """Backend spy that exposes one retryable failure before succeeding."""

    def __init__(self) -> None:
        super().__init__()
        self.attempted_upserts: list[list[dict[str, Any]]] = []

    async def upsert(self, cfg: Settings, rows: list[dict[str, Any]]) -> None:
        self.attempted_upserts.append(rows)
        if len(self.attempted_upserts) == 1:
            raise RuntimeError("backend unavailable")
        await super().upsert(cfg, rows)


@pytest.fixture(scope="module")
def catalog(settings: Settings) -> DefinitionCatalog:
    return load_definition_catalog(settings)


async def _insert_record_tx(
    conn: AsyncConnection[Any],
    catalog: DefinitionCatalog,
    *,
    workspace: str,
    text: str,
    key: str | None,
    ready: bool,
    entity: str = "entity-1",
) -> ReadyRecord:
    collection = catalog.resolve_collection("main")
    result = await conn.execute(
        """
        insert into record (
          workspace, collection, collection_version, collection_hash,
          entity, key, type, status, content, enriched_at
        ) values (
          %s, %s, %s, %s, %s, %s, 'fact', 'active', %s,
          case when %s then clock_timestamp() else null end
        )
        returning id
        """,
        (
            workspace,
            collection.name,
            collection.version,
            collection.contract_hash,
            entity,
            key,
            Jsonb({"text": text}),
            ready,
        ),
    )
    row = await result.fetchone()
    assert row is not None
    return ReadyRecord(
        id=row["id"],
        collection=collection.name,
        entity=entity,
        key=key,
        status="active",
    )


async def _claim_projection(pool: DatabasePool) -> ClaimedJob:
    claimed = await claim_job(
        pool,
        worker_id="projection",
        kinds=("index_upsert", "index_delete"),
        lease_s=30,
        max_attempts=3,
    )
    assert claimed is not None
    return claimed


async def test_ready_hook_rejects_unready_then_commits_outbox_atomically(
    db_pool: DatabasePool,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-ready"
    await create_workspace(db_pool, workspace)
    async with db_pool.connection() as conn, conn.transaction():
        record = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="not yet", key=None, ready=False
        )
        with pytest.raises(ProjectionInvariantError, match="before enrichment"):
            await on_records_ready_tx(conn, workspace=workspace, records=(record,), catalog=catalog)
        count_result = await conn.execute(
            "select count(*) as count from job where workspace = %s", (workspace,)
        )
        assert (await count_result.fetchone()) == {"count": 0}

        await conn.execute(
            "update record set enriched_at = clock_timestamp() where id = %s", (record.id,)
        )
        await on_records_ready_tx(conn, workspace=workspace, records=(record,), catalog=catalog)

    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select kind, payload, dedupe_key
            from job where workspace = %s and kind = 'index_upsert'
            """,
            (workspace,),
        )
        rows = await result.fetchall()
    assert rows == [
        {
            "kind": "index_upsert",
            "payload": {"records": [{"id": str(record.id), "collection": record.collection}]},
            "dedupe_key": None,
        }
    ]


async def test_pending_keyed_replacement_suppresses_previous_current_projection(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-keyed"
    await create_workspace(db_pool, workspace)
    backend = RecordingBackend()
    async with db_pool.connection() as conn, conn.transaction():
        previous = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="previous", key="role", ready=True
        )
        await on_records_ready_tx(conn, workspace=workspace, records=(previous,), catalog=catalog)
    first = await _claim_projection(db_pool)
    await handle_projection_job(
        db_pool,
        settings,
        catalog,
        first,
        backends={"pg_default": backend},
    )
    assert backend.documents[str(previous.id)]["is_current"] is True

    async with db_pool.connection() as conn, conn.transaction():
        pending = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="pending", key="role", ready=False
        )
        await refresh_current_projection_tx(conn, workspace=workspace, records=(pending,))
    second = await _claim_projection(db_pool)
    assert second.payload == {
        "records": [{"id": str(previous.id), "collection": previous.collection}]
    }
    await handle_projection_job(
        db_pool,
        settings,
        catalog,
        second,
        backends={"pg_default": backend},
    )
    assert backend.documents[str(previous.id)]["is_current"] is False
    assert str(pending.id) not in backend.documents

    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "update record set enriched_at = clock_timestamp() where id = %s", (pending.id,)
        )
        # ID-only mappings exercise the canonical reload path used by relation
        # and enrichment finalization.
        await on_records_ready_tx(
            conn,
            workspace=workspace,
            records=({"id": pending.id},),
            catalog=catalog,
        )
    third = await _claim_projection(db_pool)
    assert {item["id"] for item in third.payload["records"]} == {
        str(previous.id),
        str(pending.id),
    }
    await handle_projection_job(
        db_pool,
        settings,
        catalog,
        third,
        backends={"pg_default": backend},
    )
    assert backend.documents[str(previous.id)]["is_current"] is False
    assert backend.documents[str(pending.id)]["is_current"] is True


async def test_projection_retry_refetches_truth_and_missing_upsert_becomes_delete(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-refetch"
    await create_workspace(db_pool, workspace)
    backend = RecordingBackend()
    async with db_pool.connection() as conn, conn.transaction():
        record = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="initial", key=None, ready=True
        )
        await on_records_ready_tx(conn, workspace=workspace, records=(record,), catalog=catalog)
    claimed = await _claim_projection(db_pool)

    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set content = %s where id = %s",
            (Jsonb({"text": "canonical latest"}), record.id),
        )
    registry = {"pg_default": backend}
    await execute_projection_job(db_pool, settings, catalog, claimed, backends=registry)
    await execute_projection_job(db_pool, settings, catalog, claimed, backends=registry)
    assert [call[0]["text"] for call in backend.upsert_calls] == [
        "canonical latest",
        "canonical latest",
    ]
    assert len(backend.upsert_calls[0][0]["vector"]) == catalog.models.embedding.dimensions
    assert backend.upsert_calls[0][0]["has_embedding"] is False
    collection = catalog.resolve_collection("main")
    assert backend.upsert_calls[0][0]["collection_version"] == collection.version
    assert backend.upsert_calls[0][0]["collection_hash"] == collection.contract_hash

    async with db_pool.connection() as conn:
        await conn.execute("delete from record where id = %s", (record.id,))
    await execute_projection_job(db_pool, settings, catalog, claimed, backends=registry)
    assert backend.delete_calls[-1] == [{"id": str(record.id), "collection": record.collection}]
    assert str(record.id) not in backend.documents
    await complete_job(db_pool, claimed)


async def test_projection_backend_failure_is_durable_and_retry_refetches_canonical_truth(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-durable-retry"
    await create_workspace(db_pool, workspace)
    backend = FailOnceBackend()
    async with db_pool.connection() as conn, conn.transaction():
        record = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="before failure", key=None, ready=True
        )
        await on_records_ready_tx(conn, workspace=workspace, records=(record,), catalog=catalog)
    first_claim = await _claim_projection(db_pool)
    original_payload = first_claim.payload

    with pytest.raises(RuntimeError, match="backend unavailable"):
        await execute_projection_job(
            db_pool,
            settings,
            catalog,
            first_claim,
            backends={"pg_default": backend},
        )
    transition = await retry_or_dead_letter_job(
        db_pool,
        first_claim,
        max_attempts=settings.job_max_attempts,
        error_kind="backend",
        error="projection_backend: RuntimeError",
    )
    assert transition.dead is False
    async with db_pool.connection() as conn:
        pending = await (
            await conn.execute(
                """
                select attempts, payload, locked_by, lease_until, done_at, dead_at,
                       run_after > clock_timestamp() as delayed
                from job where id = %s
                """,
                (first_claim.id,),
            )
        ).fetchone()
        await conn.execute(
            "update record set content = %s where id = %s",
            (Jsonb({"text": "after failure"}), record.id),
        )
        await conn.execute(
            "update job set run_after = clock_timestamp() where id = %s", (first_claim.id,)
        )
    assert pending == {
        "attempts": 1,
        "payload": original_payload,
        "locked_by": None,
        "lease_until": None,
        "done_at": None,
        "dead_at": None,
        "delayed": True,
    }

    retry_claim = await _claim_projection(db_pool)
    assert retry_claim.id == first_claim.id
    assert retry_claim.attempts == 2
    assert retry_claim.payload == original_payload
    await handle_projection_job(
        db_pool,
        settings,
        catalog,
        retry_claim,
        backends={"pg_default": backend},
    )

    assert len(backend.attempted_upserts) == 2
    assert backend.attempted_upserts[0][0]["text"] == "before failure"
    assert backend.attempted_upserts[1][0]["text"] == "after failure"
    assert backend.documents[str(record.id)]["text"] == "after failure"
    async with db_pool.connection() as conn:
        completed = await (
            await conn.execute(
                "select attempts, done_at is not null as done, dead_at from job where id = %s",
                (first_claim.id,),
            )
        ).fetchone()
    assert completed == {"attempts": 2, "done": True, "dead_at": None}


async def test_projection_refuses_stored_collection_hash_drift_before_backend_io(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-collection-drift"
    await create_workspace(db_pool, workspace)
    async with db_pool.connection() as conn, conn.transaction():
        record = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="ready", key=None, ready=True
        )
        await on_records_ready_tx(
            conn,
            workspace=workspace,
            records=(record,),
            catalog=catalog,
        )
    claimed = await _claim_projection(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set collection_hash = %s where id = %s",
            ("0" * 64, record.id),
        )

    backend = RecordingBackend()
    with pytest.raises(ProjectionInvariantError, match="collection_definition_mismatch"):
        await execute_projection_job(
            db_pool,
            settings,
            catalog,
            claimed,
            backends={"pg_default": backend},
        )
    assert backend.upsert_calls == []
    assert backend.delete_calls == []


async def test_projection_execution_rejects_an_expired_claim_before_backend_io(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-fence"
    await create_workspace(db_pool, workspace)
    async with db_pool.connection() as conn, conn.transaction():
        record = await _insert_record_tx(
            conn, catalog, workspace=workspace, text="ready", key=None, ready=True
        )
        await on_records_ready_tx(conn, workspace=workspace, records=(record,), catalog=catalog)
    claimed = await _claim_projection(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute(
            "update job set lease_until = clock_timestamp() where id = %s", (claimed.id,)
        )
    backend = RecordingBackend()
    with pytest.raises(LeaseLost):
        await execute_projection_job(
            db_pool,
            settings,
            catalog,
            claimed,
            backends={"pg_default": backend},
        )
    assert backend.upsert_calls == []
    assert backend.delete_calls == []


async def test_ready_system_record_uses_core_projection_without_public_definition(
    db_pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
) -> None:
    workspace = "projection-system"
    await create_workspace(db_pool, workspace)
    async with db_pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, status, content, enriched_at
            ) values (
              %s, '_system', 1, %s, 'entity-1', 'run', 'active', %s,
              clock_timestamp()
            ) returning id
            """,
            (workspace, "0" * 64, Jsonb({"text": "audit"})),
        )
        row = await result.fetchone()
        assert row is not None
        await on_records_ready_tx(
            conn,
            workspace=workspace,
            records=({"id": row["id"]},),
            catalog=catalog,
        )
    claimed = await _claim_projection(db_pool)
    backend = RecordingBackend()
    await handle_projection_job(
        db_pool,
        settings,
        catalog,
        claimed,
        backends={"pg": backend},
    )
    assert backend.upsert_calls[0][0]["collection"] == "_system"
    assert backend.upsert_calls[0][0]["text"] == "audit"
