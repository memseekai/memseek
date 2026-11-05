"""Worker queue lanes against a real database: cron pages, leases, and sweeps."""

from __future__ import annotations

from typing import Any, LiteralString

import pytest
from psycopg.types.json import Jsonb
from reference_catalog import declare_test_sources, materialize_reference_catalog

import memseek.worker as worker_module
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
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
