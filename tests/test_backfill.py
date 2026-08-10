"""Applying a processor to records that already exist.

The point of a backfill is that it reaches rows ordinary enrichment never will,
without overwriting anything and without an unbounded scan.  These tests drive the
real worker lane against real rows so the write-once guarantee, the cursor, the
budget, and cancellation are all exercised rather than described.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from evolution_catalog import catalog_files, ingest, publish

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.backfill import (
    BackfillError,
    cancel_backfill,
    get_backfill,
    request_backfill,
    validate_target,
)
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.models import WorkspaceCredential
from memseek.worker import WorkerRuntime, run_worker_once
from memseek.workspace_catalog import WorkspaceCatalogRegistry


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "backfill")


def _app(settings: Settings) -> Any:
    return create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )


async def _runtime(settings: Settings, db_pool: DatabasePool) -> WorkerRuntime:
    catalog = load_definition_catalog(settings)
    return WorkerRuntime(
        settings=settings,
        catalog=catalog,
        pool=db_pool,
        catalog_registry=WorkspaceCatalogRegistry(db_pool, settings, catalog),
    )


async def _drain(runtime: WorkerRuntime, *, passes: int = 80) -> None:
    """Run passes until nothing is busy.

    A backfill does one bounded batch per pass by design, so draining one takes as
    many passes as it takes batches — plus the sweep that confirms completion.
    """

    for _ in range(passes):
        if not (await run_worker_once(runtime, worker_id="backfill-test")).busy:
            return
    raise AssertionError("worker did not settle within the pass budget")


async def _seed(
    settings: Settings,
    db_pool: DatabasePool,
    workspace: WorkspaceCredential,
    *,
    rows: int = 4,
    collections: list[dict[str, Any]] | None = None,
    processors: list[dict[str, Any]] | None = None,
) -> None:
    """Publish a package, ingest rows, and drive them to ready."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            published = await publish(
                client, headers, catalog_files(collections=collections, processors=processors)
            )
            assert published.status_code == 200, published.text
            for index in range(rows):
                ingested = await ingest(client, headers, text=f"note {index}")
                assert ingested.status_code == 200, ingested.text
    await _drain(await _runtime(settings, db_pool))


async def _annotation_count(db_pool: DatabasePool, workspace: str, processor: str) -> int:
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select count(*) as rows from record where workspace = %s and annotations ? %s",
            (workspace, processor),
        )
        row = await result.fetchone()
    return int(row["rows"]) if row else 0


async def test_backfill_reaches_records_ordinary_enrichment_never_would(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The whole point: rows written before a processor existed still get it."""

    await _seed(settings, db_pool, workspace, rows=4)
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 0

    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )
    assert handle.state == "queued"
    assert handle.annotated == 0

    await _drain(await _runtime(settings, db_pool))

    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert finished.scanned == 4
    assert finished.annotated == 4
    assert finished.completed_at is not None
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 4


async def test_backfill_never_overwrites_an_existing_annotation(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Annotation names stay write-once, so a second run cannot rewrite history."""

    await _seed(settings, db_pool, workspace, rows=2)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    target: dict[str, Any] = {
        "workspace": workspace.workspace,
        "collection": "notes",
        "version": 1,
        "processor": "tone_v1",
        "catalog": catalog,
    }
    first = await request_backfill(db_pool, **target)
    await _drain(await _runtime(settings, db_pool))
    assert (
        await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=first.id)
    ).annotated == 2

    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select id, annotations -> 'tone_v1' as tone from record"
            " where workspace = %s and annotations ? 'tone_v1' order by seq",
            (workspace.workspace,),
        )
        before = {str(row["id"]): row["tone"] for row in await result.fetchall()}
    assert len(before) == 2

    # Running the same backfill again is a no-op: selection is presence-based, so
    # every row is skipped rather than recomputed.
    second = await request_backfill(db_pool, **target)
    await _drain(await _runtime(settings, db_pool))
    repeated = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=second.id)
    assert repeated.state == "done"
    assert repeated.scanned == 0
    assert repeated.annotated == 0

    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select id, annotations -> 'tone_v1' as tone from record"
            " where workspace = %s and annotations ? 'tone_v1' order by seq",
            (workspace.workspace,),
        )
        after = {str(row["id"]): row["tone"] for row in await result.fetchall()}
    assert after == before


