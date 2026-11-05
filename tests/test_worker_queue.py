"""Worker queue lanes against a real database: cron pages, leases, and sweeps."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from reference_catalog import declare_test_sources, materialize_reference_catalog

import memseek.derive.runner as derive_runner_module
import memseek.erase as erase_module
import memseek.jobs as jobs_module
import memseek.projections as projections_module
import memseek.triggers as triggers_module
import memseek.worker as worker_module
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.derive.runner import DerivationJobResult
from memseek.models import ClaimedJob, LeaseLost
from memseek.worker import WorkerRuntime

_NIGHTLY = """name: profile.nightly
processor: profile
cron:
  expr: "0 2 * * *"
  entities: any
"""
_STAMP = "2026-11-05T02:00:00Z"


@pytest.fixture(scope="module")
def cron_catalog(tmp_path_factory: pytest.TempPathFactory) -> DefinitionCatalog:
    root = tmp_path_factory.mktemp("worker-queue") / "catalog"
    root.mkdir()
    materialize_reference_catalog(root)
    (root / "triggers" / "nightly.yaml").write_text(_NIGHTLY, encoding="utf-8")
    declare_test_sources(root, "triggers/nightly.yaml")
    return load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))


async def _workspace_with_entities(pool: DatabasePool, workspace: str, count: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)",
            (workspace, "0" * 64),
        )
        await conn.execute(
            """
            insert into record (workspace, collection, collection_version, collection_hash,
                                entity, type, status, content, enriched_at)
            select %s, 'main', 1, %s, 'e' || lpad(n::text, 4, '0'), 'event', 'active',
                   '{"text": "x"}'::jsonb, clock_timestamp()
            from generate_series(1, %s) as n
            """,
            (workspace, "a" * 64, count),
        )


async def _insert_root_scan(pool: DatabasePool, workspace: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            insert into job (workspace, kind, derivation, dedupe_key, payload)
            values (%s, 'cron_scan', 'profile', %s, %s)
            """,
            (
                workspace,
                f"cron:profile.nightly:profile:{_STAMP}",
                Jsonb(
                    {
                        "trigger": "profile.nightly",
                        "scheduled_at": _STAMP,
                        "entities": "any",
                        "cursor": None,
                    }
                ),
            ),
        )


