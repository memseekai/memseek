"""One realistic end-to-end flow through the HTTP and worker boundaries."""

from __future__ import annotations

import httpx

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.worker import WorkerRuntime, run_worker_once, worker_lifespan


async def test_end_to_end_ingest_enrich_freshness_search_and_job_status(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """Exercise ingest, enrichment, freshness, search, and job status boundaries."""

    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "e2e-flow")
    app_pool = create_pool(settings)
    app = create_app(settings, catalog=catalog, pool=app_pool)
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    records = [
        {
            "entity": "maria",
            "type": "event",
            "text": f"Maria confirmed budget item {index}. [importance=10]",
            "dedupe_key": f"e2e:event:{index}",
        }
        for index in range(9)
    ]

    fake.reset()
    fake.enqueue(Completion(text="[10,10,10,10,10,10,10,10,10]"))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post(
                "/records",
                headers=headers,
                json={"records": records},
            )
            assert inserted.status_code == 200
            inserted_rows = inserted.json()["inserted"]
            assert len(inserted_rows) == len(records)
            assert all(item["ready"] is False for item in inserted_rows)
            worker_pool = create_pool(settings)
            async with worker_lifespan(
                settings,
                catalog=catalog,
                pool=worker_pool,
            ) as runtime:
                first_pass = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-worker",
                )
                assert first_pass.enrichment_ready == len(records)
                assert first_pass.derivation_jobs == 0

            document = await client.get(
                "/document",
                headers=headers,
                params={"entity": "maria"},
            )
            assert document.status_code == 200
            document_payload = document.json()
            assert document_payload["entity"] == "maria"
            profile_freshness = next(
                item for item in document_payload["freshness"] if item["derivation"] == "profile"
            )
            assert profile_freshness["dirty"] is True
            assert profile_freshness["pending_unready"] is False
            assert profile_freshness["job"] == "enqueued"
            assert profile_freshness["last_run_at"] is None

            search = await client.post(
                "/search",
                headers=headers,
                json={
                    "q": "confirmed budget",
                    "mode": "hybrid",
                    "scope": {"entities": ["maria"], "collections": ["main"]},
                    "k": 5,
                    "include": ["text", "collection", "entity"],
                },
            )
            assert search.status_code == 200
            assert search.json()["hits"]
            assert any("budget" in hit.get("text", "").lower() for hit in search.json()["hits"])

            async with db_pool.connection() as conn:
                result = await conn.execute(
                    """
                    select id
                    from job
                    where workspace = %s and kind = 'derive' and derivation = 'profile'
                    order by created_at desc
                    limit 1
                    """,
                    (credential.workspace,),
                )
                job_row = await result.fetchone()
            assert job_row is not None

            job = await client.get(f"/jobs/{job_row['id']}", headers=headers)
            assert job.status_code == 200
            job_payload = job.json()
            assert job_payload["state"] == "enqueued"
            assert job_payload["successful_run_id"] is None
            assert job_payload["attempt_run_ids"] == []
            assert "trigger:profile.default:read" in job_payload["reasons"]


