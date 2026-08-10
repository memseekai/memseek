"""Protocol-level tests for the authenticated Streamable HTTP endpoint."""

from __future__ import annotations

import httpx
import httpx2
from mcp.client.streamable_http import streamable_http_client

from mcp import Client
from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog


async def test_mcp_http_requires_a_workspace_bearer_and_valid_origin(
    settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "mcp-http-auth")
    app = create_app(
        settings.model_copy(update={"api_cors_origins": ("https://agent.example",)}),
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    protocol_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "server/discover",
    }

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.post("/mcp", headers=protocol_headers, json=request)
            invalid = await client.post(
                "/mcp",
                headers={**protocol_headers, "Authorization": "Bearer invalid"},
                json=request,
            )
            bad_origin = await client.post(
                "/mcp",
                headers={
                    **protocol_headers,
                    "Authorization": f"Bearer {credential.api_key}",
                    "Origin": "https://attacker.example",
                },
                json=request,
            )
            mismatched_method = await client.post(
                "/mcp",
                headers={
                    **protocol_headers,
                    "Authorization": f"Bearer {credential.api_key}",
                    "Mcp-Method": "tools/list",
                },
                json=request,
            )
            preflight = await client.options(
                "/mcp",
                headers={
                    "Origin": "https://agent.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "authorization,mcp-protocol-version,mcp-method,mcp-name"
                    ),
                },
            )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer ")
    assert invalid.status_code == 401
    assert bad_origin.status_code == 403
    assert mismatched_method.status_code == 400
    assert mismatched_method.json()["error"]["code"] == -32020
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://agent.example"


async def test_mcp_http_negotiates_modern_protocol_and_lists_workspace_tools(
    settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "mcp-http-modern")
    other_credential = await create_workspace(db_pool, "mcp-http-other")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )

    async with app.router.lifespan_context(app):
        api_transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=api_transport, base_url="http://test") as api:
            inserted = await api.post(
                "/records",
                headers={"Authorization": f"Bearer {credential.api_key}"},
                json={
                    "records": [
                        {
                            "entity": "maria",
                            "type": "event",
                            "text": "Remote MCP is available.",
                        }
                    ]
                },
            )
        assert inserted.status_code == 200
        record_id = inserted.json()["inserted"][0]["id"]

        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {credential.api_key}"},
        )
        async with http_client:
            transport = streamable_http_client(
                "http://test/mcp",
                http_client=http_client,
                terminate_on_close=False,
            )
            async with Client(transport) as client:
                assert client.protocol_version == "2026-07-28"
                tools = await client.list_tools()
                record = await client.call_tool("record", {"id": record_id})

        other_http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {other_credential.api_key}"},
        )
        async with other_http_client:
            other_transport = streamable_http_client(
                "http://test/mcp",
                http_client=other_http_client,
                terminate_on_close=False,
            )
            async with Client(other_transport) as other_client:
                foreign_record = await other_client.call_tool("record", {"id": record_id})

    assert [tool.name for tool in tools.tools] == [
        "answer",
        "relevant_memory",
        "upcoming_calendar",
        "daily_prompt",
        "record",
    ]
    assert all(tool.output_schema is not None for tool in tools.tools)
    assert record.is_error is False
    assert record.structured_content is not None
    assert record.structured_content["id"] == record_id
    assert record.structured_content["content"]["text"] == "Remote MCP is available."
    assert foreign_record.is_error is True
    assert foreign_record.structured_content is None


async def test_mcp_http_keeps_handshake_era_client_compatibility(
    settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "mcp-http-legacy")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )

    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {credential.api_key}"},
        )
        async with http_client:
            transport = streamable_http_client(
                "http://test/mcp",
                http_client=http_client,
                terminate_on_close=False,
            )
            async with Client(transport, mode="legacy") as client:
                assert client.protocol_version == "2025-11-25"
                tools = await client.list_tools()

    assert "record" in {tool.name for tool in tools.tools}
