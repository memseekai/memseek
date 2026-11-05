"""Freshness probes and basis receipts agree on what a derivation has consumed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.derive.runner import enqueue_derivation_job, process_derivation_job
from memseek.derive.schema import PipelineDefinition
from memseek.freshness import compute_freshness
from memseek.jobs import claim_job
from memseek.llm.fake import fake

ENTITY = "maria"


def _profile_catalog(settings: Settings, new_events: dict[str, Any]) -> DefinitionCatalog:
    """The reference catalog with ``profile`` driven by a replacement source."""

    catalog = load_definition_catalog(settings)
    profile = catalog.derivations["profile"]
    sources = {**profile.model_dump(mode="python", by_alias=True)["sources"]}
    sources["new_events"] = new_events
    definition = PipelineDefinition.model_validate(
        {**profile.model_dump(mode="python", by_alias=True), "sources": sources}
    )
    definition = definition.model_copy(update={"definition_hash": profile.definition_hash})
    return replace(
        catalog,
        derivations=MappingProxyType({**catalog.derivations, "profile": definition}),
    )


def _windowed_snapshot(since: str, until: str) -> dict[str, Any]:
    return {
        "kind": "snapshot",
        "collections": ["main"],
        "types": ["event"],
        "statuses": ["active"],
        "keyed": False,
        "max_records": 50,
        "max_tokens": 24000,
        "allow_empty": False,
        "window": {"since": since, "until": until},
    }


async def _insert_event(
    pool: DatabasePool,
    catalog: DefinitionCatalog,
    *,
    workspace: str,
    text: str,
    occurred_at: datetime,
    ready: bool = True,
) -> UUID:
    collection = catalog.resolve_collection("main")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, occurred_at, enriched_at
            ) values (%s, %s, %s, %s, %s, 'event', %s, %s,
                      case when %s then now() else null end)
            returning id
            """,
            (
                workspace,
                collection.name,
                collection.version,
                collection.contract_hash,
                ENTITY,
                Jsonb({"text": text}),
                occurred_at,
                ready,
            ),
        )
        row = await result.fetchone()
    assert row is not None
    return row["id"]


async def _run_profile(
    pool: DatabasePool, settings: Settings, catalog: DefinitionCatalog, *, workspace: str
) -> str:
    await enqueue_derivation_job(pool, workspace=workspace, derivation="profile", entity=ENTITY)
    claimed = await claim_job(
        pool,
        worker_id="freshness-test",
        kinds=("derive",),
        derivations=("profile",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    result = await process_derivation_job(pool, claimed=claimed, settings=settings, catalog=catalog)
    return result.disposition


async def _profile_freshness(
    pool: DatabasePool, catalog: DefinitionCatalog, *, workspace: str
) -> dict[str, Any]:
    async with pool.connection() as conn:
        entries = await compute_freshness(conn, workspace=workspace, entity=ENTITY, catalog=catalog)
    (entry,) = [item.as_json() for item in entries if item.derivation == "profile"]
    return {key: entry[key] for key in ("dirty", "pending_unready")}


async def test_windowed_snapshot_is_clean_after_a_run_despite_newer_out_of_window_rows(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "fresh-window")
    catalog = _profile_catalog(
        settings, _windowed_snapshot("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    )
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="in window",
        occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="newer row, after the window",
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    fake.reset()

    before = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)
    disposition = await _run_profile(db_pool, settings, catalog, workspace=credential.workspace)
    after = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)

    assert before == {"dirty": True, "pending_unready": False}
    assert disposition == "done"
    assert after == {"dirty": False, "pending_unready": False}


async def test_windowed_snapshot_turns_dirty_for_a_new_in_window_row(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "fresh-window-new")
    catalog = _profile_catalog(
        settings, _windowed_snapshot("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    )
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="in window",
        occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    fake.reset()
    assert await _run_profile(db_pool, settings, catalog, workspace=credential.workspace) == "done"
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="late arrival inside the window",
        occurred_at=datetime(2026, 1, 20, tzinfo=UTC),
        ready=False,
    )

    entry = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)

    assert entry == {"dirty": True, "pending_unready": True}


async def _insert_profile_fact(
    pool: DatabasePool,
    catalog: DefinitionCatalog,
    *,
    workspace: str,
    key: str,
    derived_from: tuple[UUID, ...] = (),
) -> UUID:
    collection = catalog.resolve_collection("profiles")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, key, type, content, derived_from, enriched_at
            ) values (%s, %s, %s, %s, %s, %s, 'fact', %s, %s::uuid[], now())
            returning id
            """,
            (
                workspace,
                collection.name,
                collection.version,
                collection.contract_hash,
                ENTITY,
                key,
                Jsonb({"text": f"{key} value"}),
                list(derived_from),
            ),
        )
        row = await result.fetchone()
    assert row is not None
    return row["id"]


async def test_stale_citations_is_dirty_only_while_a_citation_is_stale(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "fresh-stale-citations")
    catalog = _profile_catalog(
        settings,
        {
            "kind": "stale_citations",
            "collections": ["profiles"],
            "types": ["fact"],
            "statuses": ["active"],
            "keyed": True,
            "max_records": 50,
            "max_tokens": 24000,
            "allow_empty": False,
        },
    )
    role = await _insert_profile_fact(db_pool, catalog, workspace=credential.workspace, key="role")
    await _insert_profile_fact(
        db_pool, catalog, workspace=credential.workspace, key="timeline", derived_from=(role,)
    )

    current = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)
    await _insert_profile_fact(db_pool, catalog, workspace=credential.workspace, key="role")
    stale = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)

    assert current == {"dirty": False, "pending_unready": False}
    assert stale == {"dirty": True, "pending_unready": False}