async def _rows(pool: DatabasePool, query: LiteralString, params: tuple[Any, ...]) -> list[Any]:
    async with pool.connection() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def test_chained_cron_pages_key_off_the_root_not_the_previous_page(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_CRON_PAGE_SIZE", 2)
    await _workspace_with_entities(db_pool, "cron-pages", 5)
    await _insert_root_scan(db_pool, "cron-pages")
    runtime = WorkerRuntime(settings=settings, catalog=cron_catalog, pool=db_pool)

    assert await worker_module._drain_cron_jobs(runtime, worker_id="w") == 3

    scans = await _rows(
        db_pool,
        "select dedupe_key from job where workspace = %s and kind = 'cron_scan'"
        " order by created_at, id",
        ("cron-pages",),
    )
    assert [row["dedupe_key"] for row in scans] == [
        f"cron:profile.nightly:profile:{_STAMP}",
        f"cron:profile.nightly:profile:{_STAMP}:e0002",
        f"cron:profile.nightly:profile:{_STAMP}:e0004",
    ]
    derives = await _rows(
        db_pool,
        "select entity from job where workspace = %s and kind = 'derive' order by entity",
        ("cron-pages",),
    )
    assert [row["entity"] for row in derives] == ["e0001", "e0002", "e0003", "e0004", "e0005"]


def _short_lease(settings: Settings) -> Settings:
    return settings.model_copy(update={"job_lease_s": 3, "job_heartbeat_s": 1})


async def _job_state(pool: DatabasePool, workspace: str, kind: str) -> list[tuple[bool, int]]:
    rows = await _rows(
        pool,
        "select done_at is not null as done, attempts from job"
        " where workspace = %s and kind = %s order by created_at",
        (workspace, kind),
    )
    return [(row["done"], row["attempts"]) for row in rows]


async def test_cron_scan_slower_than_its_lease_completes_under_heartbeat(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _workspace_with_entities(db_pool, "cron-slow", 1)
    await _insert_root_scan(db_pool, "cron-slow")
    select_page = worker_module._cron_entities

    async def slow_page(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(4)
        return await select_page(*args, **kwargs)

    monkeypatch.setattr(worker_module, "_cron_entities", slow_page)
    runtime = WorkerRuntime(settings=_short_lease(settings), catalog=cron_catalog, pool=db_pool)

    assert await worker_module._drain_cron_jobs(runtime, worker_id="w") == 1
    assert await _job_state(db_pool, "cron-slow", "cron_scan") == [(True, 1)]


async def test_retention_purge_slower_than_its_lease_completes_under_heartbeat(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_definition_catalog(gbrain_settings)
    package = next(item for item in catalog.packages.values() if item.retentions)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('purge-slow', %s)", ("0" * 64,)
        )
        await conn.execute(
            "insert into job (workspace, kind, dedupe_key, payload)"
            " values ('purge-slow', 'retention_purge', 'retention:slow', %s)",
            (
                Jsonb(
                    {
                        "package_name": package.name,
                        "package_version": package.version,
                        "retention": package.retentions[0].name,
                        "scheduled_at": _STAMP,
                    }
                ),
            ),
        )

    async def slow_purge(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(4)

    monkeypatch.setattr(erase_module, "purge_tombstoned_pages_tx", slow_purge)
    runtime = WorkerRuntime(settings=_short_lease(gbrain_settings), catalog=catalog, pool=db_pool)

    assert await worker_module._drain_retention_jobs(runtime, worker_id="w") == 1
    assert await _job_state(db_pool, "purge-slow", "retention_purge") == [(True, 1)]


async def test_cron_page_commits_each_enqueue_batch_and_a_retry_converges(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_CRON_ENQUEUE_BATCH", 2)
    await _workspace_with_entities(db_pool, "cron-batches", 5)
    await _insert_root_scan(db_pool, "cron-batches")
    enqueue = triggers_module.enqueue_derive_tx
    failed: list[str] = []

    async def flaky_enqueue(conn: Any, **kwargs: Any) -> Any:
        if kwargs["entity"] == "e0003" and not failed:
            failed.append(kwargs["entity"])
            raise OSError("connection reset")
        return await enqueue(conn, **kwargs)

    monkeypatch.setattr(triggers_module, "enqueue_derive_tx", flaky_enqueue)
    runtime = WorkerRuntime(settings=settings, catalog=cron_catalog, pool=db_pool)
    derives = "select entity from job where workspace = %s and kind = 'derive' order by entity"

    assert await worker_module._drain_cron_jobs(runtime, worker_id="w") == 0
    assert [row["entity"] for row in await _rows(db_pool, derives, ("cron-batches",))] == [
        "e0001",
        "e0002",
    ]

    async with db_pool.connection() as conn:
        await conn.execute("update job set run_after = now() where kind = 'cron_scan'")
    assert await worker_module._drain_cron_jobs(runtime, worker_id="w") == 1
    assert [row["entity"] for row in await _rows(db_pool, derives, ("cron-batches",))] == [
        "e0001",
        "e0002",
        "e0003",
        "e0004",
        "e0005",
    ]
    assert await _job_state(db_pool, "cron-batches", "cron_scan") == [(True, 2)]


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_worker_schedules_cron_at_most_once_per_minute(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = AsyncMock(return_value=0)
    monkeypatch.setattr(triggers_module, "schedule_cron_jobs", schedule)
    clock = _Clock(1_800_000_000.0)
    runtime = WorkerRuntime(
        settings=settings,
        catalog=cron_catalog,
        pool=db_pool,
        cadence=worker_module.Cadence(clock),
    )

    await worker_module.run_worker_once(runtime, worker_id="w")
    clock.now += 59
    await worker_module.run_worker_once(runtime, worker_id="w")
    assert schedule.await_count == 1

    clock.now += 1
    await worker_module.run_worker_once(runtime, worker_id="w")
    assert schedule.await_count == 2


async def test_worker_reaps_expired_final_attempts_every_half_lease(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
) -> None:
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('reaper', %s)", ("0" * 64,)
        )
    clock = _Clock(1_800_000_000.0)
    runtime = WorkerRuntime(
        settings=settings,
        catalog=cron_catalog,
        pool=db_pool,
        cadence=worker_module.Cadence(clock),
    )
    insert_expired = """
        insert into job (workspace, kind, derivation, entity, attempts, lease_until, locked_by)
        values ('reaper', 'derive', 'profile', %s, %s, now() - interval '1 second', 'gone:t')
    """
    dead = "select entity from job where workspace = 'reaper' and dead_at is not null"

    await worker_module.run_worker_once(runtime, worker_id="w")
    async with db_pool.connection() as conn:
        await conn.execute(insert_expired, ("a", settings.job_max_attempts))
    clock.now += settings.job_lease_s / 2 - 1
    await worker_module.run_worker_once(runtime, worker_id="w")
    assert await _rows(db_pool, dead, ()) == []

    clock.now += 1
    await worker_module.run_worker_once(runtime, worker_id="w")
    assert await _rows(db_pool, dead, ()) == [{"entity": "a"}]


async def test_worker_purges_finished_jobs_past_retention(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
) -> None:
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('old-jobs', %s)", ("0" * 64,)
        )
        await conn.execute(
            """
            insert into job (workspace, kind, derivation, entity, created_at, done_at)
            values ('old-jobs', 'derive', 'profile', 'stale', now() - interval '16 days',
                    now() - interval '15 days'),
                   ('old-jobs', 'derive', 'profile', 'recent', now() - interval '2 days',
                    now() - interval '1 day')
            """
        )
    runtime = WorkerRuntime(settings=settings, catalog=cron_catalog, pool=db_pool)

    await worker_module.run_worker_once(runtime, worker_id="w")

    remaining = "select entity from job where workspace = 'old-jobs' and kind = 'derive'"
    assert await _rows(db_pool, remaining, ()) == [{"entity": "recent"}]


async def test_worker_dead_letters_a_derive_job_unready_past_the_timeout(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        derive_runner_module,
        "process_derivation_job",
        AsyncMock(return_value=DerivationJobResult("not_ready", high_seq=0)),
    )
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('unready', %s)", ("0" * 64,)
        )
        await conn.execute(
            """
            insert into job (workspace, kind, derivation, entity, unready_since)
            values ('unready', 'derive', 'profile', 'stuck', now() - interval '3601 seconds')
            """
        )
    runtime = WorkerRuntime(
        settings=settings.model_copy(update={"unready_timeout_s": 3600}),
        catalog=cron_catalog,
        pool=db_pool,
    )

    assert await worker_module._drain_derivation_jobs(runtime, worker_id="w") == (0, 1)
    dead = "select last_error_kind from job where workspace = 'unready' and dead_at is not null"
    assert await _rows(db_pool, dead, ()) == [{"last_error_kind": "not_ready_timeout"}]


def _claimed_derive() -> ClaimedJob:
    now = datetime.now(UTC)
    return ClaimedJob(
        id=uuid4(),
        workspace="w",
        kind="derive",
        derivation="profile",
        entity="e",
        payload={},
        dedupe_key=None,
        run_after=now,
        attempts=1,
        lease_until=now + timedelta(seconds=3),
        claim_token=f"w:{uuid4()}",
        created_at=now,
    )


async def test_heartbeat_rides_out_transient_database_errors(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    beats: list[str] = []

    async def flaky_heartbeat(_pool: Any, _claimed: Any, lease_s: int) -> datetime:
        beats.append("beat")
        if len(beats) == 1:
            raise psycopg.OperationalError("server closed the connection")
        if len(beats) == 2:
            raise PoolTimeout("couldn't get a connection")
        return datetime.now(UTC) + timedelta(seconds=lease_s)

    monkeypatch.setattr(jobs_module, "heartbeat_job", flaky_heartbeat)
    runtime = WorkerRuntime(
        settings=_short_lease(settings), catalog=cast(Any, object()), pool=cast(Any, object())
    )

    async def slow_operation() -> str:
        await asyncio.sleep(4)
        return "finished"

    assert await worker_module._with_heartbeat(runtime, _claimed_derive(), slow_operation()) == (
        "finished"
    )
    assert len(beats) >= 3


async def test_heartbeat_gives_up_before_the_known_lease_expires(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def down(*_args: Any) -> datetime:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(jobs_module, "heartbeat_job", down)
    runtime = WorkerRuntime(
        settings=_short_lease(settings), catalog=cast(Any, object()), pool=cast(Any, object())
    )
    cancelled: list[bool] = []

    async def long_operation() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    started = asyncio.get_running_loop().time()
    with pytest.raises(LeaseLost):
        await worker_module._with_heartbeat(runtime, _claimed_derive(), long_operation())
    assert asyncio.get_running_loop().time() - started < 3
    assert cancelled == [True]


async def test_non_derive_job_past_its_runtime_cap_is_retried(
    settings: Settings,
    db_pool: DatabasePool,
    cron_catalog: DefinitionCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hung_projection(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(projections_module, "execute_projection_job", hung_projection)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('hung', %s)", ("0" * 64,)
        )
        await conn.execute(
            "insert into job (workspace, kind, payload) values ('hung', 'index_upsert', %s)",
            (Jsonb({"records": []}),),
        )
    runtime = WorkerRuntime(
        settings=settings.model_copy(update={"job_max_runtime_s": 1}),
        catalog=cron_catalog,
        pool=db_pool,
    )

    drained = worker_module._drain_projection_jobs(runtime, worker_id="w", backends=None)
    assert await asyncio.wait_for(drained, timeout=5) == 0
    retried = "select attempts, last_error_kind, done_at from job where workspace = 'hung'"
    assert await _rows(db_pool, retried, ()) == [
        {"attempts": 1, "last_error_kind": "TimeoutError", "done_at": None}
    ]
