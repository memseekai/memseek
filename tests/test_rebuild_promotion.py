"""Corpus replacement, divergence, and guarded promotion acceptance coverage."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.worker import WorkerRuntime, run_worker_once, worker_lifespan


def _crm_catalog(settings: Settings) -> DefinitionCatalog:
    root = Path("examples/crm_profile_catalog")
    crm_settings = settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "triggers_dir": root / "triggers",
            "views_dir": root / "views",
            "artifacts_dir": root / "artifacts",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "derivations_dir": root / "derivations",
        }
    )
    return load_definition_catalog(crm_settings)


async def _insert_ready_event(
    pool: DatabasePool, catalog: DefinitionCatalog, *, workspace: str
) -> str:
    collection = catalog.resolve_collection("crm_events")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, enriched_at
            ) values (%s, %s, %s, %s, 'contact:avery-chen', 'crm_event', %s, now())
            returning id
            """,
            (
                workspace,
                collection.name,
                collection.version,
                collection.contract_hash,
                Jsonb(
                    {
                        "text": "Avery is VP of Product and wants to launch Northstar.",
                        "source": "salesforce",
                        "event_kind": "role",
                        "account_id": "acme-cloud",
                    }
                ),
            ),
        )
        row = await result.fetchone()
    assert row is not None
    return str(row["id"])


def _replacement(source_id: str) -> str:
    return json.dumps(
        {
            "records": [
                {
                    "key": "role",
                    "text": "VP of Product.",
                    "citations": [source_id],
                },
                {"key": "commitments", "retract": True, "citations": []},
                {"key": "preferences", "retract": True, "citations": []},
                {"key": "open_threads", "retract": True, "citations": []},
                {
                    "key": "goals",
                    "text": "Launch Northstar.",
                    "citations": [source_id],
                },
            ]
        }
    )


async def _run_rebuild(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    settings: Settings,
    catalog: DefinitionCatalog,
    source_id: str,
) -> str:
    # The directly inserted ready fixture still receives the catalog's optional
    # JSON annotation before the worker reaches the manual derive job.
    fake.enqueue(
        Completion('[{"stage":"active","churn_risk":0.0}]'),
        Completion(_replacement(source_id)),
    )
    queued = await client.post(
        "/processors/crm_profile_rebuild/run",
        headers=headers,
        json={"entity": "contact:avery-chen"},
    )
    assert queued.status_code == 200, queued.text
    worker_pool = create_pool(settings)
    async with worker_lifespan(
        settings, catalog=catalog, pool=worker_pool, verify_storage=False
    ) as runtime:
        result = await run_worker_once(
            WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
            worker_id="crm-rebuild-worker",
        )
        assert result.derivation_jobs == 1
    job = await client.get(f"/jobs/{queued.json()['job_id']}", headers=headers)
    assert job.status_code == 200, job.text
    run_id = job.json()["successful_run_id"]
    assert isinstance(run_id, str)
    return run_id


