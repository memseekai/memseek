"""M7 projection-repair planning tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.records import RecordBatchRequest, insert_public_records
from memseek.reindex import reindex
from memseek.sdk import MemseekClient, MemseekHTTPError

_RECORD: dict[str, Any] = {
    "entity": "maria",
    "collection": "calendar_events",
    "type": "meeting",
    "content": {
        "title": "Planning",
        "starts_at": "2026-07-16T10:00:00Z",
        "ends_at": "2026-07-16T10:30:00Z",
        "attendees": ["Maria"],
        "external_id": "reindex-1",
    },
}


async def test_incremental_reindex_enqueues_ready_targets(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "reindex-test")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest.model_validate({"records": [_RECORD]}),
        catalog=catalog,
        settings=settings,
    )
    assert len(inserted.inserted) == 1
    result = await reindex(
        db_pool,
        workspace=credential.workspace,
        settings=settings,
        catalog=catalog,
        since_seq=0,
    )
    assert result.mode == "incremental"
    assert result.target_count == 1
    assert result.enqueued_jobs == 1


async def test_reindex_route_plans_the_same_rebuild_without_a_terminal(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """`POST /reindex` is the deployed form of `memseek reindex`.

    A hosted caller has no shell, so the operation that makes an external index
    adopt a newly declared field has to be reachable with the workspace key.
    """

    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "reindex-route")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest.model_validate({"records": [_RECORD]}),
        catalog=catalog,
        settings=settings,
    )
    assert len(inserted.inserted) == 1

    app = create_app(settings, catalog=catalog, pool=create_pool(settings), verify_storage=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sdk = MemseekClient("http://test", credential.api_key, client=client)
            planned = await sdk.reindex(since_seq=0)
            # Naming neither scope, or both, is a caller mistake either way.
            with pytest.raises(MemseekHTTPError) as neither:
                await sdk.reindex()
            with pytest.raises(MemseekHTTPError) as both:
                await sdk.reindex(since_seq=0, reset=True)

    assert planned["workspace"] == credential.workspace
    assert planned["mode"] == "incremental"
    assert planned["target_count"] == 1
    assert planned["enqueued_jobs"] == 1
    for refused in (neither, both):
        assert refused.value.status_code == 422
        body = refused.value.payload
        assert isinstance(body, dict)
        assert body["error"] == "reindex_request"
        assert body["detail"] == "choose exactly one of since_seq or reset"
