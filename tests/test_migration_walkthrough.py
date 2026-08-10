"""Executable form of docs/migration-walkthrough.md.

The walkthrough quotes real preflight reports, real counters, and real prune
output.  This drives the same four changes against a real database and asserts
each quoted claim, so the guide cannot drift away from what the service does.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest
import yaml

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.models import WorkspaceCredential
from memseek.sdk import MemseekClient
from memseek.worker import WorkerRuntime, run_worker_once
from memseek.workspace_catalog import WorkspaceCatalogRegistry

TICKETS_V1: dict[str, Any] = {
    "name": "tickets",
    "version": 1,
    "active": True,
    "mode": "event",
    "schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": True,
    },
    "fields": {
        "severity": {
            "path": "annotations.triage_v1.severity",
            "type": "integer",
            "filter": True,
            "sort": True,
        }
    },
    "required_processors": ["embedding_v1", "triage_v1"],
    "optional_processors": ["importance"],
    "search_profile": "pg_default",
}

SUMMARIES: dict[str, Any] = {
    "name": "summaries",
    "version": 1,
    "active": True,
    "mode": "keyed",
    "schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}, "tombstone": {"type": "boolean"}},
        "additionalProperties": True,
    },
    "required_processors": ["embedding_v1"],
    "search_profile": "pg_default",
}

TRIAGE_V1: dict[str, Any] = {
    "name": "triage_v1",
    "kind": "json",
    "source": "llm",
    "input": {"collections": ["tickets"]},
    "model": "cheap",
    "prompt": "Rate the severity of this support ticket from 1 to 5 and say why.",
    "output_schema": {
        "type": "object",
        "required": ["severity"],
        "properties": {
            "severity": {"type": "integer", "minimum": 1, "maximum": 5},
            "reason": {"type": "string"},
        },
    },
    "default_output": {"severity": 3, "reason": "unclassified"},
}

PROCESSORS: list[dict[str, Any]] = [
    {
        "name": "embedding_v1",
        "kind": "embedding",
        "input": {"collections": ["tickets", "summaries"]},
    },
    {
        "name": "importance",
        "kind": "score",
        "source": "constant",
        "input": {"collections": ["tickets"]},
        "scale": [1, 10],
        "value": 5,
    },
    TRIAGE_V1,
]

_SUMMARY_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["records"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "required": ["key", "text", "citations"],
                "properties": {
                    "key": {"type": "string", "enum": ["open_themes"]},
                    "text": {"type": "string", "minLength": 1},
                    "citations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "format": "uuid"},
                    },
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}

SUMMARY_DERIVATION: dict[str, Any] = {
    "name": "account_summary",
    "trigger": {"accumulator": {"metric": "count", "threshold": 3}, "cooldown_s": 60},
    "sources": {
        "new_tickets": {
            "kind": "changes",
            "collections": ["tickets"],
            # The pin the walkthrough argues for.
            "collection_versions": {"tickets": [1]},
            "types": ["ticket"],
            "statuses": ["active"],
            "keyed": False,
            "max_records": 50,
            "max_tokens": 16000,
            "allow_empty": False,
        }
    },
    "model": "cheap",
    "limits": {
        "max_tasks": 1,
        "max_llm_calls": 2,
        "max_retrieved_records": 0,
        "max_visible_records": 60,
        "max_total_tokens": 30000,
        "max_wall_s": 60,
    },
    "tasks": [
        {
            "id": "result",
            "use": "llm",
            "with": {
                "output_schema": _SUMMARY_OUTPUT,
                "prompt": (
                    "Summarize the recurring themes across these tickets for "
                    "{{entity}}.\n\n{{new_tickets.rendered}}\n"
                ),
            },
        }
    ],
    "emit": {
        "from": "{{result.records}}",
        "collection": "summaries",
        "type": "summary",
        "keys": ["open_themes"],
    },
}

VIEW: dict[str, Any] = {
    "name": "recent_tickets",
    "version": 1,
    "active": True,
    "parameters": {"entity": {"type": "string", "required": True}},
    "query": {
        "q": "",
        "mode": "recent",
        "scope": {"entities": ["{{entity}}"], "collections": ["tickets"]},
        "k": 20,
    },
}

ARTIFACT: dict[str, Any] = {
    "name": "ticket_digest",
    "version": 1,
    "active": True,
    "kind": "prompt",
    "lifecycle": "live",
    "parameters": {"entity": {"type": "string", "required": True}},
    "blocks": {
        "tickets": {
            "view": "recent_tickets@1",
            "args": {"entity": "{{entity}}"},
            "max_tokens": 2000,
        }
    },
    "template": "Open tickets for {{entity}}:\n{{tickets}}\n",
}

RANK_DEFAULT = """
candidates: 200
variants:
  hybrid: [sum, [[product, 1.0, [normalize, [max, [[similarity], [text_match]]]]]]]
  vector: [sum, [[product, 1.0, [normalize, [similarity]]]]]
  text: [sum, [[product, 1.0, [normalize, [text_match]]]]]
  recent:
    - sum
    - - [product, 1.0, [decay, [age_hours, occurred_at], {midpoint: 24, exponent: 1}]]
