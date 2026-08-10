"""Definition evolution end to end, through the real service boundary.

Every test here starts from a published package over real stored records, because
that is the only situation in which evolution is hard: an empty workspace can
publish anything.
"""

from __future__ import annotations

import copy

import httpx
import pytest
from evolution_catalog import (
    COLLECTION,
    MIGRATION,
    PROCESSORS,
    build_app,
    catalog_files,
    enrich,
    ingest,
    publish,
)

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.evolution import migrate_collection_hashes, prune_definitions
from memseek.models import WorkspaceCredential


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "evolution")


async def test_binding_edits_publish_over_existing_records(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Adding an optional processor and widening routing no longer needs a version."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await publish(client, headers, catalog_files())
            assert first.status_code == 200, first.text
            ingested = await ingest(client, headers, text="the first note")
            assert ingested.status_code == 200, ingested.text

            bound = copy.deepcopy(COLLECTION)
            bound["optional_processors"] = ["tone_v1"]
            bound["allowed_search_profiles"] = ["pg_default"]
            report = await publish(
                client, headers, catalog_files(collections=[bound]), dry_run=True
            )
            assert report.status_code == 200, report.text
            body = report.json()
            assert body["publishable"] is True
            assert body["verdict"] == "invisible"
            change = next(item for item in body["changes"] if item["family"] == "collection")
            assert change["class"] == "invisible"
            assert change["detail"] == "bindings changed; the record contract is unchanged"

            published = await publish(client, headers, catalog_files(collections=[bound]))
            assert published.status_code == 200, published.text
            assert published.json()["rewritten_records"] == 0


async def test_additive_schema_change_publishes_and_rewrites_stored_hashes(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Declaring a new optional property is a publish, not a migration project."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            for index in range(3):
                assert (
                    await ingest(client, headers, text=f"note {index}", channel="email")
                ).status_code == 200

            grown = copy.deepcopy(COLLECTION)
            grown["schema"]["properties"]["channel"] = {"type": "string"}
            grown["fields"] = {
                "channel": {
                    "path": "content.channel",
                    "type": "string",
                    "filter": True,
                    "sort": True,
                }
            }

            preflight = await publish(
                client, headers, catalog_files(collections=[grown]), dry_run=True
            )
            assert preflight.status_code == 200, preflight.text
            plan = preflight.json()
            assert plan["publishable"] is True
            assert plan["verdict"] == "additive"
            assert plan["rewrites"][0]["rows"] == 3
            assert plan["rewrites"][0]["reason"] == "additive_contract"
            # The schema was open, so the existing values had to be verified.
            assert plan["rewrites"][0]["verify_keys"] == ["channel"]

            published = await publish(client, headers, catalog_files(collections=[grown]))
            assert published.status_code == 200, published.text
            assert published.json()["rewritten_records"] == 3

            await enrich(settings, db_pool, workspace.workspace)

            # The newly declared field answers immediately for rows written before
            # it existed: PostgreSQL resolves declared paths at query time.
            found = await client.post(
                "/search",
                headers=headers,
                json={
                    "mode": "structured",
                    "k": 10,
                    "scope": {"collections": ["notes"]},
                    "where": {"channel": {"eq": "email"}},
                    "order_by": [{"field": "channel", "direction": "asc"}],
                },
            )
            assert found.status_code == 200, found.text
            assert len(found.json()["hits"]) == 3

    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select distinct collection_hash from record where workspace = %s and collection = %s",
            (workspace.workspace, "notes"),
        )
        stored = [str(row["collection_hash"]) for row in await result.fetchall()]
    assert len(stored) == 1


async def test_additive_publish_is_refused_when_a_stored_value_contradicts_it(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """An open schema's existing values decide whether a new property is additive."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            # This row already carries a numeric channel, which the declaration below
            # would reject.
            assert (await ingest(client, headers, text="odd", channel=7)).status_code == 200

            grown = copy.deepcopy(COLLECTION)
            grown["schema"]["properties"]["channel"] = {"type": "string"}
            refused = await publish(client, headers, catalog_files(collections=[grown]))

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error"] == "catalog_incompatible"
    blocker = body["compatibility"]["blockers"][0]
    assert "value the new schema rejects" in blocker["reasons"][0]
    assert "version 2" in blocker["required_action"]


async def test_reinterpreting_change_is_refused_with_a_named_action(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            assert (await ingest(client, headers, text="kept")).status_code == 200

            narrowed = copy.deepcopy(COLLECTION)
            narrowed["schema"]["required"] = ["text", "channel"]
            narrowed["schema"]["properties"]["channel"] = {"type": "string"}

            preflight = await publish(
                client, headers, catalog_files(collections=[narrowed]), dry_run=True
            )
            refused = await publish(client, headers, catalog_files(collections=[narrowed]))
            # The refusal changed nothing: the original contract is still installed.
            still_valid = await ingest(client, headers, text="after the refusal")

    assert preflight.status_code == 200
    assert preflight.json()["publishable"] is False
    assert refused.status_code == 409
    blocker = refused.json()["compatibility"]["blockers"][0]
    assert blocker["rows"] == 1
    assert "schema.required gained ['channel']" in blocker["reasons"]
    assert blocker["required_action"] == (
        "add notes version 2 with this change and keep version 1 in the package"
    )
    assert still_valid.status_code == 200


async def test_a_new_version_alongside_the_old_one_publishes(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The documented escape hatch keeps working, and the report explains it."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            assert (await ingest(client, headers, text="legacy")).status_code == 200

            frozen = copy.deepcopy(COLLECTION)
            frozen["active"] = False
            second = copy.deepcopy(COLLECTION)
            second["version"] = 2
            second["schema"]["required"] = ["text", "channel"]
            second["schema"]["properties"]["channel"] = {"type": "string"}

            published = await publish(
                client,
                headers,
                catalog_files(collections=[frozen, second], version="2.0.0"),
                package="evolving@2.0.0",
            )
            collections = await client.get("/collections", headers=headers)

    assert published.status_code == 200, published.text
    listed = {(item["name"], item["version"]): item for item in collections.json()["collections"]}
    assert listed[("notes", 1)]["active"] is False
    assert listed[("notes", 2)]["active"] is True
    # Both hashes are exposed so an author can see which identity records store.
    assert listed[("notes", 1)]["contract_hash"] != listed[("notes", 1)]["hash"]


async def test_processor_change_is_reported_even_though_it_publishes(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A changed prompt never fails, so the preflight is the only thing that says so."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200

            reworded = copy.deepcopy(PROCESSORS)
            tone = next(item for item in reworded if item["name"] == "tone_v1")
            tone["prompt"] = "Classify the tone precisely."
            report = await publish(
                client, headers, catalog_files(processors=reworded), dry_run=True
            )

    assert report.status_code == 200, report.text
    body = report.json()
    assert body["publishable"] is True
    assert body["verdict"] == "reinterpreting"
    change = next(item for item in body["changes"] if item["family"] == "processor")
    assert change["name"] == "tone_v1"
    assert change["class"] == "reinterpreting"
    assert change["required_action"] == "publish under a new processor name"
    assert "never recomputed" in change["detail"]


async def test_contract_hash_migration_is_idempotent_and_reports_drift(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A workspace written before the split is healed once, then left alone."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            assert (await ingest(client, headers, text="written earlier")).status_code == 200

    from memseek.workspace_catalog import WorkspaceCatalogRegistry

    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    notes = catalog.collections[("notes", 1)]

    # Simulate a pre-split row by restoring the whole-definition hash.
    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set collection_hash = %s where workspace = %s and collection = 'notes'",
            (notes.definition_hash, workspace.workspace),
        )

    planned = await migrate_collection_hashes(
        db_pool, workspace=workspace.workspace, catalog=catalog, dry_run=True
    )
    assert planned.rewritten == 0
    assert planned.groups[0]["reason"] == "generation_upgrade"

    first = await migrate_collection_hashes(db_pool, workspace=workspace.workspace, catalog=catalog)
    assert first.rewritten == 1
    assert first.as_json()["complete"] is True

    second = await migrate_collection_hashes(
        db_pool, workspace=workspace.workspace, catalog=catalog
    )
    assert second.rewritten == 0

    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set collection_hash = %s where workspace = %s and collection = 'notes'",
            ("c" * 64, workspace.workspace),
        )
    drifted = await migrate_collection_hashes(
        db_pool, workspace=workspace.workspace, catalog=catalog
    )
    assert drifted.rewritten == 0
    assert drifted.as_json()["complete"] is False


async def test_prune_reports_only_what_nothing_references(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            frozen = copy.deepcopy(COLLECTION)
            frozen["active"] = False
            second = copy.deepcopy(COLLECTION)
            second["version"] = 2
            assert (
                await publish(client, headers, catalog_files(collections=[frozen, second]))
            ).status_code == 200
            # This row lands in the active version 2, leaving version 1 unreferenced.
            assert (await ingest(client, headers, text="current")).status_code == 200

    from memseek.workspace_catalog import WorkspaceCatalogRegistry

    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)
    report = await prune_definitions(db_pool, workspace=workspace.workspace, catalog=catalog)

    payload = report.as_json()
    assert "collection:notes@1" in payload["safe_to_delete"]
    inactive = next(
        item
        for item in payload["candidates"]
        if item["family"] == "collection" and item["version"] == 1
    )
    assert inactive["references"] == 0
    assert inactive["detail"] == "no record was written under this contract"
    # The active version is never offered for deletion.
    assert not any(item.get("version") == 2 for item in payload["candidates"])


async def test_compatibility_route_reports_the_installed_catalog(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            assert (await ingest(client, headers, text="stored")).status_code == 200
            current = await client.get("/catalog/compatibility", headers=headers)

    assert current.status_code == 200, current.text
    body = current.json()
    # A catalog compared with itself is clean by construction.
    assert body["publishable"] is True
    assert body["verdict"] == "invisible"
    assert body["changes"] == []
    assert body["stored_rows"] == 1


async def test_prune_route_reports_the_same_candidates_over_http(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A caller with no terminal can still ask what is safe to retire.

    `GET /catalog/prune` is the deployed form of `memseek catalog-prune`, and the
    SDK reaches it without the caller assembling a request.
    """

    from memseek.sdk import MemseekClient

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            frozen = copy.deepcopy(COLLECTION)
            frozen["active"] = False
            second = copy.deepcopy(COLLECTION)
            second["version"] = 2
            assert (
                await publish(client, headers, catalog_files(collections=[frozen, second]))
            ).status_code == 200
            assert (await ingest(client, headers, text="current")).status_code == 200

            sdk = MemseekClient("http://test", workspace.api_key, client=client)
            report = await sdk.catalog.prune()

    assert "collection:notes@1" in report["safe_to_delete"]
    inactive = next(
        item
        for item in report["candidates"]
        if item["family"] == "collection" and item["version"] == 1
    )
    assert inactive["references"] == 0
    assert inactive["safe_to_delete"] is True


async def test_cursor_rebinding_over_http_records_the_same_decision(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The rebind route is refused for the same reason the function is.

    Rebinding a cursor that was never established is a caller mistake, and it
    reports as one instead of silently creating a cursor at the origin.
    """

    from memseek.sdk import MemseekClient, MemseekHTTPError

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            sdk = MemseekClient("http://test", workspace.api_key, client=client)
            with pytest.raises(MemseekHTTPError) as refused:
                await sdk.rebind_cursor("archive_notes", entity="maria", policy="carry")

    # The fixture's migration reads a snapshot, which keeps no cursor to rebind.
    assert refused.value.status_code == 422
    body = refused.value.payload
    assert isinstance(body, dict)
    assert body["error"] == "not_a_changes_source"


async def test_cursor_rebinding_is_refused_without_an_established_cursor(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A cursor that never existed cannot be repointed."""

    from memseek.evolution import EvolutionError, rebind_cursor
    from memseek.workspace_catalog import WorkspaceCatalogRegistry

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200

    registry = WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace.workspace)

    with pytest.raises(EvolutionError) as unknown:
        await rebind_cursor(
            db_pool,
            workspace=workspace.workspace,
            derivation="absent",
            entity="user:ana",
            policy="reset",
            catalog=catalog,
            settings=settings,
        )
    assert unknown.value.code == "unknown_derivation"

    # The fixture's migration reads a snapshot, which keeps no cursor to rebind.
    with pytest.raises(EvolutionError) as wrong_mode:
        await rebind_cursor(
            db_pool,
            workspace=workspace.workspace,
            derivation="archive_notes",
            entity="user:ana",
            policy="reset",
            catalog=catalog,
            settings=settings,
        )
    assert wrong_mode.value.code == "not_a_changes_source"


async def test_cursor_rebinding_records_an_audited_decision(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Both policies write an audit naming the old and new source hashes."""

    from memseek.derive.basis import source_contract_hash
    from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION
    from memseek.evolution import rebind_cursor

    changes = copy.deepcopy(MIGRATION)
    changes["name"] = "rolling_archive"
    changes["sources"]["legacy"]["kind"] = "changes"
    files = catalog_files()
    files["derivations/archive_notes.yaml"] = __import__("yaml").safe_dump(changes)
    files["packages/evolving.yaml"] = files["packages/evolving.yaml"].replace(
        "archive_notes", "rolling_archive"
    )

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            published = await publish(client, headers, files)
            assert published.status_code == 200, published.text

    registry_catalog = (
        await __import__("memseek.workspace_catalog", fromlist=["WorkspaceCatalogRegistry"])
        .WorkspaceCatalogRegistry(db_pool, settings, load_definition_catalog(settings))
        .get(workspace.workspace)
    )
    definition = registry_catalog.derivations["rolling_archive"]

    # Stand in for a completed incremental run: the cursor is the run history.
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, status, content, enriched_at
            ) values (%s, '_system', %s, %s, 'user:ana', 'run', 'active', %s, now())
            """,
            (
                workspace.workspace,
                SYSTEM_COLLECTION_VERSION,
                SYSTEM_COLLECTION_HASH,
                __import__("psycopg.types.json", fromlist=["Jsonb"]).Jsonb(
                    {
                        "text": "run",
                        "derivation": "rolling_archive",
                        "status": "ok",
                        "through_seq": 42,
                        "source_hash": "0" * 64,
                    }
                ),
            ),
        )

    carried = await rebind_cursor(
        db_pool,
        workspace=workspace.workspace,
        derivation="rolling_archive",
        entity="user:ana",
        policy="carry",
        catalog=registry_catalog,
        settings=settings,
    )
    assert carried.previous_watermark == 42
    assert carried.watermark == 42
    assert carried.previous_source_hash == "0" * 64
    assert carried.source_hash == source_contract_hash(definition)

    reset = await rebind_cursor(
        db_pool,
        workspace=workspace.workspace,
        derivation="rolling_archive",
        entity="user:ana",
        policy="reset",
        catalog=registry_catalog,
        settings=settings,
    )
    assert reset.watermark == 0

    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select content ->> 'policy' as policy, content ->> 'kind' as kind
            from record
            where workspace = %s and collection = '_system'
              and content ->> 'kind' = 'cursor_rebind'
            order by seq
            """,
            (workspace.workspace,),
        )
        audits = [(row["policy"], row["kind"]) for row in await result.fetchall()]
    assert audits == [("carry", "cursor_rebind"), ("reset", "cursor_rebind")]
