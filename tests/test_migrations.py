"""Alembic integration and normative schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from psycopg import errors

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.migrations import (
    NormativeMigrationChangedError,
    apply_migrations,
    build_alembic_config,
    normalize_database_url,
    verify_normative_migration,
)


async def test_migration_is_idempotent_and_schema_is_present(
    settings: Settings, db_pool: DatabasePool
) -> None:
    assert await apply_migrations(settings.database_url) == "0009_general_graph_indexes"
    assert await apply_migrations(settings.database_url) == "0009_general_graph_indexes"
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select
              to_regclass('public.workspace')::text as workspace,
              to_regclass('public.workspace_catalog')::text as workspace_catalog,
              to_regclass('public.record')::text as record,
              to_regclass('public.job')::text as job,
              to_regclass('public.artifact_use')::text as artifact_use,
              (select version_num from alembic_version) as revision,
              exists (select 1 from pg_extension where extname = 'vector') as vector
            """
        )
        row = await result.fetchone()
    assert row == {
        "workspace": "workspace",
        "workspace_catalog": "workspace_catalog",
        "record": "record",
        "job": "job",
        "artifact_use": "artifact_use",
        "revision": "0009_general_graph_indexes",
        "vector": True,
    }


def test_normative_migration_checksum_drift_is_rejected(tmp_path: Path) -> None:
    source = Path("migrations/001_init.sql")
    altered = tmp_path / source.name
    altered.write_bytes(source.read_bytes() + b"\n-- drift\n")
    with pytest.raises(NormativeMigrationChangedError, match=r"001_init\.sql"):
        verify_normative_migration(tmp_path)


def test_alembic_revision_graph_has_one_head(settings: Settings) -> None:
    config = build_alembic_config(settings.database_url)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0009_general_graph_indexes"]


def test_plain_postgresql_url_selects_installed_psycopg_driver() -> None:
    assert (
        normalize_database_url("postgresql://user:secret@db.example/memseek_test")
        == "postgresql+psycopg://user:secret@db.example/memseek_test"
    )


async def test_expected_job_and_record_indexes_exist(
    db_pool: DatabasePool,
) -> None:
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select indexname, indexdef
            from pg_indexes
            where schemaname = 'public'
              and tablename in ('record', 'job')
            """
        )
        definitions = {row["indexname"]: row["indexdef"] for row in await result.fetchall()}
        indexes = set(definitions)
    assert {
        "record_keyed_current",
        "record_vec",
        "record_fts",
        "record_graph_edges_out",
        "record_graph_edges_in",
        "job_ready",
        "job_active_derive",
    } <= indexes
    for name in ("record_graph_edges_out", "record_graph_edges_in"):
        assert "workspace, collection" in definitions[name]
        assert "collection = 'edges'" not in definitions[name]


async def test_normative_workspace_and_job_constraints(db_pool: DatabasePool) -> None:
    with pytest.raises(errors.CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(
                "insert into workspace (id, api_key_hash) values ('bad space', %s)",
                ("0" * 64,),
            )
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('constraints', %s)",
            ("1" * 64,),
        )
    with pytest.raises(errors.CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                insert into job (workspace, kind, derivation, entity)
                values ('constraints', 'index_upsert', 'forbidden', null)
                """
            )
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into job (workspace, kind, payload)
            values ('constraints', 'retention_purge', '{"retention":"test"}'::jsonb)
            """
        )


async def test_normative_record_json_and_embedding_pair_constraints(
    db_pool: DatabasePool,
) -> None:
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values ('record-check', %s)",
            ("2" * 64,),
        )
    with pytest.raises(errors.CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                insert into record (
                  workspace, collection_version, collection_hash, entity, type,
                  content, embedding_space
                )
                values ('record-check', 1, %s, 'entity', 'event', '{}'::jsonb, 'space')
                """,
                ("0" * 64,),
            )
