"""Async PostgreSQL pool construction and startup compatibility checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from memseek.config import Settings
from memseek.definitions.errors import CollectionDefinitionMismatch

if TYPE_CHECKING:
    from memseek.definitions import DefinitionCatalog


EMBEDDING_METADATA_KEY = "embedding"
RESOLVED_MODEL_METADATA_KEY = "resolved"
PROCESSOR_NAME_METADATA_KEY = "processor"
PROCESSOR_HASH_METADATA_KEY = "processor_config_hash"


class DatabaseCompatibilityError(RuntimeError):
    """Raised when persisted storage is incompatible with this build."""


type DatabaseRow = dict[str, Any]
type DatabaseConnection = AsyncConnection[DatabaseRow]
type DatabasePool = AsyncConnectionPool[DatabaseConnection]


def create_pool(settings: Settings) -> DatabasePool:
    """Build a closed pool whose sessions are fixed to UTC."""

    return AsyncConnectionPool[DatabaseConnection](
        conninfo=settings.database_url,
        min_size=1,
        max_size=max(4, settings.worker_concurrency + settings.index_concurrency),
        open=False,
        kwargs={
            "application_name": "memseek",
            "options": "-c timezone=UTC",
            "row_factory": dict_row,
        },
    )


async def open_pool(pool: DatabasePool, *, wait_timeout_s: float = 30.0) -> None:
    """Open a pool and wait until its minimum connections are usable."""

    try:
        await pool.open()
        await pool.wait(timeout=wait_timeout_s)
    except BaseException:
        # ``wait()`` can fail after background connections were started.  Do
        # not leave those workers or sockets alive on a failed startup.
        await pool.close()
        raise


async def close_pool(pool: DatabasePool) -> None:
    """Close all connections owned by a pool."""

    await pool.close()


async def check_database(pool: DatabasePool) -> bool:
    """Run the lightweight liveness query used by ``/health``."""

    async with pool.connection() as conn:
        result = await conn.execute("select 1 as ok")
        row = await result.fetchone()
        return bool(row and row["ok"] == 1)


def expected_embedding_model(settings: Settings, catalog: DefinitionCatalog) -> str:
    """Return the exact provider:model identity M1 must persist for embeddings."""

    embedding = catalog.models.embedding
    if not settings.llm_fake:
        return embedding.target
    return f"fake:{embedding.model}"


def _migration_required(detail: str) -> DatabaseCompatibilityError:
    return DatabaseCompatibilityError(f"storage compatibility requires migration: {detail}")


def _is_test_database(database_url: str) -> bool:
    name = unquote(urlsplit(database_url).path.rsplit("/", 1)[-1])
    return "test" in name.casefold()


async def _verify_persisted_semantics(
    pool: DatabasePool, settings: Settings, catalog: DefinitionCatalog
) -> None:
    expected_hashes = {
        name: catalog.processor_config_hashes[name] for name in sorted(catalog.processors)
    }
    expected_embedding = expected_embedding_model(settings, catalog)
    embedding_processors = tuple(
        sorted(
            name
            for name, definition in catalog.processors.items()
            if definition.kind == "embedding"
        )
    )
    async with pool.connection() as conn:
        collection_result = await conn.execute(
            """
            select distinct collection, collection_version, collection_hash
            from record
            where collection <> '_system'
            order by collection, collection_version, collection_hash
            """
        )
        stored_collections = await collection_result.fetchall()
        embedding_result = await conn.execute(
            """
            select id::text as id,
                   coalesce(
                     enrichment_meta #>> '{embedding,resolved}',
                     enrichment_meta #>> '{embedding,provider_model}',
                     enrichment_meta ->> 'embedding_model'
                   ) as resolved_model
            from record
            where enriched_at is not null
              and embedding_space = %s
              and (
                coalesce(
                  enrichment_meta #>> '{embedding,resolved}',
                  enrichment_meta #>> '{embedding,provider_model}',
                  enrichment_meta ->> 'embedding_model'
                ) is distinct from %s
                or not exists (
                  select 1
                  from jsonb_each(annotations) annotation
                  where annotation.key = any(%s::text[])
                    and annotation.value ->> 'space' = %s
                )
              )
            limit 1
            """,
            (
                catalog.models.embedding.space,
                expected_embedding,
                list(embedding_processors),
                catalog.models.embedding.space,
            ),
        )
        bad_embedding = await embedding_result.fetchone()
        processor_result = await conn.execute(
            """
            with persisted as (
              select record.id,
                     processor.name,
                     coalesce(
                       record.annotation_meta -> processor.name,
                       record.enrichment_meta -> processor.name
                     ) as metadata
              from record
              cross join lateral (
                select jsonb_object_keys(record.annotations) as name
                union
                select jsonb_object_keys(record.annotation_meta) as name
                union
                select item.key as name
                from jsonb_each(record.enrichment_meta) item
                where item.key <> 'embedding'
                  and jsonb_typeof(item.value) = 'object'
                  and item.value ? 'processor_config_hash'
                  and item.value->>'terminal' = 'true'
              ) processor
            )
            select persisted.id::text as id,
                   persisted.name as processor,
                   coalesce(
                     persisted.metadata ->> 'processor',
                     persisted.metadata ->> 'name',
                     persisted.name
                   ) as semantic_name,
                   coalesce(
                     persisted.metadata ->> 'processor_config_hash',
                     persisted.metadata ->> 'config_hash',
                     persisted.metadata ->> 'processor_hash'
                   ) as stored_hash,
                   expected.value as expected_hash
            from persisted
            left join lateral jsonb_each_text(%s) expected
              on expected.key = persisted.name
            where expected.key is null
               or coalesce(
                    persisted.metadata ->> 'processor',
                    persisted.metadata ->> 'name',
                    persisted.name
                  ) is distinct from persisted.name
               or coalesce(
                    persisted.metadata ->> 'processor_config_hash',
                    persisted.metadata ->> 'config_hash',
                    persisted.metadata ->> 'processor_hash'
                  ) is distinct from expected.value
            limit 1
            """,
            (Jsonb(expected_hashes),),
        )
        bad_processor = await processor_result.fetchone()

        fake_history = False
        if settings.llm_fake and not _is_test_database(settings.database_url):
            provider_processors = tuple(
                sorted(
                    name
                    for name, item in catalog.processors.items()
                    if item.kind == "embedding" or item.source == "llm"
                )
            )
            history_result = await conn.execute(
                """
                select exists (
                  select 1
                  from record
                  where embedding is not null
                     or annotations ?| %s::text[]
                ) as present
                """,
                (list(provider_processors),),
            )
            history_row = await history_result.fetchone()
            fake_history = bool(history_row and history_row["present"])

    for stored in stored_collections:
        try:
            catalog.resolve_stored_collection(
                str(stored["collection"]),
                int(stored["collection_version"]),
                str(stored["collection_hash"]),
            )
        except CollectionDefinitionMismatch as exc:
            raise _migration_required(str(exc)) from exc
    if bad_embedding is not None:
        raise _migration_required(
            "embedding metadata does not match the current space and provider/model "
            f"for record {bad_embedding['id']}"
        )
    if bad_processor is not None:
        raise _migration_required(
            f"processor metadata is missing or incompatible for "
            f"{bad_processor['processor']!r} on record {bad_processor['id']}"
        )
    if fake_history:
        raise _migration_required(
            "fake providers cannot be enabled over existing provider-generated history "
            "outside a dedicated test database"
        )


async def verify_storage_compatibility(
    pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    *,
    semantics: bool = True,
) -> None:
    """Fail startup when the schema or persisted provider semantics are incompatible.

    M1 writers use the exported metadata-key constants above.  Compatibility
    aliases are read for pre-release data, but missing/empty metadata is never
    assumed compatible.

    The catalog is always required, because the embedding dimension the schema
    must provide is declared there.  ``semantics=False`` keeps the check
    structural for processes that serve many workspace packages and therefore
    re-check each resolved catalog at its own operation seam.
    """

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select
              current_setting('TimeZone') as timezone,
              to_regclass('public.workspace') is not null as has_workspace,
              to_regclass('public.workspace_catalog') is not null as has_workspace_catalog,
              to_regclass('public.record') is not null as has_record,
              to_regclass('public.job') is not null as has_job,
              to_regclass('public.artifact_use') is not null as has_artifact_use,
              exists (select 1 from pg_extension where extname = 'vector') as has_vector,
              coalesce((
                select format_type(a.atttypid, a.atttypmod)
                from pg_attribute a
                where a.attrelid = to_regclass('public.record')
                  and a.attname = 'embedding'
                  and not a.attisdropped
              ), '') as embedding_type
            """
        )
        row = await result.fetchone()
    if row is None:
        raise DatabaseCompatibilityError("database compatibility query returned no result")
    if row["timezone"] != "UTC":
        raise DatabaseCompatibilityError("database sessions must use UTC")
    missing = [
        name
        for name in ("workspace", "workspace_catalog", "record", "job", "artifact_use")
        if not bool(row[f"has_{name}"])
    ]
    if missing:
        raise DatabaseCompatibilityError(f"database schema is missing: {', '.join(missing)}")
    if not row["has_vector"]:
        raise DatabaseCompatibilityError("pgvector extension is unavailable")
    expected_type = f"vector({catalog.models.embedding.dimensions})"
    if row["embedding_type"] != expected_type:
        raise DatabaseCompatibilityError(
            f"embedding.dimensions is {catalog.models.embedding.dimensions}, so record.embedding "
            f"must be {expected_type}; found {row['embedding_type']!r}. Changing the stored "
            "dimension requires a schema migration, not only a definition change."
        )
    if semantics:
        await _verify_persisted_semantics(pool, settings, catalog)


@asynccontextmanager
async def pool_lifespan(
    settings: Settings,
    *,
    catalog: DefinitionCatalog | None = None,
    verify: bool = True,
) -> AsyncIterator[DatabasePool]:
    """Own an explicitly opened and closed runtime pool."""

    pool = create_pool(settings)
    try:
        await open_pool(pool)
        if verify:
            if catalog is None:
                from memseek.definitions import load_definition_catalog

                catalog = load_definition_catalog(settings)
            await verify_storage_compatibility(pool, settings, catalog, semantics=False)
        yield pool
    finally:
        if not pool.closed:
            await close_pool(pool)
