"""Executable acceptance coverage for the SDK CRM profile quickstart."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.sdk import MemseekClient
from memseek.worker import run_worker_once, worker_lifespan

CATALOG_ROOT = Path("examples/crm_profile_catalog")


async def test_sdk_uploads_crm_package_and_computes_profile(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "sdk-crm-profile")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
    )
    events = [
        {
            "collection": "crm_events",
            "entity": "contact:avery-chen",
            "type": "crm_event",
            "text": "Avery is VP of Product for Acme Cloud.",
            "content": {
                "source": "salesforce",
                "event_kind": "role",
                "account_id": "acme-cloud",
            },
            "dedupe_key": "sdk-crm:role",
        },
        {
            "collection": "crm_events",
            "entity": "contact:avery-chen",
            "type": "crm_event",
            "text": "Avery committed to deliver Northstar by September 30.",
            "content": {
                "source": "hubspot",
                "event_kind": "commitment",
                "account_id": "acme-cloud",
            },
            "dedupe_key": "sdk-crm:commitment",
        },
        {
            "collection": "crm_events",
            "entity": "contact:avery-chen",
            "type": "crm_event",
            "text": "Avery prefers written updates before meetings.",
            "content": {
                "source": "support",
                "event_kind": "preference",
                "account_id": "acme-cloud",
            },
            "dedupe_key": "sdk-crm:preference",
        },
    ]

    fake.reset()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient(
                "http://test",
                credential.api_key,
                client=http_client,
            ) as client,
        ):
            loaded = await client.catalog.publish(
                package="crm_user_profile@2.0.0",
                directory=CATALOG_ROOT,
            )
            assert loaded["package"] == {"name": "crm_user_profile", "version": "2.0.0"}

            ingested = await client.records.ingest_many(events)
            assert len(ingested["inserted"]) == 3
            assert all(not row["ready"] for row in ingested["inserted"])
            role_id, commitment_id, preference_id = [row["id"] for row in ingested["inserted"]]
            fake.enqueue(
                Completion("[10,9,8]"),
                Completion(
                    json.dumps(
                        {
                            "records": [
                                {
                                    "key": "role",
                                    "text": "VP of Product for Acme Cloud.",
                                    "citations": [role_id],
                                },
                                {
                                    "key": "commitments",
                                    "text": "Committed to deliver Northstar by September 30.",
                                    "citations": [commitment_id],
                                },
                                {
                                    "key": "preferences",
                                    "text": "Prefers written updates before meetings.",
                                    "citations": [preference_id],
                                },
                            ]
                        }
                    )
                ),
            )

            async with worker_lifespan(
                settings,
                catalog=load_definition_catalog(settings),
                pool=create_pool(settings),
            ) as runtime:
                result = await run_worker_once(runtime, worker_id="sdk-crm-quickstart")
            assert result.enrichment_ready == 3

            document = await client.document(
                entity="contact:avery-chen",
                collections="user_profiles",
            )
            profile = {item["key"]: item for item in document["beliefs"]}
            assert set(profile) == {"role", "commitments", "preferences"}
            assert all(item["citations"] for item in profile.values())

            runs = await client.runs(
                entity="contact:avery-chen",
                processor="crm_profile",
                operation="derive",
            )
            assert runs["runs"][0]["status"] == "ok"

            brief = await client.render_artifact(
                "crm_profile_brief",
                entity="contact:avery-chen",
                query="role commitments preferences",
            )
            assert "VP of Product" in brief["rendered"]

            # The one-line summary is its own chained derivation now: crm_summary
            # re-synthesizes it from the slots whenever one changes, and cites the
            # slots it summarized (not the raw events). Feed the slot heads as its
            # citations and let the `changed` trigger fold it in over later passes.
            slots = {item["key"]: item["id"] for item in document["beliefs"]}
            fake.enqueue(
                Completion(
                    json.dumps(
                        {
                            "records": [
                                {
                                    "key": "summary",
                                    "text": (
                                        "VP of Product at Acme Cloud, committed to "
                                        "Northstar by Sep 30, prefers written updates."
                                    ),
                                    "citations": [slots["role"], slots["commitments"]],
                                }
                            ]
                        }
                    )
                )
            )
            summary = None
            async with worker_lifespan(
                settings,
                catalog=load_definition_catalog(settings),
                pool=create_pool(settings),
            ) as runtime:
                for _ in range(5):
                    await run_worker_once(runtime, worker_id="sdk-crm-summary")
                    refreshed = await client.document(
                        entity="contact:avery-chen",
                        collections="user_profiles",
                    )
                    summary = next((b for b in refreshed["beliefs"] if b["key"] == "summary"), None)
                    if summary is not None:
                        break
            assert summary is not None, "crm_summary should synthesize a summary slot"
            # It cites the profile slots it summarized, never the raw CRM events.
            assert set(summary["citations"]) <= set(slots.values())
            summary_runs = await client.runs(
                entity="contact:avery-chen",
                processor="crm_summary",
                operation="derive",
            )
            assert summary_runs["runs"][0]["status"] == "ok"
    fake.reset()