async def test_corpus_rebuild_divergence_promotion_and_stale_guard(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = _crm_catalog(settings)
    credential = await create_workspace(db_pool, "crm-rebuild-promotion")
    source_id = await _insert_ready_event(db_pool, catalog, workspace=credential.workspace)
    fake.reset()
    app = create_app(settings, catalog=catalog, pool=create_pool(settings), verify_storage=False)
    headers = {"Authorization": f"Bearer {credential.api_key}"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_run_id = await _run_rebuild(
                client,
                headers=headers,
                settings=settings,
                catalog=catalog,
                source_id=source_id,
            )
            first = await client.get(f"/runs/{first_run_id}", headers=headers)
            assert first.status_code == 200, first.text
            content = first.json()["run"]["content"]
            assert content["source_kind"] == "snapshot"
            assert content["candidate_set"]["effect"] == "replace"
            assert content["candidate_set"]["coverage"] == "complete"
            assert {item["change"] for item in content["candidate_set"]["divergence"]} == {
                "added",
                "unchanged",
            }
            assert len(content["task_trace"]) == 1
            assert content["task_trace"][0]["task"] == "result"
            assert content["task_trace"][0]["use"] == "llm"
            assert source_id in content["task_trace"][0]["citation_ids"]

            async with db_pool.connection() as conn:
                result = await conn.execute(
                    """
                    select id, key, status, content, derived_from
                    from record where workspace = %s and run_id = %s
                      and collection = 'user_profiles'
                    order by key
                    """,
                    (credential.workspace, first_run_id),
                )
                draft_rows = await result.fetchall()
                active_result = await conn.execute(
                    """
                    select count(*) as count from record
                    where workspace = %s and entity = 'contact:avery-chen'
                      and collection = 'user_profiles' and status = 'active'
                    """,
                    (credential.workspace,),
                )
                active_before = await active_result.fetchone()
            assert len(draft_rows) == 5
            assert {row["status"] for row in draft_rows} == {"draft"}
            assert active_before is not None
            assert active_before["count"] == 0
            drafts_by_key = {row["key"]: row for row in draft_rows}

            async with db_pool.connection() as conn:
                await conn.execute(
                    "update record set content = content || %s where id = %s",
                    (Jsonb({"processor": "crm_profile"}), first_run_id),
                )
            wrong_processor = await client.post(
                "/promote",
                headers=headers,
                json={
                    "entity": "contact:avery-chen",
                    "source_run_id": first_run_id,
                    "artifact": "crm_profile_candidate",
                },
            )
            assert wrong_processor.status_code == 422, wrong_processor.text
            assert wrong_processor.json()["error"] == "promotion_source"

            async with db_pool.connection() as conn:
                await conn.execute(
                    "update record set content = content || %s where id = %s",
                    (Jsonb({"processor": "crm_profile_rebuild"}), first_run_id),
                )
                await conn.execute(
                    "update record set enriched_at = now() where run_id = %s",
                    (first_run_id,),
                )
            promoted = await client.post(
                "/promote",
                headers=headers,
                json={
                    "entity": "contact:avery-chen",
                    "source_run_id": first_run_id,
                    "artifact": "crm_profile_candidate",
                },
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["promoted"] == 5
            promotion_run_id = promoted.json()["promotion_run_id"]

            async with db_pool.connection() as conn:
                active_result = await conn.execute(
                    """
                    select id, key, status, content, derived_from
                    from record where workspace = %s and run_id = %s
                      and collection = 'user_profiles'
                    order by key
                    """,
                    (credential.workspace, promotion_run_id),
                )
                active_rows = await active_result.fetchall()
                draft_result = await conn.execute(
                    """
                    select id, key, status from record
                    where workspace = %s and run_id = %s
                      and collection = 'user_profiles'
                    """,
                    (credential.workspace, first_run_id),
                )
                retained_drafts = await draft_result.fetchall()
            assert len(active_rows) == 5
            assert {row["status"] for row in active_rows} == {"active"}
            for row in active_rows:
                draft = drafts_by_key[row["key"]]
                assert row["content"] == draft["content"]
                assert draft["id"] in row["derived_from"]
            assert {row["status"] for row in retained_drafts} == {"draft"}

            repeated = await client.post(
                "/promote",
                headers=headers,
                json={
                    "entity": "contact:avery-chen",
                    "source_run_id": first_run_id,
                    "artifact": "crm_profile_candidate",
                },
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["promoted"] == 0
            assert repeated.json()["skipped"] == 5

            second_run_id = await _run_rebuild(
                client,
                headers=headers,
                settings=settings,
                catalog=catalog,
                source_id=source_id,
            )
            async with db_pool.connection() as conn:
                await conn.execute(
                    "update record set enriched_at = now() where run_id = %s",
                    (second_run_id,),
                )
                profiles = catalog.resolve_collection("user_profiles")
                await conn.execute(
                    """
                    insert into record (
                      workspace, collection, collection_version, collection_hash,
                      entity, key, type, status, content, enriched_at
                    ) values (%s, %s, %s, %s, 'contact:avery-chen', 'goals',
                              'profile', 'active', %s, now())
                    """,
                    (
                        credential.workspace,
                        profiles.name,
                        profiles.version,
                        profiles.contract_hash,
                        Jsonb({"text": "A newer live goal."}),
                    ),
                )
                before_result = await conn.execute(
                    """
                    select count(*) as count from record
                    where workspace = %s and collection = '_system' and type = 'run'
                      and content->>'operation' = 'promote'
                    """,
                    (credential.workspace,),
                )
                before_promotions = await before_result.fetchone()
                heads_result = await conn.execute(
                    """
                    select distinct on (key) id, key from record
                    where workspace = %s and entity = 'contact:avery-chen'
                      and collection = 'user_profiles' and status = 'active'
                    order by key, seq desc
                    """,
                    (credential.workspace,),
                )
                heads_before = {row["key"]: row["id"] for row in await heads_result.fetchall()}
            stale = await client.post(
                "/promote",
                headers=headers,
                json={
                    "entity": "contact:avery-chen",
                    "source_run_id": second_run_id,
                    "artifact": "crm_profile_candidate",
                },
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"] == "promotion_stale"
            async with db_pool.connection() as conn:
                after_result = await conn.execute(
                    """
                    select count(*) as count from record
                    where workspace = %s and collection = '_system' and type = 'run'
                      and content->>'operation' = 'promote'
                    """,
                    (credential.workspace,),
                )
                after_promotions = await after_result.fetchone()
                heads_result = await conn.execute(
                    """
                    select distinct on (key) id, key from record
                    where workspace = %s and entity = 'contact:avery-chen'
                      and collection = 'user_profiles' and status = 'active'
                    order by key, seq desc
                    """,
                    (credential.workspace,),
                )
                heads_after = {row["key"]: row["id"] for row in await heads_result.fetchall()}
            assert before_promotions is not None
            assert after_promotions is not None
            assert after_promotions["count"] == before_promotions["count"]
            assert heads_after == heads_before