async def test_backfill_respects_its_row_budget(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A budget is a hard stop, because a backfill spends real provider money."""

    await _seed(settings, db_pool, workspace, rows=5)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
        max_rows=2,
    )
    await _drain(await _runtime(settings, db_pool))

    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert finished.scanned == 2
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 2


async def test_bounded_batches_advance_a_resumable_cursor(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Each step is bounded and reports where the next one must continue."""

    from memseek.enrichment import backfill_annotations

    await _seed(settings, db_pool, workspace, rows=6)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)

    cursor = 0
    seen = 0
    steps = 0
    while True:
        batch = await backfill_annotations(
            db_pool,
            settings,
            catalog,
            workspace=workspace.workspace,
            collection="notes",
            version=1,
            processor="tone_v1",
            after_seq=cursor,
            limit=2,
        )
        steps += 1
        seen += batch.scanned
        if batch.exhausted:
            break
        assert batch.scanned == 2
        assert batch.last_seq > cursor
        cursor = batch.last_seq
    assert seen == 6
    # Three full pages plus the page that discovered the end.
    assert steps == 4
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 6


async def test_backfill_lane_completes_a_multi_batch_target(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A target larger than one batch still finishes, through queued successors."""

    await _seed(settings, db_pool, workspace, rows=6)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )
    runtime = WorkerRuntime(
        settings=settings.model_copy(update={"backfill_batch": 1}),
        catalog=load_definition_catalog(settings),
        pool=db_pool,
        catalog_registry=registry,
    )
    await _drain(runtime)
    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert finished.scanned == 6
    assert finished.annotated == 6
    # A completed backfill has confirmed from the first record: the cursor is back
    # at zero because the sweep that finished it started there and found nothing.
    assert finished.cursor_seq == 0


async def test_cancelling_a_backfill_keeps_what_it_already_wrote(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    await _seed(settings, db_pool, workspace, rows=3)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )
    cancelled = await cancel_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert cancelled.state == "cancelled"
    assert cancelled.completed_at is not None

    # A cancelled backfill's queued job is a no-op rather than a failure.
    await _drain(await _runtime(settings, db_pool))
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 0

    with pytest.raises(BackfillError) as refused:
        await cancel_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert refused.value.code == "not_live"
    assert refused.value.status == 409


async def test_one_live_backfill_per_target(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Two operators cannot race the same rows."""

    await _seed(settings, db_pool, workspace, rows=2)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    kwargs: dict[str, Any] = {
        "workspace": workspace.workspace,
        "collection": "notes",
        "version": 1,
        "processor": "tone_v1",
        "catalog": catalog,
    }
    await request_backfill(db_pool, **kwargs)
    with pytest.raises(BackfillError) as conflict:
        await request_backfill(db_pool, **kwargs)
    assert conflict.value.code == "backfill_exists"
    assert conflict.value.status == 409


def test_validate_target_refuses_impossible_requests(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)

    with pytest.raises(BackfillError) as unknown_collection:
        validate_target(catalog, collection="nope", version=1, processor="embedding_v1")
    assert unknown_collection.value.code == "unknown_collection"

    with pytest.raises(BackfillError) as unknown_processor:
        validate_target(catalog, collection="main", version=1, processor="nope")
    assert unknown_processor.value.code == "unknown_processor"

    with pytest.raises(BackfillError) as out_of_scope:
        # sentiment_v1 admits only `main`, so it cannot annotate calendar events.
        validate_target(catalog, collection="calendar_events", version=1, processor="sentiment_v1")
    assert out_of_scope.value.code == "processor_scope"


async def test_backfill_routes_report_progress_and_cancel(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The HTTP surface an author actually uses."""

    await _seed(settings, db_pool, workspace, rows=2)
    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/backfill",
                headers=headers,
                json={"collection": "notes", "version": 1, "processor": "tone_v1"},
            )
            assert started.status_code == 202, started.text
            handle = started.json()
            assert handle["state"] == "queued"
            listed = await client.get("/backfill", headers=headers)
            assert [item["id"] for item in listed.json()["backfills"]] == [handle["id"]]

            duplicate = await client.post(
                "/backfill",
                headers=headers,
                json={"collection": "notes", "version": 1, "processor": "tone_v1"},
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["error"] == "backfill_exists"

            unknown = await client.post(
                "/backfill",
                headers=headers,
                json={"collection": "notes", "version": 1, "processor": "nope"},
            )
            assert unknown.status_code == 422
            assert unknown.json()["error"] == "unknown_processor"

            await _drain(await _runtime(settings, db_pool))

            done = await client.get(f"/backfill/{handle['id']}", headers=headers)
            assert done.status_code == 200
            assert done.json()["state"] == "done"
            assert done.json()["annotated"] == 2

            stale = await client.post(f"/backfill/{handle['id']}/cancel", headers=headers)
            assert stale.status_code == 409

            missing = await client.get(
                f"/backfill/{UUID(int=0)}",
                headers=headers,
            )
            assert missing.status_code == 404


async def test_a_finished_backfill_leaves_no_claimed_job_behind(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Every claim must be completed, or the lane accrues zombie leases."""

    await _seed(settings, db_pool, workspace, rows=3)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )
    await _drain(await _runtime(settings, db_pool))

    assert (
        await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    ).state == "done"
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select count(*) filter (where done_at is null and dead_at is null) as open,
                   count(*) filter (where locked_by is not null) as locked,
                   count(*) filter (where dead_at is not null) as dead
            from job
            where workspace = %s and kind = 'annotation_backfill'
            """,
            (workspace.workspace,),
        )
        row = await result.fetchone()
    assert row is not None
    assert int(row["open"]) == 0
    assert int(row["locked"]) == 0
    assert int(row["dead"]) == 0


async def test_a_multi_claim_backfill_completes_every_job_it_queues(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Hand-off queues a successor and completes the claim that queued it."""

    await _seed(settings, db_pool, workspace, rows=8)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )
    # One row per batch and five batches per claim forces at least two claims.
    runtime = WorkerRuntime(
        settings=settings.model_copy(update={"backfill_batch": 1}),
        catalog=load_definition_catalog(settings),
        pool=db_pool,
        catalog_registry=registry,
    )
    await _drain(runtime)

    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert finished.annotated == 8
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select count(*) as total,
                   count(*) filter (where done_at is not null) as done
            from job
            where workspace = %s and kind = 'annotation_backfill'
            """,
            (workspace.workspace,),
        )
        row = await result.fetchone()
    assert row is not None
    # More than one job ran, and every one of them was completed.
    assert int(row["total"]) > 1
    assert int(row["done"]) == int(row["total"])


async def test_a_row_locked_by_another_transaction_is_not_silently_skipped(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Selection skips locked rows, so `done` must survive a concurrent lock."""

    from memseek.enrichment import backfill_annotations

    await _seed(settings, db_pool, workspace, rows=4)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)

    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select id, seq from record where workspace = %s and collection = 'notes'"
            " order by seq limit 1",
            (workspace.workspace,),
        )
        first = await result.fetchone()
    assert first is not None

    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
    )

    # Hold the earliest row's lock while the first sweep runs. Selection skips it
    # and the cursor moves past it, which is exactly the case a single forward
    # sweep would lose.
    async with db_pool.connection() as holder, holder.transaction():
        await holder.execute("select id from record where id = %s for update", (first["id"],))
        batch = await backfill_annotations(
            db_pool,
            settings,
            catalog,
            workspace=workspace.workspace,
            collection="notes",
            version=1,
            processor="tone_v1",
            after_seq=0,
            limit=200,
        )
        assert batch.scanned == 3
        assert batch.last_seq > int(first["seq"])

    # The lane, run after the lock is released, must still reach the skipped row
    # and only then report completion.
    await _drain(await _runtime(settings, db_pool))
    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 4


