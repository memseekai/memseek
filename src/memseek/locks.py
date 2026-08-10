"""Stable PostgreSQL advisory-lock keys and acquisition helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from psycopg import AsyncConnection

_LOCK_PREFIX = b"memseek.advisory-lock.v1\x00"


def advisory_lock_key(domain: str, *parts: str) -> int:
    """Map an unambiguous, domain-separated name to PostgreSQL signed int64."""

    digest = hashlib.sha256()
    digest.update(_LOCK_PREFIX)
    for component in (domain, *parts):
        value = component.encode("utf-8")
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return int.from_bytes(digest.digest()[:8], "big", signed=True)


def workspace_lock_key(workspace: str) -> int:
    """Return the common key used by shared writers and exclusive erasure."""

    return advisory_lock_key("workspace-mutation", workspace)


def entity_lock_key(workspace: str, entity: str) -> int:
    """Return the keyed-state serialization key for an entity."""

    return advisory_lock_key("entity-state", workspace, entity)


def sorted_entity_lock_keys(workspace: str, entities: Iterable[str]) -> tuple[int, ...]:
    """Deduplicate and numerically order entity keys to prevent lock inversions."""

    return tuple(sorted({entity_lock_key(workspace, entity) for entity in entities}))


async def acquire_workspace_lock(
    conn: AsyncConnection[Any], workspace: str, *, exclusive: bool = False
) -> int:
    """Acquire the transaction-scoped workspace mutation lock."""

    key = workspace_lock_key(workspace)
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await conn.execute(f"select {function}(%s)", (key,))
    return key


async def acquire_entity_locks(
    conn: AsyncConnection[Any], workspace: str, entities: Iterable[str]
) -> tuple[int, ...]:
    """Acquire transaction-scoped entity locks in canonical numeric order."""

    keys = sorted_entity_lock_keys(workspace, entities)
    for key in keys:
        await conn.execute("select pg_advisory_xact_lock(%s)", (key,))
    return keys
