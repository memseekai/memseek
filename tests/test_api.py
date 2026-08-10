"""M5 API surface and health behavior tests."""

from __future__ import annotations

from typing import Any, cast

import httpx

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog


async def test_health_success_and_routes_are_exposed(
    settings: Settings,
) -> None:
    app = create_app(
        settings,
        catalog=cast(Any, object()),
        pool=create_pool(settings),
        verify_storage=False,
    )
    assert [getattr(route, "path", None) for route in app.routes] == [
        "/health",
        "/catalog",
        "/catalog/compatibility",
        "/catalog/prune",
        "/catalog",
        "/processors/{processor_name}/run",
        "/jobs/{job_id}",
        "/jobs/{job_id}/retry",
        "/backfill",
        "/backfill",
        "/backfill/{backfill_id}",
        "/backfill/{backfill_id}/cancel",
        "/derivations/{derivation_name}/rebind",
        "/reindex",
        "/records",
        "/records/{record_id}",
        "/timeline",
        "/entities",
        "/document",
        "/document/history",
        "/delta",
        "/cursor",
        "/search",
        "/search",
        "/answer",
        "/views",
        "/views/{view_name}/query",
        "/rank/schema",
        "/runs",
        "/runs/{run_id}",
        "/context",
        "/collections",
        "/processors",
        "/triggers",
        "/tools",
        "/artifacts",
        "/artifacts/{artifact_name}/render",
        "/artifacts/{artifact_name}/snapshot",
        "/artifacts/{artifact_name}/uses",
        "/artifact-uses/{use_id}",
        "/artifact-uses/{use_id}/feedback",
        "/artifacts/{artifact_name}",
        "/promote",
        "/erase",
    ]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True}


async def test_health_returns_503_after_database_failure(
    settings: Settings,
) -> None:
    app = create_app(
        settings,
        catalog=cast(Any, object()),
        pool=create_pool(settings),
        verify_storage=False,
    )

    class BrokenPool:
        def connection(self) -> None:
            raise RuntimeError("database unavailable")

    async with app.router.lifespan_context(app):
        healthy_pool = app.state.pool
        app.state.pool = BrokenPool()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        app.state.pool = healthy_pool
    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": False}


async def test_configured_cors_allows_workspace_explorer_origin(
    settings: Settings,
) -> None:
    app = create_app(
        settings.model_copy(update={"api_cors_origins": ("http://localhost:4321",)}),
        catalog=cast(Any, object()),
        pool=create_pool(settings),
        verify_storage=False,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/catalog",
                headers={
                    "Origin": "http://localhost:4321",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4321"
    assert "Authorization" in response.headers["access-control-allow-headers"]


async def test_records_requires_authentication(settings: Settings) -> None:
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/records",
                json={"records": [{"entity": "maria", "type": "event", "text": "hello"}]},
            )
    assert response.status_code == 401
    assert response.json() == {
        "error": "unauthorized",
        "detail": "invalid bearer credential",
    }


async def test_records_insert_exact_retry_and_conflict(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "api-test")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    body = {
        "records": [
            {
                "entity": "maria",
                "type": "event",
                "text": "Maria confirmed the budget.",
                "dedupe_key": "crm:event:123",
            }
        ]
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post("/records", headers=headers, json=body)
            duplicate = await client.post("/records", headers=headers, json=body)
            conflicting_body = {
                "records": [{**body["records"][0], "text": "A different immutable event."}]
            }
            conflict = await client.post("/records", headers=headers, json=conflicting_body)

    assert inserted.status_code == 200
    inserted_payload = inserted.json()
    assert inserted_payload["inserted"][0]["index"] == 0
    assert inserted_payload["inserted"][0]["ready"] is False
    assert inserted_payload["duplicates"] == []
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "inserted": [],
        "duplicates": [inserted_payload["inserted"][0]],
    }
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "dedupe_conflict"


async def test_entities_lists_real_workspace_entities(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "entities-api")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post(
                "/records",
                headers=headers,
                json={
                    "records": [
                        {"entity": "maria", "type": "event", "text": "Maria updated the plan."},
                        {"entity": "kai", "type": "event", "text": "Kai reviewed the plan."},
                    ]
                },
            )
            listed = await client.get("/entities?q=mar", headers=headers)

    assert inserted.status_code == 200
    assert listed.status_code == 200
    entities = listed.json()["entities"]
    assert len(entities) == 1
    assert entities[0]["entity"] == "maria"
    assert entities[0]["record_count"] == 1
    assert entities[0]["last_seq"] > 0
    assert "T" in entities[0]["last_seen"]


async def test_records_apply_collection_text_projection(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "calendar-api")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    body = {
        "records": [
            {
                "entity": "maria",
                "collection": "calendar_events",
                "type": "meeting",
                "content": {
                    "title": "Planning",
                    "starts_at": "2026-07-16T10:00:00Z",
                    "ends_at": "2026-07-16T10:30:00Z",
                    "attendees": ["Maria", "Kai"],
                },
            }
        ]
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/records",
                headers={"Authorization": f"Bearer {credential.api_key}"},
                json=body,
            )
    assert response.status_code == 200
    assert response.json()["inserted"][0]["ready"] is True
    record_id = response.json()["inserted"][0]["id"]
    async with db_pool.connection() as conn:
        result = await conn.execute("select content from record where id = %s", (record_id,))
        row = await result.fetchone()
    assert row is not None
    assert row["content"]["text"] == (
        "Planning starts 2026-07-16T10:00:00Z and ends 2026-07-16T10:30:00Z; "
        'attendees: ["Maria","Kai"]'
    )
