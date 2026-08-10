"""Acceptance coverage for the bounded synchronous ``POST /answer`` surface."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import yaml

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.derive.basis import _stale_citation_input
from memseek.derive.runner import enqueue_derivation_job, process_derivation_job
from memseek.enrichment import enrich_once
from memseek.jobs import claim_job
from memseek.llm.fake import fake
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.worker import WorkerRuntime, run_worker_once


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


async def _ready_page(
    settings: Settings,
    db_pool: DatabasePool,
    *,
    workspace: str,
) -> str:
    catalog = load_definition_catalog(settings)
    inserted = await insert_public_records(
        db_pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya founded Acme.",
                    content={
                        "title": "Maya",
                        "body": "Maya founded [Acme](companies/acme).",
                        "type": "person",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="answer-ready-page",
    )
    return str(inserted.inserted[0].id)


async def test_answer_returns_visible_citations_gaps_and_saved_provenance(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    credential = await create_workspace(db_pool, "answer-save")
    page_id = await _ready_page(settings, db_pool, workspace=credential.workspace)
    fake.reset()
    fake.enqueue(
        json.dumps(
            {
                "answer": "Maya founded Acme.",
                "citations": [page_id],
                "gaps": ["No founding date is recorded."],
            }
        )
    )

    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "What company did Maya found?", "save": True},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["answer"] == "Maya founded Acme."
    assert payload["citations"] == [page_id]
    assert payload["gaps"] == ["No founding date is recorded."]
    assert payload["input_ids"] == [page_id]
    assert payload["model_usage"]["calls"] == 1
    assert payload["model_usage"]["estimated"] is True
    assert payload["saved_id"] is not None

    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select id, content, derived_from
                from record
                where workspace = %s and collection = 'syntheses'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert str(row["id"]) == payload["saved_id"]
    assert row["content"] == {
        "text": "Maya founded Acme.",
        "question": "What company did Maya found?",
        "gaps": ["No founding date is recorded."],
        "rewrite": False,
    }
    assert [str(value) for value in row["derived_from"]] == [page_id]


async def test_saved_answer_replays_when_a_direct_citation_is_superseded(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "answer-repair")
    original_page_id = await _ready_page(settings, db_pool, workspace=credential.workspace)
    fake.reset()
    fake.enqueue(
        json.dumps(
            {
                "answer": "Maya founded Acme.",
                "citations": [original_page_id],
                "gaps": [],
            }
        )
    )
    async with _client(settings) as client:
        saved = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "What company did Maya found?", "save": True},
        )
    assert saved.status_code == 200, saved.text
    saved_id = saved.json()["saved_id"]
    assert isinstance(saved_id, str)

    async def wait_until_ready(*record_ids: str) -> None:
        for _ in range(16):
            async with db_pool.connection() as conn:
                result = await conn.execute(
                    """
                    select count(*) as count
                    from record
                    where workspace = %s and id = any(%s::uuid[]) and enriched_at is not null
                    """,
                    (credential.workspace, list(record_ids)),
                )
                row = await result.fetchone()
            if row is not None and row["count"] == len(record_ids):
                return
            await enrich_once(db_pool, settings, catalog)
        raise AssertionError(f"records did not become ready: {record_ids}")

    await wait_until_ready(saved_id)

    replacement = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya founded New Acme.",
                    content={
                        "title": "Maya",
                        "body": "Maya founded New Acme.",
                        "type": "person",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    replacement_page_id = str(replacement.inserted[0].id)
    await wait_until_ready(replacement_page_id)
    async with db_pool.connection() as conn:
        stale = await _stale_citation_input(
            conn,
            workspace=credential.workspace,
            entity="answer",
            source=catalog.derivations["repair_synthesis"].driver,
        )
    assert [str(item.id) for item in stale or ()] == [saved_id]
    await enqueue_derivation_job(
        db_pool,
        workspace=credential.workspace,
        derivation="repair_synthesis",
        entity="answer",
    )
    claimed = await claim_job(
        db_pool,
        worker_id="answer-repair",
        kinds=("derive",),
        derivations=("repair_synthesis",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    while claimed is not None and claimed.entity != "answer":
        await process_derivation_job(
            db_pool,
            claimed=claimed,
            settings=settings,
            catalog=catalog,
        )
        claimed = await claim_job(
            db_pool,
            worker_id="answer-repair",
            kinds=("derive",),
            derivations=("repair_synthesis",),
            lease_s=settings.job_lease_s,
            max_attempts=settings.job_max_attempts,
        )
    assert claimed is not None
    assert claimed.entity == "answer"
    fake.reset()
    fake.enqueue(
        json.dumps(
            {
                "answer": "Maya founded New Acme.",
                "citations": [replacement_page_id],
                "gaps": [],
            }
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'syntheses'
                  and key = (
                    select key from record where workspace = %s and id = %s::uuid
                  )
                order by seq desc
                limit 1
                """,
                (credential.workspace, credential.workspace, saved_id),
            )
        ).fetchone()
    assert row is not None
    assert row["content"] == {
        "text": "Maya founded New Acme.",
        "question": "What company did Maya found?",
        "gaps": [],
        "rewrite": False,
    }
    assert replacement_page_id in [str(value) for value in row["derived_from"]]
    assert original_page_id not in [str(value) for value in row["derived_from"]]


