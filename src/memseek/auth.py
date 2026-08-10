"""One-time API-key creation and hash-only workspace authentication."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from psycopg import errors
from psycopg_pool import AsyncConnectionPool

from memseek.models import WorkspaceCredential

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DUMMY_HASH = "0" * 64


class AuthenticationError(RuntimeError):
    """Raised when a bearer header is absent or invalid."""


class WorkspaceAlreadyExists(RuntimeError):
    """Raised when creation would rotate an existing workspace credential."""


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    workspace: str
    expires_at: float


class ApiKeyCache:
    """Bounded TTL cache containing only hashes and workspace identifiers."""

    def __init__(self, *, ttl_s: int = 60, max_size: int = 1_024) -> None:
        if ttl_s <= 0 or max_size <= 0:
            raise ValueError("API-key cache bounds must be positive")
        self._ttl_s = min(ttl_s, 60)
        self._max_size = max_size
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    def get(self, key_hash: str) -> str | None:
        entry = self._entries.get(key_hash)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key_hash]
            return None
        self._entries.move_to_end(key_hash)
        return entry.workspace

    def put(self, key_hash: str, workspace: str) -> None:
        self._entries[key_hash] = _CacheEntry(
            workspace=workspace, expires_at=time.monotonic() + self._ttl_s
        )
        self._entries.move_to_end(key_hash)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


def hash_api_key(api_key: str) -> str:
    """Return the canonical lowercase SHA-256 representation."""

    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def parse_bearer_header(authorization: str | None) -> str:
    """Extract a non-empty bearer credential without accepting extra fields."""

    if authorization is None:
        raise AuthenticationError("missing bearer credential")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthenticationError("invalid bearer credential")
    return parts[1]


async def create_workspace(pool: AsyncConnectionPool[Any], workspace: str) -> WorkspaceCredential:
    """Create a workspace and disclose its cryptographically random key once."""

    if not _WORKSPACE_RE.fullmatch(workspace):
        raise ValueError("workspace must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    api_key = secrets.token_urlsafe(32)
    key_hash = hash_api_key(api_key)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "insert into workspace (id, api_key_hash) values (%s, %s)",
                (workspace, key_hash),
            )
    except errors.UniqueViolation as exc:
        raise WorkspaceAlreadyExists(f"workspace already exists: {workspace}") from exc
    return WorkspaceCredential(workspace=workspace, api_key=api_key)


async def authenticate_api_key(
    pool: AsyncConnectionPool[Any], api_key: str, cache: ApiKeyCache | None = None
) -> str | None:
    """Resolve a bearer key using hash-only lookup and constant-time verification."""

    supplied_hash = hash_api_key(api_key)
    if cache is not None:
        cached = cache.get(supplied_hash)
        if cached is not None:
            return cached
    async with pool.connection() as conn:
        result = await conn.execute(
            "select id, api_key_hash from workspace where api_key_hash = %s",
            (supplied_hash,),
        )
        row = await result.fetchone()
    stored_hash = row["api_key_hash"] if row is not None else _DUMMY_HASH
    if not hmac.compare_digest(supplied_hash, stored_hash) or row is None:
        return None
    workspace = str(row["id"])
    if cache is not None:
        cache.put(supplied_hash, workspace)
    return workspace
