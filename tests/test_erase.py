"""M7 canonical erasure and projection-invalidation tests."""

from __future__ import annotations

from typing import Any

import httpx

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog


async def test_erase_record_writes_audit_and_index_delete_job(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "erase-record")
    app = create_app(
        settings,
        catalog=catalog,
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
                        {
                            "entity": "maria",
                            "collection": "calendar_events",
                            "type": "meeting",
                            "content": {
                                "title": "Planning",
                                "starts_at": "2026-07-16T10:00:00Z",
                                "ends_at": "2026-07-16T10:30:00Z",
                                "attendees": ["Maria"],
                                "external_id": "erase-1",
                            },
                        }
                    ]
                },
            )
            assert inserted.status_code == 200, inserted.text
            record_id = inserted.json()["inserted"][0]["id"]

            erased = await client.post(
                "/erase",
                headers=headers,
                json={"record_ids": [record_id]},
            )
            assert erased.status_code == 200, erased.text
            payload = erased.json()
            assert payload["deleted_count"] == 1
            assert payload["affected_entity_count"] == 1
            assert payload["index_delete_job_id"]

    async with db_pool.connection() as conn:
        record = await (
            await conn.execute(
                "select id from record where workspace = %s and id = %s",
                (credential.workspace, record_id),
            )
        ).fetchone()
        assert record is None
        audit = await (
            await conn.execute(
                """
                select type, entity, content
                from record
                where workspace = %s and collection = '_system' and type = 'erasure'
                """,
                (credential.workspace,),
            )
        ).fetchone()
        assert audit is not None
        assert audit["entity"] == "_audit"
        assert audit["content"]["deleted_count"] == 1
        jobs = await (
            await conn.execute(
                """
                select kind, payload
                from job
                where workspace = %s and kind = 'index_delete'
                """,
                (credential.workspace,),
            )
        ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["records"] == [{"id": record_id, "collection": "calendar_events"}]


async def test_erase_entity_selector_removes_all_rows(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "erase-entity")
    app = create_app(
        settings,
        catalog=catalog,
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    records: list[dict[str, Any]] = [
        {
            "entity": "maria",
            "collection": "calendar_events",
            "type": "meeting",
            "content": {
                "title": f"Planning {index}",
                "starts_at": "2026-07-16T10:00:00Z",
                "ends_at": "2026-07-16T10:30:00Z",
                "attendees": ["Maria"],
                "external_id": f"erase-{index}",
            },
        }
        for index in range(2)
    ]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            inserted = await client.post("/records", headers=headers, json={"records": records})
            assert inserted.status_code == 200, inserted.text
            erased = await client.post(
                "/erase",
                headers=headers,
                json={"entity": "maria"},
            )
            assert erased.status_code == 200, erased.text
            assert erased.json()["deleted_count"] == 2