"""

TICKETS = (
    ("Cannot log in to the dashboard", "email"),
    ("Invoice 4471 shows the wrong tax", "email"),
    ("Requesting SSO for our team", "portal"),
    ("Data export finished but is empty", "portal"),
    ("Phone call: renewal questions", "phone"),
    ("Webhook retries are flooding our endpoint", "email"),
)

ENTITY = "acct:northwind"


def catalog_files(
    *,
    collections: list[dict[str, Any]],
    processors: list[dict[str, Any]],
    derivations: list[dict[str, Any]] | None = None,
    version: str,
) -> dict[str, str]:
    """Assemble the support-desk catalog exactly as the walkthrough shows it."""

    all_collections = [*collections, SUMMARIES]
    all_derivations = [SUMMARY_DERIVATION, *(derivations or [])]
    files = {
        "collections/tickets.yaml": yaml.safe_dump({"collections": all_collections}),
        "conf/processors.yaml": yaml.safe_dump({"processors": processors}),
        "conf/rank_default.yaml": RANK_DEFAULT,
        "views/recent_tickets.yaml": yaml.safe_dump({"views": [VIEW]}),
        "artifacts/ticket_digest.yaml": yaml.safe_dump({"artifacts": [ARTIFACT]}),
    }
    names = [item["name"] for item in processors]
    for derivation in all_derivations:
        files[f"derivations/{derivation['name']}.yaml"] = yaml.safe_dump(derivation)
        names.append(derivation["name"])
    files["packages/support.yaml"] = yaml.safe_dump(
        {
            "packages": [
                {
                    "name": "support",
                    "version": version,
                    "collections": [
                        f"{item['name']}@{item['version']}" for item in all_collections
                    ],
                    "processors": names,
                    "views": ["recent_tickets@1"],
                    "artifacts": ["ticket_digest@1"],
                    "search_profiles": ["pg_default"],
                }
            ]
        }
    )
    return files


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "support")


def _app(settings: Settings) -> Any:
    return create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )


async def _drain(settings: Settings, db_pool: DatabasePool, *, passes: int = 120) -> None:
    catalog = load_definition_catalog(settings)
    runtime = WorkerRuntime(
        settings=settings,
        catalog=catalog,
        pool=db_pool,
        catalog_registry=WorkspaceCatalogRegistry(db_pool, settings, catalog),
    )
    for _ in range(passes):
        if not (await run_worker_once(runtime, worker_id="walkthrough")).busy:
            return
    raise AssertionError("worker did not settle within the pass budget")


async def _publish(
    sdk: MemseekClient,
    files: dict[str, str],
    *,
    version: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish the way the guide does, through the client rather than a shell.

    The guide names a directory; this assembles the same catalog in memory, which
    is the other argument to the same call and the same route.  A refused publish
    raises, so reaching the next line is the status assertion.
    """

    return await sdk.catalog.publish_files(
        package=f"support@{version}", files=files, dry_run=dry_run
    )


