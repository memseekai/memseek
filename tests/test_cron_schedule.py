"""The cron scheduler: one bucket per pass, a durable checkpoint, one leader."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from reference_catalog import declare_test_sources, materialize_reference_catalog

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.locks import advisory_lock_key
from memseek.triggers import schedule_cron_jobs

_CRON_TRIGGERS = {
    "hourly_dirty.yaml": """name: profile.hourly_dirty
processor: profile
cron:
  expr: "0 * * * *"
  entities: dirty
""",
    "hourly_any.yaml": """name: profile.hourly_any
processor: profile
cron:
  expr: "0 * * * *"
  entities: any
""",
}


@pytest.fixture(scope="module")
def cron_catalog(tmp_path_factory: pytest.TempPathFactory) -> DefinitionCatalog:
    root = tmp_path_factory.mktemp("cron-schedule") / "catalog"
    root.mkdir()
    materialize_reference_catalog(root)
    for name, document in _CRON_TRIGGERS.items():
        (root / "triggers" / name).write_text(document, encoding="utf-8")
        declare_test_sources(root, f"triggers/{name}")
    return load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))


async def _cron_jobs(pool: DatabasePool) -> list[tuple[str, Any]]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select dedupe_key, payload from job
            where workspace = 'ws' and kind = 'cron_scan'
            order by dedupe_key
            """
        )
        return [(row["dedupe_key"], row["payload"]) for row in await result.fetchall()]


async def _checkpoints(pool: DatabasePool) -> dict[str, datetime]:
    async with pool.connection() as conn:
        result = await conn.execute(
            "select trigger, scheduled_at from cron_checkpoint where workspace = 'ws'"
        )
        return {
            row["trigger"]: row["scheduled_at"].astimezone(UTC) for row in await result.fetchall()
        }


async def test_each_trigger_gets_its_own_scan_for_a_shared_bucket(
    db_pool: DatabasePool, cron_catalog: DefinitionCatalog
) -> None:
    await create_workspace(db_pool, "ws")

    inserted = await schedule_cron_jobs(
        db_pool, catalog=cron_catalog, now=datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
    )

    assert inserted == 2
    assert await _cron_jobs(db_pool) == [
        (
            "cron:profile.hourly_any:profile:2030-01-01T12:00:00Z",
            {
                "trigger": "profile.hourly_any",
                "scheduled_at": "2030-01-01T12:00:00Z",
                "entities": "any",
                "cursor": None,
            },
        ),
        (
            "cron:profile.hourly_dirty:profile:2030-01-01T12:00:00Z",
            {
                "trigger": "profile.hourly_dirty",
                "scheduled_at": "2030-01-01T12:00:00Z",
                "entities": "dirty",
                "cursor": None,
            },
        ),
    ]


async def test_missed_buckets_collapse_into_the_latest(
    db_pool: DatabasePool, cron_catalog: DefinitionCatalog
) -> None:
    await create_workspace(db_pool, "ws")
    await schedule_cron_jobs(
        db_pool, catalog=cron_catalog, now=datetime(2030, 1, 1, 9, 5, tzinfo=UTC)
    )

    inserted = await schedule_cron_jobs(
        db_pool, catalog=cron_catalog, now=datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
    )

    assert inserted == 2
    assert [key for key, _payload in await _cron_jobs(db_pool)] == [
        "cron:profile.hourly_any:profile:2030-01-01T09:00:00Z",
        "cron:profile.hourly_any:profile:2030-01-01T12:00:00Z",
        "cron:profile.hourly_dirty:profile:2030-01-01T09:00:00Z",
        "cron:profile.hourly_dirty:profile:2030-01-01T12:00:00Z",
    ]
    assert await _checkpoints(db_pool) == {
        "profile.hourly_any": datetime(2030, 1, 1, 12, tzinfo=UTC),
        "profile.hourly_dirty": datetime(2030, 1, 1, 12, tzinfo=UTC),
    }


async def test_checkpoint_outlives_finished_job_rows(
    db_pool: DatabasePool, cron_catalog: DefinitionCatalog
) -> None:
    await create_workspace(db_pool, "ws")
    now = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
    await schedule_cron_jobs(db_pool, catalog=cron_catalog, now=now)
    async with db_pool.connection() as conn:
        await conn.execute("delete from job where workspace = 'ws'")

    assert await schedule_cron_jobs(db_pool, catalog=cron_catalog, now=now) == 0
    assert await _cron_jobs(db_pool) == []


async def test_only_one_scheduler_runs_at_a_time(
    db_pool: DatabasePool, cron_catalog: DefinitionCatalog
) -> None:
    await create_workspace(db_pool, "ws")
    async with db_pool.connection() as leader, leader.transaction():
        await leader.execute(
            "select pg_advisory_xact_lock(%s)", (advisory_lock_key("cron-scheduler"),)
        )

        inserted = await schedule_cron_jobs(
            db_pool, catalog=cron_catalog, now=datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
        )

    assert inserted == 0
    assert await _cron_jobs(db_pool) == []
