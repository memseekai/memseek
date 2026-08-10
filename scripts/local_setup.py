"""One-shot setup for the local Docker stack: a workspace, a key, a catalog.

`docker compose up` runs this once the API is healthy. It is deliberately
idempotent — running it again on an existing stack re-publishes the catalog and
leaves the existing workspace key alone — so `docker compose up` is safe to
repeat and never silently invalidates the key you already exported.

The key is written to a bind-mounted file rather than printed, so the next step
is `export MEMSEEK_API_KEY=$(cat .memseek/api_key)` instead of copying a token
out of interleaved container logs.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from memseek.auth import WorkspaceAlreadyExists, create_workspace
from memseek.config import Settings
from memseek.db import pool_lifespan
from memseek.sdk import MemseekClient

WORKSPACE = os.environ.get("MEMSEEK_WORKSPACE", "local")
KEY_FILE = Path(os.environ.get("MEMSEEK_KEY_FILE", "/state/api_key"))
API_URL = os.environ.get("MEMSEEK_URL", "http://api:8000")
CATALOG_DIR = os.environ.get("MEMSEEK_CATALOG_DIR", "examples/agent_memory_catalog")
PACKAGE = os.environ.get("MEMSEEK_PACKAGE", "agent_memory@0.3.0")


async def _ensure_workspace(settings: Settings) -> str | None:
    """Create the workspace and return its key, or None if it already exists.

    A key is disclosed exactly once, at creation. If the workspace is already
    there we cannot re-read it, so the existing key file stays authoritative.
    """
    async with pool_lifespan(settings) as pool:
        try:
            credential = await create_workspace(pool, WORKSPACE)
        except WorkspaceAlreadyExists:
            return None
        return credential.api_key


async def _read_tool_surface(client: MemseekClient, *, expected: str | None) -> list[dict]:
    """Read back the declared MCP allowlist and check whose it is.

    This used to retry, because the first read after startup could answer from
    the shipped default catalog instead of the published one. There is no
    default catalog any more, so there is nothing to be served by mistake and
    one read is enough — but it is still checked, because an interface
    belonging to the wrong package is worth failing on rather than logging.
    """

    payload = await client._request("GET", "/tools")
    package = payload.get("package", {}).get("name")
    if expected is not None and package != expected:
        raise SystemExit(f"published {expected!r} but /tools reports {package!r}")
    return list(payload.get("tools", []))


def _persist_key(minted: str | None) -> tuple[str | None, str]:
    """Settle on the key to use and return it with the line to print.

    Synchronous and called through a thread: the lint rule that forbids blocking
    pathlib calls inside async functions is right, and this is the file I/O.
    """
    if minted is not None:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(minted, encoding="utf-8")
        # Deliberately not 0600. This file is written by a container and read by
        # you, and bind-mount ownership differs across Docker platforms — on
        # Linux a root-owned 0600 file is one your shell cannot cat. It is a
        # local development key in a gitignored directory; the trade is worth it.
        KEY_FILE.chmod(0o644)
        return minted, f"workspace {WORKSPACE!r} created; key written to {KEY_FILE}"

    if KEY_FILE.exists():
        existing = KEY_FILE.read_text(encoding="utf-8").strip()
        return existing, f"workspace {WORKSPACE!r} already exists; reusing {KEY_FILE}"

    return None, (
        f"workspace {WORKSPACE!r} exists but {KEY_FILE} is missing — its key was "
        f"disclosed once and cannot be recovered.\n"
        f"Run `docker compose down -v` to start over, or mint a second workspace "
        f"with `docker compose run --rm setup` after changing MEMSEEK_WORKSPACE."
    )


async def main() -> int:
    settings = Settings()

    minted = await _ensure_workspace(settings)
    api_key, note = await asyncio.to_thread(_persist_key, minted)
    if note:
        print(note)
    if api_key is None:
        return 1

    async with MemseekClient(API_URL, api_key) as client:
        result = await client.catalog.publish(package=PACKAGE, directory=CATALOG_DIR)
        package = result.get("package", {})
        print(
            f"published {package.get('name')}@{package.get('version')} "
            f"({len(result.get('files', []))} files) from {CATALOG_DIR}"
        )

        tools = await _read_tool_surface(client, expected=package.get("name"))
        names = ", ".join(tool["name"] for tool in tools)
        print(f"MCP interface ready — {len(tools)} tools: {names}")

    # The address you reach it on from outside Docker, which is not API_URL:
    # that one names the container. Compose passes the published port in.
    public = os.environ.get("MEMSEEK_PUBLIC_URL", "http://127.0.0.1:8000")
    print(f"\nAPI      {public}")
    print(f"MCP      {public}/mcp")
    print("Key      export MEMSEEK_API_KEY=$(cat .memseek/api_key)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
