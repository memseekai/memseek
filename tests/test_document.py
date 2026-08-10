"""M2 current-document assembly and freshness-reporting tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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


async def _document(
    client: httpx.AsyncClient, api_key: str, params: dict[str, Any]
) -> httpx.Response:
    return await client.get("/document", params=params, headers=_headers(api_key))


def _freshness(response: httpx.Response, derivation: str = "profile") -> dict[str, Any]:
    entries = [entry for entry in response.json()["freshness"] if entry["derivation"] == derivation]
    assert len(entries) == 1
    return entries[0]


async def _seed_ok_run(
    db_pool: DatabasePool,
    *,
    workspace: str,
    entity: str,
    derivation: str,
    high_seq: int,
    completed_at: str | None = None,
) -> UUID:
    run_id = uuid4()
    content = {
        "text": f"{derivation} ok",
        "operation": "derive",
        "processor": derivation,
        "status": "ok",
        "high_seq": high_seq,
        "completed_at": completed_at or datetime.now(UTC).isoformat(),
    }
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
                Jsonb(content),
            ),
        )
    return run_id


async def _seed_derive_job(
    db_pool: DatabasePool,
    *,
    workspace: str,
    entity: str,
    derivation: str = "profile",
) -> UUID:
    job_id = uuid4()
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into job (id, workspace, kind, derivation, entity)
            values (%s, %s, 'derive', %s, %s)
            """,
            (job_id, workspace, derivation, entity),
        )
    return job_id


async def _mark_ready(db_pool: DatabasePool, record_id: str) -> None:
    async with db_pool.connection() as conn:
        await conn.execute("update record set enriched_at = now() where id = %s", (record_id,))


async def test_document_selects_latest_per_collection_scoped_key(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-latest")
    async with _client(settings) as client:
        snapshot = {"entity": "maria", "collection": "prompt_snapshots", "type": "prompt"}
        await _post_records(
            client, credential.api_key, [{**snapshot, "key": "body", "text": "old body"}]
        )
        (new_body,) = await _post_records(
            client, credential.api_key, [{**snapshot, "key": "body", "text": "new body"}]
        )
        (profile_role,) = await _post_records(
            client,
            credential.api_key,
            [
                {
                    "entity": "maria",
                    "collection": "profiles",
                    "type": "fact",
                    "key": "body",
                    "text": "same key other collection",
                }
            ],
        )
        await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "unkeyed event"}],
        )
        response = await _document(client, credential.api_key, {"entity": "maria"})
        scoped = await _document(
            client, credential.api_key, {"entity": "maria", "collections": "profiles"}
        )
    assert response.status_code == 200
    body = response.json()
    beliefs = {(item["collection"], item["key"]): item for item in body["beliefs"]}
    assert set(beliefs) == {("prompt_snapshots", "body"), ("profiles", "body")}
    assert beliefs[("prompt_snapshots", "body")]["id"] == new_body
    assert beliefs[("prompt_snapshots", "body")]["text"] == "new body"
    assert beliefs[("prompt_snapshots", "body")]["ready"] is True
    assert beliefs[("profiles", "body")]["id"] == profile_role
    assert beliefs[("profiles", "body")]["ready"] is False
    assert body["retractions"] == []
    scoped_beliefs = scoped.json()["beliefs"]
    assert [item["collection"] for item in scoped_beliefs] == ["profiles"]


