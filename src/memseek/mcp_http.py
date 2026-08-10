"""Authenticated Streamable HTTP transport for the package-declared MCP surface.

The HTTP endpoint is intentionally hosted by the existing FastAPI process. A
workspace API key is the MCP bearer token, so remote clients have the same
workspace isolation and catalog authority as every other Memseek API caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from memseek.auth import authenticate_api_key
from memseek.mcp_server import make_http_mcp_server

MCP_HTTP_PATH = "/mcp"
_MCP_SCOPE = "memseek:mcp"


class WorkspaceTokenVerifier:
    """Resolve an existing Memseek API key into an MCP authorization principal."""

    def __init__(self, application: Any) -> None:
        self._application = application

    async def verify_token(self, token: str) -> AccessToken | None:
        workspace = await authenticate_api_key(
            self._application.state.pool,
            token,
            self._application.state.api_key_cache,
        )
        if workspace is None:
            return None
        return AccessToken(
            token=token,
            client_id="memseek-api-key",
            scopes=[_MCP_SCOPE],
            subject=workspace,
            claims={"iss": "memseek"},
        )


class ValidateMcpOriginMiddleware:
    """Apply the Streamable HTTP Origin requirement before authentication."""

    def __init__(self, app: ASGIApp, allowed_origins: Sequence[str]) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            origin_header = next(
                (value for name, value in scope["headers"] if name.lower() == b"origin"),
                None,
            )
            if origin_header is not None:
                try:
                    origin = origin_header.decode("ascii")
                except UnicodeDecodeError:
                    origin = ""
                if origin not in self._allowed_origins:
                    await Response("Invalid Origin header", status_code=403)(scope, receive, send)
                    return
        await self._app(scope, receive, send)


class McpEndpointMiddleware:
    """Route the exact MCP endpoint to its ASGI transport before FastAPI routing."""

    def __init__(self, app: ASGIApp, *, endpoint: ASGIApp) -> None:
        self._app = app
        self._endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in {MCP_HTTP_PATH, f"{MCP_HTTP_PATH}/"}:
            await self._endpoint(scope, receive, send)
            return
        await self._app(scope, receive, send)


class McpHttpRuntime:
    """Own the dynamic MCP server, internal API client, and HTTP transport lifespan."""

    def __init__(self, application: Any, *, allowed_origins: Sequence[str]) -> None:
        self._internal_client = httpx.AsyncClient(
            base_url="http://memseek.internal",
            transport=httpx.ASGITransport(app=application),
            timeout=httpx.Timeout(180.0),
        )
        server = make_http_mcp_server(self._internal_client)
        self._manager = StreamableHTTPSessionManager(
            app=server,
            # Current MCP HTTP is explicitly sessionless. The SDK still
            # performs its legacy compatibility routing for older clients.
            stateless=True,
        )

        endpoint: ASGIApp = RequireAuthMiddleware(
            self._manager.asgi_app,
            required_scopes=[_MCP_SCOPE],
        )
        endpoint = AuthContextMiddleware(endpoint)
        endpoint = AuthenticationMiddleware(
            endpoint,
            backend=BearerAuthBackend(WorkspaceTokenVerifier(application)),
        )
        if allowed_origins:
            endpoint = CORSMiddleware(
                endpoint,
                allow_origins=list(allowed_origins),
                allow_credentials=False,
                allow_methods=["POST"],
                # MCP-Param-* header names are definition-driven, so an exact
                # static list cannot represent every valid interface.
                allow_headers=["*"],
            )
        self.endpoint = ValidateMcpOriginMiddleware(endpoint, allowed_origins)

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self._internal_client, self._manager.run():
            yield


__all__ = [
    "MCP_HTTP_PATH",
    "McpEndpointMiddleware",
    "McpHttpRuntime",
    "ValidateMcpOriginMiddleware",
    "WorkspaceTokenVerifier",
]
