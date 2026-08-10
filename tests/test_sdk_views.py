"""Public SDK coverage for generic named views."""

from __future__ import annotations

import httpx

from memseek.sdk import MemseekClient


async def test_sdk_lists_and_queries_graph_views_without_private_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"views": [{"name": "dependency_graph"}]})
        if request.url.path == "/answer":
            return httpx.Response(200, json={"answer": "Database"})
        return httpx.Response(200, json={"nodes": ["api", "database"]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://memseek.test"
    ) as transport_client:
        client = MemseekClient("http://memseek.test", "secret", client=transport_client)
        views = await client.views()
        graph = await client.query_view(
            "dependency_graph", seed="api", predicates=["depends_on"], depth=2
        )
        answer = await client.answer(
            question="What does the API depend on?",
            anchor="api",
            graph="dependency_graph",
        )

    assert views == {"views": [{"name": "dependency_graph"}]}
    assert graph == {"nodes": ["api", "database"]}
    assert answer == {"answer": "Database"}
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/views"),
        ("POST", "/views/dependency_graph/query"),
        ("POST", "/answer"),
    ]
    assert requests[1].read() == b'{"seed":"api","predicates":["depends_on"],"depth":2}'
    assert requests[1].headers["authorization"] == "Bearer secret"
    assert requests[2].read() == (
        b'{"question":"What does the API depend on?","rewrite":false,"save":false,'
        b'"anchor":"api","graph":"dependency_graph"}'
    )