async def test_answer_rejects_citations_not_visible_to_the_model(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    credential = await create_workspace(db_pool, "answer-citation")
    await _ready_page(settings, db_pool, workspace=credential.workspace)
    fake.reset()
    fake.enqueue(
        json.dumps(
            {
                "answer": "Unsupported answer.",
                "citations": [str(uuid4())],
                "gaps": [],
            }
        )
    )

    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "What company did Maya found?"},
        )

    assert response.status_code == 502
    assert response.json()["error"] == "answer_citation"


async def test_answer_can_opt_in_to_one_bounded_retrieval_rewrite(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    credential = await create_workspace(db_pool, "answer-rewrite")
    page_id = await _ready_page(settings, db_pool, workspace=credential.workspace)
    fake.reset()
    fake.enqueue(
        json.dumps({"query": "Maya founded Acme"}),
        json.dumps(
            {
                "answer": "Maya founded Acme.",
                "citations": [page_id],
                "gaps": [],
            }
        ),
    )

    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "Which company did Maya establish?", "rewrite": True},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["retrieval_query"] == "Maya founded Acme"
    assert payload["answer"] == "Maya founded Acme."
    assert payload["citations"] == [page_id]
    assert payload["model_usage"]["calls"] == 2
    assert len(fake.completion_calls) == 2
    assert fake.completion_calls[0].output_schema_name == "answer_query"
    assert fake.completion_calls[1].output_schema_name == "answer"


async def test_answer_validates_request_shape(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    credential = await create_workspace(db_pool, "answer-schema")
    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "   ", "unexpected": True},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "request_schema"


async def test_answer_resolves_answerable_collections_from_the_catalog(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """Answer scope is declared, not a fixed vocabulary of collection names."""

    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    assert "pages" in catalog.answerable_collections
    # gbrain saves answers into `syntheses`, which is deliberately not itself a
    # synthesis source: an answer must never become evidence for the next answer.
    assert "syntheses" not in catalog.answerable_collections
    assert "edges" not in catalog.answerable_collections

    credential = await create_workspace(db_pool, "answer-declared")
    page_id = await _ready_page(settings, db_pool, workspace=credential.workspace)
    fake.reset()
    fake.enqueue(json.dumps({"answer": "Maya founded Acme.", "citations": [page_id], "gaps": []}))

    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "What did Maya found?"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["citations"] == [page_id]
    scope = fake.completion_calls[0].prompt
    assert "pages/page" in scope


async def test_answer_is_unavailable_when_no_collection_declares_it(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
) -> None:
    """A catalog that opts nothing in cannot synthesize, and says so."""

    collections = tmp_path / "collections"
    collections.mkdir()
    assert settings.collections_dir is not None  # the reference catalog fixture sets it
    for path in sorted(settings.collections_dir.glob("*.yaml")):
        source = yaml.safe_load(path.read_text())
        for block in source["collections"]:
            block.pop("answerable", None)
        (collections / path.name).write_text(yaml.safe_dump(source, sort_keys=False))
    closed = settings.model_copy(update={"collections_dir": collections})

    credential = await create_workspace(db_pool, "answer-none")
    async with _client(closed) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "Anything at all?"},
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "answer_unavailable"
    assert "answerable: true" in payload["detail"]


async def test_answer_entities_scope_excludes_another_entitys_memory(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """A scoped answer cannot even see, let alone cite, another entity's records."""

    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "answer-scope")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="agent.alice",
                    collection="main",
                    type="observation",
                    text="Alice standardized on Fastify for new services.",
                ),
                PublicRecordInput(
                    entity="agent.bob",
                    collection="main",
                    type="observation",
                    text="Bob standardized on Express for new services.",
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="answer-scope",
    )
    alice_id, bob_id = (str(row.id) for row in inserted.inserted)

    fake.reset()
    # The model tries to cite Bob's record while the request is scoped to Alice.
    fake.enqueue(json.dumps({"answer": "Express.", "citations": [bob_id], "gaps": []}))
    async with _client(settings) as client:
        leaked = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "Which framework?", "entities": ["agent.alice"]},
        )
    assert leaked.status_code == 502
    assert leaked.json()["error"] == "answer_citation"
    assert bob_id not in fake.completion_calls[0].prompt
    assert alice_id in fake.completion_calls[0].prompt

    fake.reset()
    fake.enqueue(json.dumps({"answer": "Fastify.", "citations": [alice_id], "gaps": []}))
    async with _client(settings) as client:
        scoped = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "Which framework?", "entities": ["agent.alice"]},
        )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["citations"] == [alice_id]


async def test_answer_rejects_a_malformed_entity_scope(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "answer-scope-shape")
    async with _client(settings) as client:
        response = await client.post(
            "/answer",
            headers={"Authorization": f"Bearer {credential.api_key}"},
            json={"question": "Which framework?", "entities": ["agent.alice", "*"]},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "request_schema"
