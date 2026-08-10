"""M2 delta feed and scope-bound cursor tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog, sha256_canonical
from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _post_records(
    client: httpx.AsyncClient, api_key: str, records: list[dict[str, Any]]
) -> list[str]:
    response = await client.post("/records", headers=_headers(api_key), json={"records": records})
    assert response.status_code == 200, response.text
    inserted = sorted(response.json()["inserted"], key=lambda item: item["index"])
    return [item["id"] for item in inserted]


async def _seed_system_run(db_pool: DatabasePool, *, workspace: str, entity: str) -> None:
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, type, content, enriched_at
            )
            values (%s, '_system', %s, %s, %s, 'run', %s, now())
            """,
            (
                workspace,
                SYSTEM_COLLECTION_VERSION,
                SYSTEM_COLLECTION_HASH,
                entity,
                Jsonb(
                    {
                        "text": "profile ok",
                        "operation": "derive",
                        "processor": "profile",
                        "status": "ok",
                        "high_seq": 1,
                    }
                ),
            ),
        )


def _expected_scope_hash(
    *,
    entity: str = "*",
    collections: list[str] | None = None,
    status: str = "active",
    include_system: bool = False,
) -> str:
    return sha256_canonical(
        {
            "entity": entity,
            "collections": sorted(set(collections)) if collections is not None else None,
            "status": status,
            "include_system": include_system,
        }
    )


async def test_delta_streams_ascending_including_unready_and_tombstones(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-stream")
    async with _client(settings) as client:
        (unready_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "pending enrichment"}],
        )
        (keyed_id,) = await _post_records(
            client,
            credential.api_key,
            [
                {
                    "entity": "kai",
                    "collection": "prompt_snapshots",
                    "type": "prompt",
                    "key": "body",
                    "text": "keyed",
                }
            ],
        )
        (tombstone_id,) = await _post_records(
            client,
            credential.api_key,
            [
                {
                    "entity": "kai",
                    "collection": "prompt_snapshots",
                    "type": "prompt",
                    "key": "body",
                    "tombstone": True,
                    "derived_from": [keyed_id],
                }
            ],
        )
        await _seed_system_run(db_pool, workspace="delta-stream", entity="maria")
        response = await client.get(
            "/delta", params={"consumer": "cache"}, headers=_headers(credential.api_key)
        )
        with_system = await client.get(
            "/delta",
            params={"consumer": "auditor", "include_system": "true"},
            headers=_headers(credential.api_key),
        )
        scoped = await client.get(
            "/delta",
            params={"consumer": "maria-only", "entity": "maria"},
            headers=_headers(credential.api_key),
        )
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["records"]] == [unready_id, keyed_id, tombstone_id]
    seqs = [row["seq"] for row in body["records"]]
    assert seqs == sorted(seqs)
    assert body["next_cursor"] == seqs[-1]
    assert body["scope_hash"] == _expected_scope_hash()
    assert body["truncated"] is False
    by_id = {row["id"]: row for row in body["records"]}
    assert by_id[unready_id]["ready"] is False
    assert by_id[unready_id]["entity"] == "maria"
    assert by_id[tombstone_id]["tombstone"] is True
    assert by_id[tombstone_id]["citations"] == [keyed_id]

    assert any(row["collection"] == "_system" for row in with_system.json()["records"])
    scoped_body = scoped.json()
    assert [row["id"] for row in scoped_body["records"]] == [unready_id]
    assert scoped_body["scope_hash"] == _expected_scope_hash(entity="maria")


