"""Catalog-declared tombstone retention integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.triggers import schedule_cron_jobs
from memseek.worker import WorkerRuntime, run_worker_once


async def _insert_page(
    pool: DatabasePool,
    *,
    workspace: str,
    catalog: DefinitionCatalog,
    settings: Settings,
    key: str,
) -> UUID:
    result = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="retention",
                    collection="pages",
                    key=key,
                    type="page",
                    text="A retained page body.",
                    content={
                        "title": "Retained page",
                        "body": "A retained page body.",
                        "type": "note",
                    },
                ),
            )
        ),
        catalog=catalog,  # type: ignore[arg-type]
        settings=settings,
    )
    return result.inserted[0].id


async def _tombstone_page(
    pool: DatabasePool,
    *,
    workspace: str,
    catalog: DefinitionCatalog,
    settings: Settings,
    key: str,
    parent_id: UUID,
) -> UUID:
    result = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="retention",
                    collection="pages",
                    key=key,
                    type="page",
                    tombstone=True,
                    derived_from=(parent_id,),
                ),
            )
        ),
        catalog=catalog,  # type: ignore[arg-type]
        settings=settings,
    )
    return result.inserted[0].id


async def test_gbrain_retention_purges_expired_current_page_tombstones(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    package = catalog.resolve_package("gbrain", "0.13.0")
    assert package.retentions[0].model_dump() == {
        "name": "purge_pages",
        "collection": "pages@1",
        "after_days": 30,
        "cron": "23 3 * * *",
        "max_pages": 25,
    }
    credential = await create_workspace(db_pool, "gbrain-retention")
    page_id = await _insert_page(
        db_pool,
        workspace=credential.workspace,
        catalog=catalog,
        settings=settings,
        key="expired-page",
    )
    await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="retention",
                    collection="edges",
                    type="edge",
                    text="retention mentions expired-page",
                    content={
                        "subject": "retention",
                        "object": "expired-page",
                        "predicate": "mentions",
                        "link_source": "markdown",
                        "context": "retention mentions expired-page",
                        "confidence": 1.0,
                    },
                    derived_from=(page_id,),
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    tombstone_id = await _tombstone_page(
        db_pool,
        workspace=credential.workspace,
        catalog=catalog,
        settings=settings,
        key="expired-page",
        parent_id=page_id,
    )
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set created_at = clock_timestamp() - interval '31 days'
            where workspace = %s and id = %s::uuid
            """,
            (credential.workspace, tombstone_id),
        )

    result = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="retention-worker",
    )

    assert result.retention_jobs == 1
    async with db_pool.connection() as conn:
        pages = await (
            await conn.execute(
                "select id from record where workspace = %s and collection = 'pages'",
                (credential.workspace,),
            )
        ).fetchall()
        edges = await (
            await conn.execute(
                "select id from record where workspace = %s and collection = 'edges'",
                (credential.workspace,),
            )
        ).fetchall()
        audit = await (
            await conn.execute(
                """
                select content
                from record
                where workspace = %s and collection = '_system' and type = 'erasure'
                """,
                (credential.workspace,),
            )
        ).fetchone()
        job = await (
            await conn.execute(
                """
                select done_at, payload
                from job
                where workspace = %s and kind = 'retention_purge'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert pages == []
    assert edges == []
    assert audit is not None
    assert audit["content"]["operation"] == "erase"
    assert audit["content"]["deleted_count"] >= 3
    assert job is not None
    assert job["done_at"] is not None
    assert job["payload"]["retention"] == "purge_pages"


async def test_retention_uses_server_created_at_not_client_occurred_at(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "gbrain-retention-created-at")
    page_id = await _insert_page(
        db_pool,
        workspace=credential.workspace,
        catalog=catalog,
        settings=settings,
        key="recent-tombstone",
    )
    tombstone_id = await _tombstone_page(
        db_pool,
        workspace=credential.workspace,
        catalog=catalog,
        settings=settings,
        key="recent-tombstone",
        parent_id=page_id,
    )
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set occurred_at = clock_timestamp() - interval '31 days'
            where workspace = %s and id = %s::uuid
            """,
            (credential.workspace, tombstone_id),
        )

    result = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="retention-created-at-worker",
    )

    assert result.retention_jobs == 1
    async with db_pool.connection() as conn:
        records = await (
            await conn.execute(
                """
                select id
                from record
                where workspace = %s and collection = 'pages'
                order by id
                """,
                (credential.workspace,),
            )
        ).fetchall()
    assert [str(row["id"]) for row in records] == sorted(map(str, (page_id, tombstone_id)))


async def test_retention_schedules_only_the_latest_tick_after_downtime(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(gbrain_settings)
    await create_workspace(db_pool, "gbrain-retention-schedule")

    await schedule_cron_jobs(
        db_pool,
        catalog=catalog,
        now=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    await schedule_cron_jobs(
        db_pool,
        catalog=catalog,
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )

    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                """
                select payload->>'scheduled_at' as scheduled_at
                from job
                where workspace = 'gbrain-retention-schedule'
                  and kind = 'retention_purge'
                order by payload->>'scheduled_at'
                """
            )
        ).fetchall()
    assert [row["scheduled_at"] for row in rows] == [
        "2026-07-01T03:23:00Z",
        "2026-07-10T03:23:00Z",
    ]
