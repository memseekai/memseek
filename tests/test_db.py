"""Pool construction and storage compatibility tests."""

from __future__ import annotations

import logging
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.errors import QueryCanceled
from psycopg.types.json import Jsonb

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import (
    DatabaseCompatibilityError,
    DatabasePool,
    check_database,
    close_pool,
    create_pool,
    expected_embedding_model,
    open_pool,
    verify_storage_compatibility,
)
from memseek.definitions import DefinitionCatalog, load_definition_catalog


async def test_pool_starts_closed_and_sessions_use_utc(settings: Settings) -> None:
    pool = create_pool(settings)
    assert pool.closed
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            result = await conn.execute("select current_setting('TimeZone') as timezone")
            row = await result.fetchone()
        assert row == {"timezone": "UTC"}
    finally:
        await pool.close()
    assert pool.closed


_GUARDRAILS = (
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "temp_file_limit",
)


async def _session_guardrails(settings: Settings) -> dict[str, str]:
    pool = create_pool(settings)
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            shown = {}
            for name in _GUARDRAILS:
                row = await (await conn.execute(f"show {name}")).fetchone()
                assert row is not None
                shown[name] = row[name]
            return shown
    finally:
        await close_pool(pool)


async def test_pooled_sessions_carry_the_configured_guardrails(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "database_statement_timeout_ms": 1_234,
            "database_lock_timeout_ms": 2_345,
            "database_idle_in_transaction_session_timeout_ms": 3_456,
            "database_temp_file_limit_kb": 4_096,
        }
    )

    assert await _session_guardrails(configured) == {
        "statement_timeout": "1234ms",
        "lock_timeout": "2345ms",
        "idle_in_transaction_session_timeout": "3456ms",
        "temp_file_limit": "4MB",
    }


async def test_unset_guardrails_leave_the_server_defaults(settings: Settings) -> None:
    unset = Settings(
        database_url=settings.database_url,
        database_statement_timeout_ms=0,
        database_lock_timeout_ms=0,
        database_idle_in_transaction_session_timeout_ms=0,
        database_temp_file_limit_kb=0,
    )

    assert await _session_guardrails(unset) == {
        "statement_timeout": "0",
        "lock_timeout": "0",
        "idle_in_transaction_session_timeout": "0",
        "temp_file_limit": "-1",
    }


async def test_statement_timeout_cancels_a_runaway_query(settings: Settings) -> None:
    pool = create_pool(settings.model_copy(update={"database_statement_timeout_ms": 50}))
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            with pytest.raises(QueryCanceled, match="statement timeout"):
                await conn.execute("select pg_sleep(5)")
    finally:
        await close_pool(pool)


async def test_temp_file_limit_is_skipped_for_a_role_that_cannot_set_it(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    role = f"memseek_guard_{uuid4().hex[:12]}"
    url = urlsplit(settings.database_url)
    role_url = urlunsplit(url._replace(netloc=f"{role}:{role}@{url.hostname}:{url.port}"))
    async with await AsyncConnection.connect(settings.database_url, autocommit=True) as admin:
        await admin.execute(
            sql.SQL("create role {} login password {}").format(
                sql.Identifier(role), sql.Literal(role)
            )
        )
    caplog.set_level(logging.WARNING, logger="memseek.db")
    try:
        shown = await _session_guardrails(
            settings.model_copy(
                update={"database_url": role_url, "database_temp_file_limit_kb": 4_096}
            )
        )
    finally:
        async with await AsyncConnection.connect(settings.database_url, autocommit=True) as admin:
            await admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))

    assert shown["temp_file_limit"] == "-1"
    assert shown["statement_timeout"] == "1min"
    assert [record.msg for record in caplog.records] == ["database.temp_file_limit_not_applied"]


async def test_database_health_and_storage_compatibility(
    settings: Settings, db_pool: DatabasePool
) -> None:
    assert await check_database(db_pool)
    await verify_storage_compatibility(db_pool, settings, load_definition_catalog(settings))


