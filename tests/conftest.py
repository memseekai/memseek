"""PostgreSQL fixtures shared by M0 integration tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from memseek.config import Settings
from memseek.db import DatabasePool, close_pool, create_pool, open_pool
from memseek.migrations import apply_migrations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def catalog_settings(settings: Settings, root: Path) -> Settings:
    """Point one Settings at a self-contained catalog directory.

    Nothing is loaded by default any more, so every test that needs
    definitions names the catalog it is testing against. `resources/` holds the
    reference catalog; `examples/*_catalog/` hold the worked ones.
    """

    return settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "derivations_dir": root / "derivations",
            "triggers_dir": root / "triggers",
            "views_dir": root / "views",
            "artifacts_dir": root / "artifacts",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "search_profiles_file": root / "conf/search_profiles.yaml",
            "rank_default_file": root / "conf/rank_default.yaml",
        }
    )


@pytest.fixture(scope="session")
def bare_settings() -> Settings:
    """Settings with no catalog at all — the shipped default.

    Use this to assert what a service does before anything is published.
    """

    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55432/memseek_test"
    )
    database_name = urlsplit(database_url).path.rsplit("/", 1)[-1]
    if "test" not in database_name.lower():
        pytest.fail(f"refusing to run against a non-test database: {database_name}")
    return Settings(database_url=database_url, llm_fake=True)


@pytest.fixture(scope="session")
def settings(bare_settings: Settings) -> Settings:
    """The reference catalog in `resources/`, loaded explicitly.

    It used to arrive implicitly from the repository root. It does not any
    more: a process ships no definitions unless it is told where they are.
    """

    return catalog_settings(
        bare_settings,
        REPOSITORY_ROOT / "resources",
    ).model_copy(
        update={
            # The reference catalog carries its own processors and its own
            # ranking, because that ranking names a scorer the catalog defines.
            # Models and search profiles stay deployment configuration.
            "models_file": REPOSITORY_ROOT / "conf/models.yaml",
            "search_profiles_file": REPOSITORY_ROOT / "conf/search_profiles.yaml",
        }
    )


@pytest.fixture
def gbrain_settings(bare_settings: Settings) -> Settings:
    """The self-contained gbrain package catalog."""

    return catalog_settings(bare_settings, REPOSITORY_ROOT / "examples" / "gbrain_catalog")


@pytest.fixture(scope="session", autouse=True)
async def migrated_database(settings: Settings) -> None:
    await apply_migrations(settings.database_url)


@pytest.fixture
async def db_pool(settings: Settings) -> AsyncIterator[DatabasePool]:
    pool = create_pool(settings)
    await open_pool(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "truncate table artifact_use, backfill, record_embedding, job, cursor,"
            " record, workspace restart identity cascade"
        )
    try:
        yield pool
    finally:
        await close_pool(pool)
