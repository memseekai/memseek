"""Workspace bearer-key security tests."""

from __future__ import annotations

import base64

import pytest

import memseek.auth as auth_module
from memseek.auth import (
    ApiKeyCache,
    WorkspaceAlreadyExists,
    authenticate_api_key,
    create_workspace,
    hash_api_key,
    parse_bearer_header,
)
from memseek.db import DatabasePool


async def test_workspace_key_is_disclosed_once_and_only_hash_is_stored(
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "tenant-1")
    padding = "=" * (-len(credential.api_key) % 4)
    assert len(base64.urlsafe_b64decode(credential.api_key + padding)) >= 32
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select api_key_hash from workspace where id = %s", (credential.workspace,)
        )
        row = await result.fetchone()
    assert row == {"api_key_hash": hash_api_key(credential.api_key)}
    assert credential.api_key not in str(row)
    with pytest.raises(WorkspaceAlreadyExists):
        await create_workspace(db_pool, "tenant-1")


async def test_authentication_and_hash_only_cache(
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "cached")
    cache = ApiKeyCache(ttl_s=60, max_size=1)
    assert await authenticate_api_key(db_pool, credential.api_key, cache) == "cached"
    assert credential.api_key not in repr(cache._entries)
    assert await authenticate_api_key(db_pool, "wrong-key", cache) is None


def test_bearer_header_is_strict() -> None:
    assert parse_bearer_header("Bearer secret") == "secret"
    with pytest.raises(Exception, match="bearer"):
        parse_bearer_header("Basic secret")


def test_cache_caps_ttl_at_sixty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now[0])
    cache = ApiKeyCache(ttl_s=600, max_size=2)
    cache.put("a" * 64, "workspace-a")
    now[0] = 159.999
    assert cache.get("a" * 64) == "workspace-a"
    now[0] = 160.0
    assert cache.get("a" * 64) is None


def test_cache_evicts_least_recently_used_hash() -> None:
    cache = ApiKeyCache(ttl_s=60, max_size=2)
    cache.put("a" * 64, "workspace-a")
    cache.put("b" * 64, "workspace-b")
    assert cache.get("a" * 64) == "workspace-a"
    cache.put("c" * 64, "workspace-c")
    assert cache.get("b" * 64) is None
    assert cache.get("a" * 64) == "workspace-a"
    assert cache.get("c" * 64) == "workspace-c"