async def test_stored_collection_hash_drift_requires_migration(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    collection = catalog.resolve_collection("main")
    await create_workspace(db_pool, "collection-drift")
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content
            )
            values ('collection-drift', %s, %s, %s, 'agent-1', 'event', %s)
            """,
            (
                collection.name,
                collection.version,
                "0" * 64,
                Jsonb({"text": "old collection semantics"}),
            ),
        )

    with pytest.raises(DatabaseCompatibilityError, match="collection_definition_mismatch"):
        await verify_storage_compatibility(db_pool, settings, catalog)


class FailingWaitPool:
    def __init__(self) -> None:
        self.closed = True

    async def open(self) -> None:
        self.closed = False

    async def wait(self, *, timeout: float) -> None:  # noqa: ASYNC109 - pool test double
        assert timeout == 0.01
        raise RuntimeError("wait failed")

    async def close(self) -> None:
        self.closed = True


async def test_failed_pool_wait_closes_partial_resources() -> None:
    pool = FailingWaitPool()
    with pytest.raises(RuntimeError, match="wait failed"):
        await open_pool(cast(DatabasePool, pool), wait_timeout_s=0.01)
    assert pool.closed


async def _insert_compatible_annotation(
    pool: DatabasePool,
    catalog: DefinitionCatalog,
    *,
    processor: str,
    annotation: dict[str, object],
    metadata_hash: str | None = None,
    embedding_space: str | None = None,
    embedding_model: str | None = None,
) -> None:
    await create_workspace(pool, "compat")
    processor_hash = metadata_hash or catalog.processor_config_hashes[processor]
    metadata = {
        processor: {
            "processor": processor,
            "processor_config_hash": processor_hash,
        }
    }
    enrichment_meta: dict[str, object] = {}
    embedding: str | None = None
    if embedding_space is not None and embedding_model is not None:
        embedding = f"[{','.join('0' for _ in range(1536))}]"
        enrichment_meta = {"embedding": {"resolved": embedding_model}}
    collection = catalog.resolve_collection("main")
    async with pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, embedding, embedding_space,
              annotations, annotation_meta, enrichment_meta, enriched_at
            )
            values (
              'compat', 'main', %s, %s, 'agent-1', 'event', %s,
              %s::vector, %s, %s, %s, %s, clock_timestamp()
            )
            """,
            (
                collection.version,
                collection.contract_hash,
                Jsonb({"text": "compatibility fixture"}),
                embedding,
                embedding_space,
                Jsonb({processor: annotation}),
                Jsonb(metadata),
                Jsonb(enrichment_meta),
            ),
        )


async def test_processor_hash_metadata_is_checked(
    settings: Settings, db_pool: DatabasePool
) -> None:
    catalog = load_definition_catalog(settings)
    await _insert_compatible_annotation(
        db_pool, catalog, processor="importance", annotation={"value": 5}
    )
    await verify_storage_compatibility(db_pool, settings, catalog)
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set annotation_meta = jsonb_set(
              annotation_meta, '{importance,processor_config_hash}', to_jsonb(%s::text)
            )
            """,
            ("0" * 64,),
        )
    with pytest.raises(DatabaseCompatibilityError, match="importance"):
        await verify_storage_compatibility(db_pool, settings, catalog)


async def test_missing_processor_metadata_requires_migration(
    settings: Settings, db_pool: DatabasePool
) -> None:
    catalog = load_definition_catalog(settings)
    await _insert_compatible_annotation(
        db_pool, catalog, processor="importance", annotation={"value": 5}
    )
    async with db_pool.connection() as conn:
        await conn.execute("update record set annotation_meta = '{}'::jsonb")
    with pytest.raises(DatabaseCompatibilityError, match="importance"):
        await verify_storage_compatibility(db_pool, settings, catalog)


async def test_terminal_optional_processor_metadata_is_compatibility_checked(
    settings: Settings, db_pool: DatabasePool
) -> None:
    catalog = load_definition_catalog(settings)
    await create_workspace(db_pool, "terminal-compat")
    collection = catalog.resolve_collection("main")
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, enrichment_meta, enriched_at
            ) values (
              'terminal-compat', 'main', %s, %s, 'agent-1', 'event', %s, %s, now()
            )
            """,
            (
                collection.version,
                collection.contract_hash,
                Jsonb({"text": "terminal optional fixture"}),
                Jsonb(
                    {
                        "sentiment_v1": {
                            "terminal": True,
                            "processor_config_hash": catalog.processor_config_hashes[
                                "sentiment_v1"
                            ],
                        }
                    }
                ),
            ),
        )
    await verify_storage_compatibility(db_pool, settings, catalog)
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set enrichment_meta = jsonb_set(
              enrichment_meta,
              '{sentiment_v1,processor_config_hash}',
              to_jsonb(%s::text)
            )
            """,
            ("0" * 64,),
        )
    with pytest.raises(DatabaseCompatibilityError, match="sentiment_v1"):
        await verify_storage_compatibility(db_pool, settings, catalog)


async def test_embedding_space_and_resolved_model_metadata_are_checked(
    settings: Settings, db_pool: DatabasePool
) -> None:
    catalog = load_definition_catalog(settings)
    await _insert_compatible_annotation(
        db_pool,
        catalog,
        processor="embedding_v1",
        annotation={"space": catalog.models.embedding.space},
        embedding_space=catalog.models.embedding.space,
        embedding_model=expected_embedding_model(settings, catalog),
    )
    await verify_storage_compatibility(db_pool, settings, catalog)
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set enrichment_meta = jsonb_set(
              enrichment_meta, '{embedding,resolved}', '"other:model"'::jsonb
            )
            """
        )
    with pytest.raises(DatabaseCompatibilityError, match="embedding metadata"):
        await verify_storage_compatibility(db_pool, settings, catalog)


async def test_fake_mode_rejects_provider_history_outside_test_database(
    settings: Settings, db_pool: DatabasePool
) -> None:
    catalog = load_definition_catalog(settings)
    await _insert_compatible_annotation(
        db_pool, catalog, processor="importance", annotation={"value": 5}
    )
    development_settings = settings.model_copy(
        update={"database_url": "postgresql://unused/empty_dev"}
    )
    with pytest.raises(DatabaseCompatibilityError, match="fake providers"):
        await verify_storage_compatibility(db_pool, development_settings, catalog)