async def test_an_unbudgeted_backfill_migrates_everything_without_monopolizing(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The "just migrate everything" case: no budget, bounded per pass, completes."""

    await _seed(settings, db_pool, workspace, rows=20)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    handle = await request_backfill(
        db_pool,
        workspace=workspace.workspace,
        collection="notes",
        version=1,
        processor="tone_v1",
        catalog=catalog,
        # No max_rows: reach every eligible record.
    )
    assert handle.max_rows is None

    runtime = WorkerRuntime(
        settings=settings.model_copy(update={"backfill_batch": 4}),
        catalog=load_definition_catalog(settings),
        pool=db_pool,
        catalog_registry=registry,
    )

    # One pass must not swallow the whole corpus: that is what keeps every other
    # lane serviced while a long migration runs.
    first = await run_worker_once(runtime, worker_id="bulk")
    assert first.backfill_batches == 1
    assert first.backfilled_annotations == 4
    mid = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert mid.state == "running"
    assert mid.scanned == 4

    passes = 1
    while (await run_worker_once(runtime, worker_id="bulk")).busy:
        passes += 1
        assert passes < 40, "an unbudgeted backfill should converge"

    finished = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
    assert finished.state == "done"
    assert finished.scanned == 20
    assert finished.annotated == 20
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 20
    # 20 records at 4 per pass, plus the sweep that confirms completion.
    assert passes >= 6


async def test_a_budget_slice_can_be_repeated_until_everything_is_reached(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A budgeted backfill is a slice; re-requesting it resumes automatically."""

    await _seed(settings, db_pool, workspace, rows=7)
    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    runtime = await _runtime(settings, db_pool)

    reached = 0
    slices = 0
    while True:
        handle = await request_backfill(
            db_pool,
            workspace=workspace.workspace,
            collection="notes",
            version=1,
            processor="tone_v1",
            catalog=catalog,
            max_rows=3,
        )
        await _drain(runtime)
        done = await get_backfill(db_pool, workspace=workspace.workspace, backfill_id=handle.id)
        assert done.state == "done"
        reached += done.scanned
        slices += 1
        if done.scanned < 3:
            # Ran out of records rather than budget: everything is reached.
            break
        assert slices < 6

    assert reached == 7
    assert slices == 3
    assert await _annotation_count(db_pool, workspace.workspace, "tone_v1") == 7