async def test_document_draft_lane_is_independent(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-draft")
    async with _client(settings) as client:
        base = {"entity": "maria", "collection": "prompt_snapshots", "type": "prompt"}
        (active_id,) = await _post_records(
            client, credential.api_key, [{**base, "key": "body", "text": "active"}]
        )
        (draft_id,) = await _post_records(
            client,
            credential.api_key,
            [{**base, "key": "body", "text": "draft", "status": "draft"}],
        )
        active = await _document(client, credential.api_key, {"entity": "maria"})
        draft = await _document(client, credential.api_key, {"entity": "maria", "status": "draft"})
    assert [item["id"] for item in active.json()["beliefs"]] == [active_id]
    assert [item["id"] for item in draft.json()["beliefs"]] == [draft_id]


async def test_document_moves_tombstoned_keys_to_retractions(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-tombstone")
    async with _client(settings) as client:
        base = {"entity": "maria", "collection": "prompt_snapshots", "type": "prompt"}
        (kept_id,) = await _post_records(
            client, credential.api_key, [{**base, "key": "kept", "text": "kept"}]
        )
        (retracted_id,) = await _post_records(
            client, credential.api_key, [{**base, "key": "gone", "text": "was here"}]
        )
        (tombstone_id,) = await _post_records(
            client,
            credential.api_key,
            [{**base, "key": "gone", "tombstone": True, "derived_from": [retracted_id]}],
        )
        response = await _document(client, credential.api_key, {"entity": "maria"})
    body = response.json()
    assert [item["id"] for item in body["beliefs"]] == [kept_id]
    assert len(body["retractions"]) == 1
    retraction = body["retractions"][0]
    assert retraction["collection"] == "prompt_snapshots"
    assert retraction["key"] == "gone"
    assert retraction["id"] == tombstone_id
    assert isinstance(retraction["seq"], int)


async def test_document_belief_citations_exclude_the_run_parent(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-citations")
    catalog = load_definition_catalog(settings)
    definition = catalog.resolve_collection("prompt_snapshots")
    async with _client(settings) as client:
        (cited_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "evidence"}],
        )
        run_id = await _seed_ok_run(
            db_pool,
            workspace="doc-citations",
            entity="maria",
            derivation="profile",
            high_seq=1,
        )
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                insert into record (
                  workspace, collection, collection_version, collection_hash,
                  entity, key, type, content, enriched_at, run_id, depth, derived_from
                )
                values (%s, %s, %s, %s, 'maria', 'body', 'prompt', %s, now(), %s, 1, %s)
                """,
                (
                    "doc-citations",
                    definition.name,
                    definition.version,
                    definition.definition_hash,
                    Jsonb({"text": "derived belief"}),
                    run_id,
                    [run_id, UUID(cited_id)],
                ),
            )
        response = await _document(
            client, credential.api_key, {"entity": "maria", "collections": "prompt_snapshots"}
        )
    (belief,) = response.json()["beliefs"]
    assert belief["run_id"] == str(run_id)
    assert belief["citations"] == [cited_id]


async def test_document_never_returns_a_partial_document(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-bounds")
    bounded_rows = settings.model_copy(update={"max_document_records": 1})
    bounded_bytes = settings.model_copy(update={"max_response_bytes": 300})
    async with _client(settings) as client:
        base = {"entity": "maria", "collection": "prompt_snapshots", "type": "prompt"}
        await _post_records(
            client,
            credential.api_key,
            [
                {**base, "key": "one", "text": "first"},
                {**base, "key": "two", "text": "second"},
            ],
        )
    async with _client(bounded_rows) as client:
        over_rows = await _document(client, credential.api_key, {"entity": "maria"})
        narrowed = await _document(
            client, credential.api_key, {"entity": "maria", "collections": "profiles"}
        )
    async with _client(bounded_bytes) as client:
        over_bytes = await _document(client, credential.api_key, {"entity": "maria"})
    assert over_rows.status_code == 409
    assert over_rows.json()["error"] == "document_too_large"
    assert narrowed.status_code == 200
    assert over_bytes.status_code == 409
    assert over_bytes.json()["error"] == "document_too_large"


async def test_document_freshness_starts_clean(settings: Settings, db_pool: DatabasePool) -> None:
    credential = await create_workspace(db_pool, "doc-clean")
    async with _client(settings) as client:
        response = await _document(client, credential.api_key, {"entity": "maria"})
    assert response.status_code == 200
    entry = _freshness(response)
    assert entry == {
        "derivation": "profile",
        "last_run_at": None,
        "watermark": 0,
        "dirty": False,
        "pending_unready": False,
        "job": None,
        "error_kind": None,
    }


async def test_document_freshness_tracks_dirty_and_unready_barrier(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-dirty")
    async with _client(settings) as client:
        (event_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "pending observation"}],
        )
        pending = await _document(client, credential.api_key, {"entity": "maria"})
        await _mark_ready(db_pool, event_id)
        ready = await _document(client, credential.api_key, {"entity": "maria"})
    pending_entry = _freshness(pending)
    assert pending_entry["dirty"] is True
    assert pending_entry["pending_unready"] is True
    ready_entry = _freshness(ready)
    assert ready_entry["dirty"] is True
    assert ready_entry["pending_unready"] is False


async def test_document_freshness_ignores_rows_outside_the_input_scope(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-scope")
    async with _client(settings) as client:
        await _post_records(
            client,
            credential.api_key,
            [
                # The profile input scope is main/{event,chat,observation},
                # active, unkeyed.  None of these rows may set dirty.
                {
                    "entity": "maria",
                    "collection": "prompt_snapshots",
                    "type": "prompt",
                    "key": "body",
                    "text": "keyed other collection",
                },
                {"entity": "maria", "type": "note", "text": "wrong type"},
                {"entity": "maria", "type": "event", "text": "draft", "status": "draft"},
                {"entity": "maria", "type": "event", "key": "keyed", "text": "keyed main row"},
            ],
        )
        response = await _document(client, credential.api_key, {"entity": "maria"})
    entry = _freshness(response)
    assert entry["dirty"] is False
    assert entry["pending_unready"] is False


async def test_document_freshness_reads_watermark_from_run_records(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-watermark")
    completed_at = datetime.now(UTC).isoformat()
    async with _client(settings) as client:
        (event_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "consumed"}],
        )
        await _mark_ready(db_pool, event_id)
        async with db_pool.connection() as conn:
            seq_result = await conn.execute("select seq from record where id = %s", (event_id,))
            seq_row = await seq_result.fetchone()
        assert seq_row is not None
        await _seed_ok_run(
            db_pool,
            workspace="doc-watermark",
            entity="maria",
            derivation="profile",
            high_seq=int(seq_row["seq"]),
            completed_at=completed_at,
        )
        response = await _document(client, credential.api_key, {"entity": "maria"})
    entry = _freshness(response)
    assert entry["watermark"] == int(seq_row["seq"])
    assert entry["last_run_at"] == completed_at
    assert entry["dirty"] is False
    assert entry["pending_unready"] is False


async def test_document_freshness_maps_job_states(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-jobs")
    async with _client(settings) as client:
        job_id = await _seed_derive_job(db_pool, workspace="doc-jobs", entity="maria")

        async def job_state() -> dict[str, Any]:
            response = await _document(client, credential.api_key, {"entity": "maria"})
            assert response.status_code == 200
            return _freshness(response)

        enqueued = await job_state()
        async with db_pool.connection() as conn:
            await conn.execute(
                "update job set run_after = now() + interval '1 hour' where id = %s",
                (job_id,),
            )
        queued = await job_state()
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                update job
                set locked_by = 'worker:token',
                    lease_until = now() + interval '5 minutes'
                where id = %s
                """,
                (job_id,),
            )
        running = await job_state()
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                update job
                set locked_by = null, lease_until = null,
                    dead_at = now(), last_error_kind = 'provider'
                where id = %s
                """,
                (job_id,),
            )
        await _seed_derive_job(db_pool, workspace="doc-jobs", entity="maria")
        dead = await job_state()
        await _seed_ok_run(
            db_pool,
            workspace="doc-jobs",
            entity="maria",
            derivation="profile",
            high_seq=1,
        )
        superseded = await job_state()
    assert (enqueued["job"], enqueued["error_kind"]) == ("enqueued", None)
    assert (queued["job"], queued["error_kind"]) == ("queued", None)
    assert (running["job"], running["error_kind"]) == ("running", None)
    assert (dead["job"], dead["error_kind"]) == ("dead", "provider")
    assert (superseded["job"], superseded["error_kind"]) == ("enqueued", None)


async def test_document_max_staleness_enqueues_stale_work(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "doc-swr")
    async with _client(settings) as client:
        (event_id,) = await _post_records(
            client,
            credential.api_key,
            [{"entity": "maria", "type": "event", "text": "stale trigger"}],
        )
        await _mark_ready(db_pool, event_id)
        response = await _document(
            client, credential.api_key, {"entity": "maria", "max_staleness": 0}
        )
        negative = await _document(
            client, credential.api_key, {"entity": "maria", "max_staleness": -1}
        )
    assert response.status_code == 200
    assert _freshness(response)["dirty"] is True
    async with db_pool.connection() as conn:
        result = await conn.execute("select count(*) as jobs from job")
        row = await result.fetchone()
    assert row is not None
    assert row["jobs"] == 1
    assert negative.status_code == 422
