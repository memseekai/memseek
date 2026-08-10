"""Offline Turbopuffer adapter contract tests."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from memseek.config import Settings
from memseek.search.registry import CandidateQuery
from memseek.search.spec import SearchSource
from memseek.search.turbopuffer import TurbopufferError, TurbopufferSearchBackend, namespace_name


def test_namespace_names_are_hashed_and_layout_stable() -> None:
    shared = namespace_name("maria")
    per_collection = namespace_name("maria", collection="profiles", layout="per_collection")
    assert shared.startswith("ms_")
    assert len(shared) == 27
    assert per_collection.startswith("ms_")
    assert "maria" not in per_collection
    assert per_collection != namespace_name(
        "maria", collection="calendar_events", layout="per_collection"
    )


async def test_turbopuffer_upsert_and_candidate_query_contract() -> None:
    record_id = uuid4()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"rows": [{"id": str(record_id), "$dist": 0.2}]})
        return httpx.Response(200, json={})

    backend = TurbopufferSearchBackend(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    settings = Settings(
        turbopuffer_api_key="test-key",
        turbopuffer_base_url="https://tp.test",
    )
    source = SearchSource.model_validate(
        {
            "name": "memory",
            "mode": "text",
            "k": 5,
        }
    )
    query = CandidateQuery(source=source, query="hello")
    hits = await backend.candidates(settings, object(), "maria", query, None)
    assert hits[0].id == record_id
    await backend.upsert(
        settings,
        [
            {
                "id": str(record_id),
                "workspace": "maria",
                "collection": "profiles",
                "collection_version": 1,
                "collection_hash": "0" * 64,
                "vector": [1.0],
                "text": "hello",
            }
        ],
    )
    assert requests[0].headers["authorization"] == "Bearer test-key"
    body = requests[1].content
    assert b"upsert_rows" in body
    assert b"workspace" not in body
    await backend.aclose()


async def test_turbopuffer_per_collection_fanout_is_bounded() -> None:
    backend = TurbopufferSearchBackend(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        )
    )
    settings = Settings(
        turbopuffer_api_key="test-key",
        turbopuffer_layout="per_collection",
        max_collection_fanout=1,
    )
    source = SearchSource.model_validate(
        {
            "name": "memory",
            "mode": "recent",
            "scope": {"collections": ["profiles", "calendar_events"]},
        }
    )
    with pytest.raises(TurbopufferError, match="MAX_COLLECTION_FANOUT") as caught:
        await backend.candidates(settings, object(), "maria", CandidateQuery(source=source), None)
    assert caught.value.code == "search_fanout"
    await backend.aclose()