async def test_delta_scope_hash_is_order_insensitive_and_filter_bound(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-hash")
    async with _client(settings) as client:
        headers = _headers(credential.api_key)
        forward = await client.get(
            "/delta",
            params={"consumer": "c", "collections": "main,profiles"},
            headers=headers,
        )
        backward = await client.get(
            "/delta",
            params={"consumer": "c", "collections": "profiles,main"},
            headers=headers,
        )
        drafts = await client.get(
            "/delta", params={"consumer": "c", "status": "draft"}, headers=headers
        )
    assert forward.json()["scope_hash"] == backward.json()["scope_hash"]
    assert forward.json()["scope_hash"] == _expected_scope_hash(collections=["main", "profiles"])
    assert drafts.json()["scope_hash"] == _expected_scope_hash(status="draft")
    assert drafts.json()["scope_hash"] != forward.json()["scope_hash"]


async def test_delta_reads_from_cursor_without_advancing_it(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-cursor")
    async with _client(settings) as client:
        headers = _headers(credential.api_key)
        ids = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": f"event {index}"} for index in range(3)],
        )
        first = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
        first_body = first.json()
        first_seq = first_body["records"][0]["seq"]
        advanced = await client.post(
            "/cursor",
            headers=headers,
            json={
                "consumer": "cache",
                "entity": "*",
                "position": first_seq,
                "scope_hash": first_body["scope_hash"],
            },
        )
        second = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
        async with db_pool.connection() as conn:
            result = await conn.execute(
                "select position from cursor where workspace = %s and consumer = %s",
                ("delta-cursor", "cache"),
            )
            stored = await result.fetchone()
    assert [row["id"] for row in first_body["records"]] == ids
    assert advanced.status_code == 200
    assert advanced.json()["position"] == first_seq
    assert [row["id"] for row in second.json()["records"]] == ids[1:]
    assert stored is not None
    assert int(stored["position"]) == first_seq


async def test_delta_rejects_a_scope_change_for_a_stored_cursor(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-mismatch")
    async with _client(settings) as client:
        headers = _headers(credential.api_key)
        original = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
        saved = await client.post(
            "/cursor",
            headers=headers,
            json={
                "consumer": "cache",
                "entity": "*",
                "position": 0,
                "scope_hash": original.json()["scope_hash"],
            },
        )
        rescoped = await client.get(
            "/delta", params={"consumer": "cache", "collections": "main"}, headers=headers
        )
        same_scope = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
    assert saved.status_code == 200
    assert rescoped.status_code == 409
    assert rescoped.json()["error"] == "cursor_scope_mismatch"
    assert same_scope.status_code == 200


async def test_cursor_is_monotonic_scope_bound_and_force_resettable(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-monotonic")
    scope_hash = _expected_scope_hash()
    other_hash = _expected_scope_hash(status="draft")
    async with _client(settings) as client:
        headers = _headers(credential.api_key)

        async def post_cursor(payload: dict[str, Any]) -> httpx.Response:
            return await client.post(
                "/cursor",
                headers=headers,
                json={"consumer": "cache", "entity": "*", **payload},
            )

        created = await post_cursor({"position": 5, "scope_hash": scope_hash})
        advanced = await post_cursor({"position": 9, "scope_hash": scope_hash})
        idempotent = await post_cursor({"position": 9, "scope_hash": scope_hash})
        regressed = await post_cursor({"position": 4, "scope_hash": scope_hash})
        rescoped = await post_cursor({"position": 9, "scope_hash": other_hash})
        forced = await post_cursor({"position": 2, "scope_hash": other_hash, "force": True})
        malformed = await post_cursor({"position": 1, "scope_hash": "zz"})
        negative = await post_cursor({"position": -1, "scope_hash": scope_hash})
    assert created.status_code == 200
    assert created.json()["position"] == 5
    assert advanced.status_code == 200
    assert idempotent.status_code == 200
    assert regressed.status_code == 409
    assert regressed.json()["error"] == "cursor_regression"
    assert rescoped.status_code == 409
    assert rescoped.json()["error"] == "cursor_scope_mismatch"
    assert forced.status_code == 200
    assert forced.json() == {
        "consumer": "cache",
        "entity": "*",
        "position": 2,
        "scope_hash": other_hash,
        "updated_at": forced.json()["updated_at"],
    }
    assert malformed.status_code == 422
    assert negative.status_code == 422


async def test_byte_bounded_pages_resume_without_gaps_or_overlap(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "delta-bytes")
    bounded = settings.model_copy(update={"max_response_bytes": 1_400})
    async with _client(settings) as client:
        ids = await _post_records(
            client,
            credential.api_key,
            [
                {"entity": "maria", "type": "event", "text": f"event {index} " + "x" * 220}
                for index in range(4)
            ],
        )
    async with _client(bounded) as client:
        headers = _headers(credential.api_key)
        first = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
        first_body = first.json()
        assert first.status_code == 200
        assert first_body["truncated"] is True
        assert 0 < len(first_body["records"]) < 4
        assert first_body["next_cursor"] == first_body["records"][-1]["seq"]
        saved = await client.post(
            "/cursor",
            headers=headers,
            json={
                "consumer": "cache",
                "entity": "*",
                "position": first_body["next_cursor"],
                "scope_hash": first_body["scope_hash"],
            },
        )
        assert saved.status_code == 200
        collected = [row["id"] for row in first_body["records"]]
        while True:
            page = await client.get("/delta", params={"consumer": "cache"}, headers=headers)
            page_body = page.json()
            if not page_body["records"]:
                break
            collected.extend(row["id"] for row in page_body["records"])
            advance = await client.post(
                "/cursor",
                headers=headers,
                json={
                    "consumer": "cache",
                    "entity": "*",
                    "position": page_body["next_cursor"],
                    "scope_hash": page_body["scope_hash"],
                },
            )
            assert advance.status_code == 200
        timeline = await client.get("/timeline", params={"entity": "maria"}, headers=headers)
    assert collected == ids
    timeline_body = timeline.json()
    assert timeline_body["truncated"] is True
    assert timeline_body["next_before_seq"] == timeline_body["records"][-1]["seq"]
    assert 0 < len(timeline_body["records"]) < 4