def _with_channel_declared() -> dict[str, Any]:
    """Change 1: declare the content key that was already being written."""

    grown = copy.deepcopy(TICKETS_V1)
    grown["schema"]["properties"]["channel"] = {"type": "string"}
    grown["fields"]["channel"] = {
        "path": "content.channel",
        "type": "string",
        "filter": True,
        "sort": True,
    }
    return grown


def _triage_v2() -> dict[str, Any]:
    """Change 2: a better prompt under a new name that supersedes the old one."""

    improved = copy.deepcopy(TRIAGE_V1)
    improved["name"] = "triage_v2"
    improved["prompt"] = (
        "Rate this support ticket's severity from 1 to 5. Treat data loss and "
        "billing errors as at least 4."
    )
    improved["supersedes"] = "triage_v1"
    return improved


def _with_supersession() -> dict[str, Any]:
    superseding = _with_channel_declared()
    superseding["optional_processors"] = ["importance", "triage_v2"]
    superseding["fields"]["severity"]["path"] = "annotations.triage_v2.severity"
    return superseding


def _tickets_v2() -> dict[str, Any]:
    """Change 3: channel becomes required and constrained."""

    second = _with_supersession()
    second["version"] = 2
    second["active"] = True
    second["schema"]["required"] = ["text", "channel"]
    second["schema"]["properties"]["channel"] = {
        "type": "string",
        "enum": ["email", "portal", "phone"],
    }
    second["schema"]["additionalProperties"] = False
    return second


MIGRATION: dict[str, Any] = {
    "name": "tickets_v1_to_v2",
    "sources": {
        "legacy": {
            "kind": "changes",
            "collections": ["tickets"],
            "collection_versions": {"tickets": [1]},
            "statuses": ["active"],
            "keyed": False,
            "max_records": 100,
            "max_tokens": 40000,
            "allow_empty": False,
        }
    },
    "model": None,
    "limits": {
        "max_tasks": 1,
        "max_llm_calls": 0,
        "max_retrieved_records": 0,
        "max_visible_records": 100,
        "max_total_tokens": 40000,
        "max_wall_s": 30,
    },
    "tasks": [
        {
            "id": "migrated",
            "use": "map_records",
            "input": {"records": "{{legacy.records}}"},
            "with": {
                "keep": ["text"],
                "set": {"channel": {"from": "content.channel", "default": "email"}},
                "carry_key": False,
            },
        }
    ],
    "emit": {
        "from": "{{migrated}}",
        "collection": "tickets",
        "collection_version": 2,
        "type": "ticket",
        "max_records": 100,
    },
}


