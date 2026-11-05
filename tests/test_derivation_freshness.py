"""Freshness probes and basis receipts agree on what a derivation has consumed."""

from __future__ import annotations

import json
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


def _snapshot(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "snapshot",
        "collections": ["main"],
        "types": ["event"],
        "statuses": ["active"],
        "keyed": False,
        "max_records": 50,
        "max_tokens": 24000,
        "allow_empty": False,
        **overrides,
    }


async def _latest_run(pool: DatabasePool, *, workspace: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select content from record
            where workspace = %s and collection = '_system' and type = 'run'
              and content->>'operation' = 'derive'
            order by seq desc limit 1
            """,
            (workspace,),
        )
        row = await result.fetchone()
    assert row is not None
    return row["content"]


async def test_snapshot_runs_over_the_ready_prefix_below_an_unready_row(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "snapshot-ready-prefix")
    catalog = _profile_catalog(settings, _snapshot())
    occurred = datetime(2026, 1, 15, tzinfo=UTC)
    first = await _insert_event(
        db_pool, catalog, workspace=credential.workspace, text="ready", occurred_at=occurred
    )
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="awaiting enrichment",
        occurred_at=occurred,
        ready=False,
    )
    fake.reset()

    disposition = await _run_profile(db_pool, settings, catalog, workspace=credential.workspace)
    assert disposition == "done"
    run = await _latest_run(db_pool, workspace=credential.workspace)
    async with db_pool.connection() as conn:
        seq_row = await (
            await conn.execute("select seq from record where id = %s", (first,))
        ).fetchone()
    assert seq_row is not None
    freshness = await _profile_freshness(db_pool, catalog, workspace=credential.workspace)

    assert (run["high_seq"], run["basis"]["input_ids"]) == (seq_row["seq"], [str(first)])
    assert freshness == {"dirty": True, "pending_unready": True}


async def test_snapshot_with_only_unready_rows_waits(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "snapshot-all-unready")
    catalog = _profile_catalog(settings, _snapshot(allow_empty=True))
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="awaiting enrichment",
        occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
        ready=False,
    )
    fake.reset()

    disposition = await _run_profile(db_pool, settings, catalog, workspace=credential.workspace)

    assert disposition == "not_ready"


async def _profile_calls(
    pool: DatabasePool, settings: Settings, catalog: DefinitionCatalog, *, workspace: str
) -> int:
    """Run ``profile`` once and return how many model calls the run made."""

    fake.reset()
    assert await _run_profile(pool, settings, catalog, workspace=workspace) == "done"
    return len(fake.completion_calls)


async def test_unchanged_snapshot_basis_skips_the_tasks_until_an_input_changes(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "digest-snapshot")
    catalog = _profile_catalog(settings, _snapshot())
    occurred = datetime(2026, 1, 15, tzinfo=UTC)
    first = await _insert_event(
        db_pool, catalog, workspace=credential.workspace, text="first", occurred_at=occurred
    )

    initial = await _profile_calls(db_pool, settings, catalog, workspace=credential.workspace)
    initial_run = await _latest_run(db_pool, workspace=credential.workspace)
    repeat = await _profile_calls(db_pool, settings, catalog, workspace=credential.workspace)
    repeat_run = await _latest_run(db_pool, workspace=credential.workspace)
    second = await _insert_event(
        db_pool, catalog, workspace=credential.workspace, text="second", occurred_at=occurred
    )
    changed = await _profile_calls(db_pool, settings, catalog, workspace=credential.workspace)
    changed_run = await _latest_run(db_pool, workspace=credential.workspace)

    assert (initial, repeat, changed) == (1, 0, 1)
    assert initial_run["basis"]["input_ids"] == [str(first)]
    assert repeat_run["basis"]["input_ids"] == []
    assert repeat_run["basis"]["digest"] == initial_run["basis"]["digest"]
    assert repeat_run["high_seq"] == initial_run["high_seq"]
    assert changed_run["basis"]["input_ids"] == [str(first), str(second)]
    assert changed_run["basis"]["digest"] != initial_run["basis"]["digest"]


async def test_a_definition_change_reruns_an_unchanged_snapshot(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "digest-definition")
    catalog = _profile_catalog(settings, _snapshot())
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="first",
        occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    initial = await _profile_calls(db_pool, settings, catalog, workspace=credential.workspace)
    profile = catalog.derivations["profile"]
    edited = replace(
        catalog,
        derivations=MappingProxyType(
            {
                **catalog.derivations,
                "profile": profile.model_copy(update={"definition_hash": "edited"}),
            }
        ),
    )

    rerun = await _profile_calls(db_pool, settings, edited, workspace=credential.workspace)

    assert (initial, rerun) == (1, 1)


async def test_an_allow_empty_snapshot_never_skips(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "digest-allow-empty")
    catalog = _profile_catalog(settings, _snapshot(allow_empty=True))
    await _insert_event(
        db_pool,
        catalog,
        workspace=credential.workspace,
        text="first",
        occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
    )

    calls = [
        await _profile_calls(db_pool, settings, catalog, workspace=credential.workspace)
        for _ in range(2)
    ]

    assert calls == [1, 1]


async def test_stale_citation_repair_reruns_when_a_cited_parent_moves_again(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "digest-citations")
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
    workspace = credential.workspace
    role = await _insert_profile_fact(db_pool, catalog, workspace=workspace, key="role")
    await _insert_profile_fact(
        db_pool, catalog, workspace=workspace, key="timeline", derived_from=(role,)
    )
    await _insert_profile_fact(db_pool, catalog, workspace=workspace, key="role")

    stale = await _profile_calls(db_pool, settings, catalog, workspace=workspace)
    unchanged = await _profile_calls(db_pool, settings, catalog, workspace=workspace)
    await _insert_profile_fact(db_pool, catalog, workspace=workspace, key="role")
    moved = await _profile_calls(db_pool, settings, catalog, workspace=workspace)

    assert (stale, unchanged, moved) == (1, 0, 1)


async def _role_versions(pool: DatabasePool, *, workspace: str) -> list[tuple[str, list[UUID]]]:
    async with pool.connection() as conn:
        await conn.execute(
            "update record set enriched_at = now() where workspace = %s and enriched_at is null",
            (workspace,),
        )
        result = await conn.execute(
            """
            select content->>'text' as text, derived_from[2:] as citations
            from record
            where workspace = %s and collection = 'profiles' and key = 'role'
            order by seq
            """,
            (workspace,),
        )
        return [(row["text"], row["citations"]) for row in await result.fetchall()]


async def test_an_output_equal_to_its_head_is_not_written_again(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "unchanged-output")
    workspace = credential.workspace
    catalog = _profile_catalog(settings, _snapshot())
    occurred = datetime(2026, 1, 15, tzinfo=UTC)

    async def run_with(role: str, citation: UUID) -> str:
        event = f"evidence for {role} via {citation}"
        await _insert_event(db_pool, catalog, workspace=workspace, text=event, occurred_at=occurred)
        await _role_versions(db_pool, workspace=workspace)
        fake.reset()
        fake.enqueue(
            json.dumps({"records": [{"key": "role", "text": role, "citations": [str(citation)]}]})
        )
        assert await _run_profile(db_pool, settings, catalog, workspace=workspace) == "done"
        return (await _latest_run(db_pool, workspace=workspace))["status"]

    first = await _insert_event(
        db_pool, catalog, workspace=workspace, text="first", occurred_at=occurred
    )
    second = await _insert_event(
        db_pool, catalog, workspace=workspace, text="second", occurred_at=occurred
    )
    statuses = [await run_with("VP of Product.", first)]
    statuses.append(await run_with("VP of Product.", first))
    statuses.append(await run_with("VP of Product.", second))
    statuses.append(await run_with("CTO.", second))

    assert statuses == ["ok", "noop", "ok", "ok"]
    assert await _role_versions(db_pool, workspace=workspace) == [
        ("VP of Product.", [first]),
        ("VP of Product.", [second]),
        ("CTO.", [second]),
    ]
