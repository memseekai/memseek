"""Pool construction and storage compatibility tests."""

from __future__ import annotations

from typing import cast

import pytest
from psycopg.types.json import Jsonb

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import (
    DatabaseCompatibilityError,
    DatabasePool,
    check_database,
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