async def test_end_to_end_fake_user_profile_and_live_prompt(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """Run a real fake-provider profile update through the M4/M6 read surfaces."""

    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "e2e-profile")
    app_pool = create_pool(settings)
    app = create_app(settings, catalog=catalog, pool=app_pool)
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    records = [
        {
            "entity": "maria",
            "type": "event",
            "text": f"Maria confirmed profile fact {index}. [importance=10]",
            "dedupe_key": f"e2e:profile:{index}",
        }
        for index in range(9)
    ]

    fake.reset()
    fake.enqueue(Completion(text="[10,10,10,10,10,10,10,10,10]"))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post(
                "/records",
                headers=headers,
                json={"records": records},
            )
            assert inserted.status_code == 200
            inserted_rows = inserted.json()["inserted"]
            assert len(inserted_rows) == len(records)
            first_record_id = inserted_rows[0]["id"]

            worker_pool = create_pool(settings)
            async with worker_lifespan(
                settings,
                catalog=catalog,
                pool=worker_pool,
            ) as runtime:
                enriched = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-profile-enricher",
                )
                assert enriched.enrichment_ready == len(records)

                fake.enqueue(
                    Completion(
                        text=(
                            '{"records":[{"key":"role",'
                            f'"text":"Leads the platform team.",'
                            f'"citations":["{first_record_id}"]}}]}}'
                        )
                    )
                )
                queued = await client.post(
                    "/processors/profile/run",
                    headers=headers,
                    json={"entity": "maria"},
                )
                assert queued.status_code == 200
                job_id = queued.json()["job_id"]

                derived = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-profile-deriver",
                )
                assert derived.not_ready_jobs == 0

                materialized = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-profile-materializer",
                )
                assert materialized.enrichment_ready >= 1

            document = await client.get(
                "/document",
                headers=headers,
                params={"entity": "maria"},
            )
            assert document.status_code == 200
            document_payload = document.json()
            assert any(
                belief["collection"] == "profiles"
                and belief["key"] == "role"
                and belief["text"] == "Leads the platform team."
                for belief in document_payload["beliefs"]
            )
            freshness = next(
                item for item in document_payload["freshness"] if item["derivation"] == "profile"
            )
            assert freshness["dirty"] is False
            assert freshness["last_run_at"] is not None

            search = await client.post(
                "/search",
                headers=headers,
                json={
                    "q": "profile fact",
                    "mode": "hybrid",
                    "scope": {"entities": ["maria"], "collections": ["main"]},
                    "k": 5,
                    "include": ["text", "collection", "entity"],
                },
            )
            assert search.status_code == 200
            assert search.json()["hits"]

            status = await client.get(f"/jobs/{job_id}", headers=headers)
            assert status.status_code == 200
            status_payload = status.json()
            assert status_payload["state"] == "done"
            assert status_payload["successful_run_id"] is not None

            runs = await client.get(
                "/runs",
                headers=headers,
                params={"entity": "maria", "processor": "profile", "operation": "derive"},
            )
            assert runs.status_code == 200
            run_rows = runs.json()["runs"]
            assert run_rows
            run_id = run_rows[0]["id"]
            run = await client.get(f"/runs/{run_id}", headers=headers)
            assert run.status_code == 200
            run_content = run.json()["run"]["content"]
            assert run_content["status"] == "ok"
            assert run_content["source_kind"] == "changes"
            assert run_content["candidate_set"]["effect"] == "patch"
            assert run_content["candidate_set"]["coverage"] == "partial"
            assert run_content["basis"]["expected_heads"]
            assert len(run_content["task_trace"]) == 1
            assert run_content["task_trace"][0]["task"] == "result"
            assert run_content["task_trace"][0]["use"] == "llm"
            assert first_record_id in run_content["task_trace"][0]["citation_ids"]

            artifact = await client.post(
                "/artifacts/daily_agent_prompt/render",
                headers=headers,
                json={
                    "entity": "maria",
                    "task": "prepare the next update",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                },
            )
            assert artifact.status_code == 200
            artifact_payload = artifact.json()
            assert "Leads the platform team." in artifact_payload["rendered"]
            assert artifact_payload["manifest"]["input_record_ids"]
            assert artifact_payload["manifest"]["rendered_sha256"]


async def test_end_to_end_importance_threshold_recomputes_profile(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """A ready importance batch automatically schedules and recomputes profile state."""

    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "e2e-threshold")
    app_pool = create_pool(settings)
    app = create_app(settings, catalog=catalog, pool=app_pool)
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    records = [
        {
            "entity": "maria",
            "type": "event",
            "text": f"Maria made durable profile decision {index}. [importance=10]",
            "dedupe_key": f"e2e:threshold:{index}",
        }
        for index in range(10)
    ]

    fake.reset()
    fake.enqueue(
        Completion(text="[10,10,10,10,10,10,10,10,10,10]"),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post("/records", headers=headers, json={"records": records})
            assert inserted.status_code == 200
            first_record_id = inserted.json()["inserted"][0]["id"]
            fake.enqueue(
                Completion(
                    text=(
                        '{"records":[{"key":"role",'
                        '"text":"Leads the platform team.","citations":["'
                        + first_record_id
                        + '"]}]}'
                    )
                )
            )
            worker_pool = create_pool(settings)
            async with worker_lifespan(settings, catalog=catalog, pool=worker_pool) as runtime:
                enriched_and_derived = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-threshold-worker",
                )
                assert enriched_and_derived.enrichment_ready == len(records)

                async with db_pool.connection() as conn:
                    result = await conn.execute(
                        """
                        select id
                        from job
                        where workspace = %s and kind = 'derive' and derivation = 'profile'
                        order by created_at desc
                        limit 1
                        """,
                        (credential.workspace,),
                    )
                    job_row = await result.fetchone()
                assert job_row is not None
                job_id = job_row["id"]

                materialized = await run_worker_once(
                    WorkerRuntime(settings=settings, catalog=catalog, pool=runtime.pool),
                    worker_id="e2e-threshold-materializer",
                )
                assert materialized.enrichment_ready >= 1

            job = await client.get(f"/jobs/{job_id}", headers=headers)
            assert job.status_code == 200
            assert job.json()["state"] == "done"
            assert "trigger:profile.default:threshold" in job.json()["reasons"]
            document = await client.get(
                "/document",
                headers=headers,
                params={"entity": "maria"},
            )
            assert document.status_code == 200
            assert any(
                belief["collection"] == "profiles" and belief["key"] == "role"
                for belief in document.json()["beliefs"]
            )
