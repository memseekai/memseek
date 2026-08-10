"""M2 dereference, timeline, and history read-view tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION
from memseek.render import TRUNCATION_SENTINEL


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


async def _seed_derived_row(
    db_pool: DatabasePool,
    settings: Settings,
    *,
    workspace: str,
    entity: str,
    collection: str,
    key: str,
    text: str,
    cited_id: UUID,
) -> tuple[UUID, UUID]:
    """Insert a run row and one keyed output citing ``cited_id`` through it."""

    catalog = load_definition_catalog(settings)
    definition = catalog.resolve_collection(collection)
    run_id = uuid4()
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              id, workspace, collection, collection_version, collection_hash,
              entity, type, content, enriched_at
            )
            values (%s, %s, '_system', %s, %s, %s, 'run', %s, now())
            """,
            (
                run_id,
                workspace,
                SYSTEM_COLLECTION_VERSION,
                SYSTEM_COLLECTION_HASH,
                entity,
                Jsonb(
                    {
                        "text": "derive ok",
                        "operation": "derive",
                        "processor": "profile",
                        "status": "ok",
                        "high_seq": 1,
                    }
                ),
            ),
        )
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, key, type, content, enriched_at, run_id, depth, derived_from
            )
            values (%s, %s, %s, %s, %s, %s, 'summary', %s, now(), %s, 1, %s)
            returning id
            """,
            (
                workspace,
                collection,
                definition.version,
                definition.definition_hash,
                entity,
                key,
                Jsonb({"text": text}),
                run_id,
                [run_id, cited_id],
            ),
        )
        row = await result.fetchone()
        assert row is not None
        return row["id"], run_id


async def test_dereference_returns_full_row_and_touches_access_time(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "reads-deref")
    async with _client(settings) as client:
        (source_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "Maria confirmed the budget."}],
        )
        derived_id, run_id = await _seed_derived_row(
            db_pool,
            settings,
            workspace="reads-deref",
            entity="maria",
            collection="prompt_snapshots",
            key="body",
            text="Maria leads platform.",
            cited_id=UUID(source_id),
        )
        response = await client.get(f"/records/{derived_id}", headers=_headers(credential.api_key))
        assert response.status_code == 200
        row = response.json()
        assert row["id"] == str(derived_id)
        assert row["collection"] == "prompt_snapshots"
        assert row["collection_version"] == 1
        assert len(row["collection_hash"]) == 64
        assert row["entity"] == "maria"
        assert row["key"] == "body"
        assert row["type"] == "summary"
        assert row["status"] == "active"
        assert row["content"]["text"] == "Maria leads platform."
        assert row["tombstone"] is False
        assert row["ready"] is True
        assert row["enriched_at"] is not None
        assert row["run_id"] == str(run_id)
        assert row["depth"] == 1
        assert row["derived_from"] == [str(run_id), source_id]
        assert row["enrichment_error"] is None
        assert "embedding" not in row
        assert row["scores"] == {}
        for field in ("annotations", "annotation_meta", "enrichment_meta"):
            assert row[field] == {}
        for field in ("occurred_at", "created_at", "last_accessed"):
            assert isinstance(row[field], str)

        async with db_pool.connection() as conn:
            before_result = await conn.execute(
                "select last_accessed from record where id = %s", (derived_id,)
            )
            before_row = await before_result.fetchone()
        assert before_row is not None
        again = await client.get(f"/records/{derived_id}", headers=_headers(credential.api_key))
        assert again.status_code == 200
        async with db_pool.connection() as conn:
            after_result = await conn.execute(
                "select last_accessed from record where id = %s", (derived_id,)
            )
            after_row = await after_result.fetchone()
        assert after_row is not None
        assert after_row["last_accessed"] > before_row["last_accessed"]


async def test_dereference_touch_can_be_disabled(settings: Settings, db_pool: DatabasePool) -> None:
    credential = await create_workspace(db_pool, "reads-no-touch")
    untouched = settings.model_copy(update={"touch_on_read": False})
    async with _client(untouched) as client:
        (record_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "hello"}],
        )
        async with db_pool.connection() as conn:
            before_result = await conn.execute(
                "select last_accessed from record where id = %s", (record_id,)
            )
            before_row = await before_result.fetchone()
        response = await client.get(f"/records/{record_id}", headers=_headers(credential.api_key))
        assert response.status_code == 200
        async with db_pool.connection() as conn:
            after_result = await conn.execute(
                "select last_accessed from record where id = %s", (record_id,)
            )
            after_row = await after_result.fetchone()
    assert before_row is not None
    assert after_row is not None
    assert after_row["last_accessed"] == before_row["last_accessed"]


async def test_dereference_rejects_foreign_missing_and_malformed_ids(
    settings: Settings, db_pool: DatabasePool
) -> None:
    owner = await create_workspace(db_pool, "reads-owner")
    outsider = await create_workspace(db_pool, "reads-outsider")
    async with _client(settings) as client:
        (record_id,) = await _post_records(
            client,
            owner.api_key,
            [{"entity": "maria", "type": "event", "text": "private"}],
        )
        unauthorized = await client.get(f"/records/{record_id}")
        foreign = await client.get(f"/records/{record_id}", headers=_headers(outsider.api_key))
        missing = await client.get(f"/records/{uuid4()}", headers=_headers(owner.api_key))
        malformed = await client.get("/records/not-a-uuid", headers=_headers(owner.api_key))
    assert unauthorized.status_code == 401
    assert foreign.status_code == 404
    assert foreign.json()["error"] == "record_not_found"
    assert missing.status_code == 404
    assert malformed.status_code == 422
    assert malformed.json()["error"] == "invalid_id"


async def test_timeline_orders_newest_first_and_filters(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "reads-timeline")
    async with _client(settings) as client:
        ids = await _post_records(
            client,
            credential.api_key,
            [
                {"entity": "maria", "type": "event", "text": "first"},
                {"entity": "maria", "type": "chat", "text": "second"},
                {
                    "entity": "maria",
                    "collection": "prompt_snapshots",
                    "type": "prompt",
                    "key": "body",
                    "text": "third",
                },
                {"entity": "maria", "type": "event", "text": "draft note", "status": "draft"},
                {"entity": "kai", "type": "event", "text": "other entity"},
            ],
        )
        headers = _headers(credential.api_key)
        default = await client.get("/timeline", params={"entity": "maria"}, headers=headers)
        typed = await client.get(
            "/timeline", params={"entity": "maria", "types": "chat"}, headers=headers
        )
        scoped = await client.get(
            "/timeline",
            params={"entity": "maria", "collections": "prompt_snapshots"},
            headers=headers,
        )
        drafts = await client.get(
            "/timeline", params={"entity": "maria", "status": "draft"}, headers=headers
        )
        everything = await client.get(
            "/timeline", params={"entity": "maria", "status": "all"}, headers=headers
        )
    assert default.status_code == 200
    body = default.json()
    assert [row["id"] for row in body["records"]] == [ids[2], ids[1], ids[0]]
    seqs = [row["seq"] for row in body["records"]]
    assert seqs == sorted(seqs, reverse=True)
    assert body["next_before_seq"] is None
    assert body["truncated"] is False
    by_id = {row["id"]: row for row in body["records"]}
    assert by_id[ids[0]]["ready"] is False
    assert by_id[ids[2]]["ready"] is True
    assert by_id[ids[2]]["key"] == "body"
    assert [row["id"] for row in typed.json()["records"]] == [ids[1]]
    assert [row["id"] for row in scoped.json()["records"]] == [ids[2]]
    assert [row["id"] for row in drafts.json()["records"]] == [ids[3]]
    assert len(everything.json()["records"]) == 4


async def test_timeline_pagination_and_system_visibility(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "reads-pages")
    async with _client(settings) as client:
        ids = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": f"event {index}"} for index in range(3)],
        )
        await _seed_derived_row(
            db_pool,
            settings,
            workspace="reads-pages",
            entity="maria",
            collection="prompt_snapshots",
            key="body",
            text="derived",
            cited_id=UUID(ids[0]),
        )
        headers = _headers(credential.api_key)
        first = await client.get(
            "/timeline", params={"entity": "maria", "limit": 2}, headers=headers
        )
        assert first.status_code == 200
        first_body = first.json()
        assert len(first_body["records"]) == 2
        assert first_body["next_before_seq"] == first_body["records"][-1]["seq"]
        second = await client.get(
            "/timeline",
            params={"entity": "maria", "limit": 2, "before_seq": first_body["next_before_seq"]},
            headers=headers,
        )
        second_body = second.json()
        collected = [row["id"] for row in first_body["records"] + second_body["records"]]
        assert len(collected) == len(set(collected)) == 4

        hidden = await client.get("/timeline", params={"entity": "maria"}, headers=headers)
        shown = await client.get(
            "/timeline", params={"entity": "maria", "include_system": "true"}, headers=headers
        )
        over_limit = await client.get(
            "/timeline", params={"entity": "maria", "limit": 101}, headers=headers
        )
        missing_entity = await client.get("/timeline", headers=headers)
    assert all(row["collection"] != "_system" for row in hidden.json()["records"])
    assert any(row["collection"] == "_system" for row in shown.json()["records"])
    assert over_limit.status_code == 422
    assert over_limit.json()["error"] == "request_schema"
    assert missing_entity.status_code == 422


async def test_timeline_text_uses_compact_truncation(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "reads-compact")
    long_text = "a" * 300 + "MIDDLE" + "b" * 300
    async with _client(settings) as client:
        await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": long_text}],
        )
        response = await client.get(
            "/timeline", params={"entity": "maria"}, headers=_headers(credential.api_key)
        )
    rendered = response.json()["records"][0]["text"]
    assert len(rendered) == 500
    assert TRUNCATION_SENTINEL in rendered
    assert rendered.startswith("a")
    assert rendered.endswith("b")
    assert "MIDDLE" not in rendered


async def test_history_returns_every_version_of_one_key(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "reads-history")
    async with _client(settings) as client:
        headers = _headers(credential.api_key)
        base = {"entity": "maria", "collection": "prompt_snapshots", "type": "prompt"}
        (v1,) = await _post_records(
            client, credential.api_key, [{**base, "key": "body", "text": "version one"}]
        )
        (v2,) = await _post_records(
            client, credential.api_key, [{**base, "key": "body", "text": "version two"}]
        )
        (draft,) = await _post_records(
            client,
            credential.api_key,
            [{**base, "key": "body", "text": "draft", "status": "draft"}],
        )
        (tombstone,) = await _post_records(
            client,
            credential.api_key,
            [{**base, "key": "body", "tombstone": True, "derived_from": [v2]}],
        )
        await _post_records(
            client, credential.api_key, [{**base, "key": "other", "text": "unrelated"}]
        )
        history = await client.get(
            "/document/history",
            params={"entity": "maria", "collection": "prompt_snapshots", "key": "body"},
            headers=headers,
        )
        paged = await client.get(
            "/document/history",
            params={
                "entity": "maria",
                "collection": "prompt_snapshots",
                "key": "body",
                "limit": 2,
            },
            headers=headers,
        )
        missing_collection = await client.get(
            "/document/history", params={"entity": "maria", "key": "body"}, headers=headers
        )
    assert history.status_code == 200
    body = history.json()
    assert [row["id"] for row in body["versions"]] == [tombstone, draft, v2, v1]
    by_id = {row["id"]: row for row in body["versions"]}
    assert by_id[tombstone]["tombstone"] is True
    assert by_id[tombstone]["citations"] == [v2]
    assert by_id[draft]["status"] == "draft"
    assert by_id[v1]["content"]["text"] == "version one"
    assert body["next_before_seq"] is None

    paged_body = paged.json()
    assert len(paged_body["versions"]) == 2
    assert paged_body["next_before_seq"] == paged_body["versions"][-1]["seq"]
    assert missing_collection.status_code == 422