async def test_the_support_desk_walkthrough(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Every claim the guide makes, in the order the guide makes it."""

    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sdk = MemseekClient("http://test", workspace.api_key, client=client)
            # ---- Day one -------------------------------------------------
            day_one = catalog_files(
                collections=[TICKETS_V1], processors=PROCESSORS, version="1.0.0"
            )
            await _publish(sdk, day_one, version="1.0.0")
            for text, channel in TICKETS:
                await sdk.records.ingest(
                    collection="tickets",
                    entity=ENTITY,
                    type="ticket",
                    text=text,
                    # Accepted without being declared.
                    content={"channel": channel},
                )
            await _drain(settings, db_pool)

            # ---- Change 1: declare channel ------------------------------
            step1 = catalog_files(
                collections=[_with_channel_declared()],
                processors=PROCESSORS,
                version="1.1.0",
            )
            plan = await _publish(sdk, step1, version="1.1.0", dry_run=True)
            assert plan["verdict"] == "additive"
            assert plan["publishable"] is True
            assert plan["stored_rows"] == 6
            assert plan["blockers"] == []
            change = next(item for item in plan["changes"] if item["family"] == "collection")
            assert change["class"] == "additive"
            assert "new schema properties ['channel']" in change["detail"]
            assert "new declared fields ['channel']" in change["detail"]
            rewrite = plan["rewrites"][0]
            assert rewrite["rows"] == 6
            assert rewrite["reason"] == "additive_contract"
            # The schema was open, so the values already stored must be checked.
            assert rewrite["verify_keys"] == ["channel"]

            published = await _publish(sdk, step1, version="1.1.0")
            assert published["rewritten_records"] == 6

            # The declared field answers for records written before it existed.
            # This is the exact call the guide shows.
            email = await sdk.search(
                query="",
                collections=["tickets"],
                mode="structured",
                k=10,
                where={"channel": {"eq": "email"}},
                order_by=[{"field": "channel", "direction": "asc"}],
            )
            assert len(email["hits"]) == 3

            # An external search backend adopts the new attribute only after a
            # projection rebuild, which a hosted caller asks for by request.
            rebuilt = await sdk.reindex(since_seq=0)
            assert rebuilt["mode"] == "incremental"
            assert rebuilt["enqueued_jobs"] == 1
            # Workspace-scoped, not collection-scoped: every ready record is a
            # projection target, which is more than the six tickets.
            assert rebuilt["target_count"] >= len(TICKETS)
            await _drain(settings, db_pool)

            # ---- Change 2: better triage, applied to history ------------
            improved = [*PROCESSORS, _triage_v2()]
            step2 = catalog_files(
                collections=[_with_supersession()], processors=improved, version="1.2.0"
            )
            plan = await _publish(sdk, step2, version="1.2.0", dry_run=True)
            assert plan["verdict"] == "additive"
            assert plan["publishable"] is True
            collection_change = next(
                item for item in plan["changes"] if item["family"] == "collection"
            )
            assert collection_change["class"] == "additive"
            assert (
                "fields ['severity'] now prefer a superseding annotation"
                in collection_change["detail"]
            )
            assert plan["rewrites"][0]["verify_absent_annotations"] == ["triage_v2"]
            added = next(
                item
                for item in plan["changes"]
                if item["family"] == "processor" and item["name"] == "triage_v2"
            )
            assert added["status"] == "added"
            assert added["class"] == "additive"

            await _publish(sdk, step2, version="1.2.0")

            # The severity field still answers, before any backfill runs, because
            # the supersession fallback reads triage_v1 for every stored ticket.
            severity = await sdk.search(
                query="",
                collections=["tickets"],
                mode="structured",
                k=10,
                where={"severity": {"gte": 1}},
                order_by=[{"field": "severity", "direction": "desc"}],
            )
            assert len(severity["hits"]) == 6

            handle = await sdk.backfill.start(
                collection="tickets", version=1, processor="triage_v2"
            )
            assert handle["state"] == "queued"
            # No budget: the walkthrough's "just migrate everything" default.
            assert handle["max_rows"] is None
            await _drain(settings, db_pool)

            progress = await sdk.backfill.retrieve(handle["id"])
            assert progress["state"] == "done"
            assert progress["scanned"] == 6
            assert progress["annotated"] == 6
            # Confirmed from the first record, which is what the zero means.
            assert progress["cursor_seq"] == 0

            # ---- Change 3: require channel ------------------------------
            frozen = _with_supersession()
            frozen["active"] = False
            step3 = catalog_files(
                collections=[frozen, _tickets_v2()],
                processors=improved,
                derivations=[MIGRATION],
                version="2.0.0",
            )
            plan = await _publish(sdk, step3, version="2.0.0", dry_run=True)
            assert plan["verdict"] == "additive"
            assert plan["publishable"] is True
            second = next(
                item
                for item in plan["changes"]
                if item["family"] == "collection" and item.get("version") == 2
            )
            assert second["status"] == "added"
            assert second["class"] == "additive"
            # The pinned summary source is untouched by the active-version flip.
            assert [item["name"] for item in plan["changes"] if item["family"] == "derivation"] == [
                "tickets_v1_to_v2"
            ]

            await _publish(sdk, step3, version="2.0.0")

            # ---- Change 4: move the old tickets forward -----------------
            run = await sdk.run_processor("tickets_v1_to_v2", entity=ENTITY)
            assert run["enqueued"] is True
            await _drain(settings, db_pool)

            # ---- What is left over --------------------------------------
            # Read-only, and reachable with the workspace key alone.
            prune = await sdk.catalog.prune()

    async with db_pool.connection() as conn:
        by_version = await conn.execute(
            """
            select collection_version as version, count(*) as rows
            from record
            where workspace = %s and collection = 'tickets'
            group by 1 order by 1
            """,
            (workspace.workspace,),
        )
        counts = {int(row["version"]): int(row["rows"]) for row in await by_version.fetchall()}
        channels = await conn.execute(
            """
            select collection_version as version, content ->> 'channel' as channel,
                   count(*) as rows
            from record
            where workspace = %s and collection = 'tickets'
            group by 1, 2 order by 1, 2
            """,
            (workspace.workspace,),
        )
        distribution: dict[int, dict[str, int]] = {}
        for row in await channels.fetchall():
            distribution.setdefault(int(row["version"]), {})[str(row["channel"])] = int(row["rows"])
        lineage = await conn.execute(
            """
            select count(*) as rows from record
            where workspace = %s and collection = 'tickets' and collection_version = 2
              and cardinality(derived_from) > 0
            """,
            (workspace.workspace,),
        )
        lineage_row = await lineage.fetchone()

    # Copied forward, not moved: both versions hold all six.
    assert counts == {1: 6, 2: 6}
    # Channels preserved exactly through the mapping.
    assert distribution[1] == {"email": 3, "phone": 1, "portal": 2}
    assert distribution[2] == distribution[1]
    assert lineage_row is not None
    assert int(lineage_row["rows"]) == 6

    # The prune report the guide quotes, fetched over the same authenticated
    # client that made every other change.
    stale = next(
        item
        for item in prune["candidates"]
        if item["family"] == "collection" and item.get("version") == 1
    )
    assert stale["references"] == 6
    assert stale["reference_kind"] == "records"
    assert stale["safe_to_delete"] is False
    assert prune["safe_to_delete"] == []


async def test_an_unpinned_derivation_source_follows_the_active_version(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The reason the walkthrough pins its source.

    An unpinned source resolves to whichever version is active, so introducing a
    new active version silently changes what the derivation reads — and the
    preflight reports that as reinterpreting rather than letting it pass unseen.
    """

    unpinned = copy.deepcopy(SUMMARY_DERIVATION)
    del unpinned["sources"]["new_tickets"]["collection_versions"]

    def files(collections: list[dict[str, Any]], version: str) -> dict[str, str]:
        built = catalog_files(collections=collections, processors=PROCESSORS, version=version)
        built["derivations/account_summary.yaml"] = yaml.safe_dump(unpinned)
        return built

    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sdk = MemseekClient("http://test", workspace.api_key, client=client)
            await _publish(sdk, files([TICKETS_V1], "1.0.0"), version="1.0.0")

            frozen = copy.deepcopy(TICKETS_V1)
            frozen["active"] = False
            second = copy.deepcopy(TICKETS_V1)
            second["version"] = 2
            second["active"] = True
            plan = await _publish(
                sdk,
                files([frozen, second], "2.0.0"),
                version="2.0.0",
                dry_run=True,
            )

    summary = next(
        item
        for item in plan["changes"]
        if item["family"] == "derivation" and item["name"] == "account_summary"
    )
    assert summary["class"] == "reinterpreting"
    assert summary["differing_fields"] == ["sources"]
    assert summary["required_action"] == "publish under a new derivation name"
