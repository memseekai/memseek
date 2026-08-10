"""Advisory lock key determinism and serialization tests."""

from __future__ import annotations

import asyncio

from memseek.db import DatabasePool
from memseek.locks import (
    acquire_entity_locks,
    acquire_workspace_lock,
    entity_lock_key,
    sorted_entity_lock_keys,
    workspace_lock_key,
)


def test_lock_keys_have_stable_golden_values() -> None:
    assert workspace_lock_key("acme") == 5581592336123191950
    assert entity_lock_key("acme", "agent:1") == -4643738490743001864
    assert sorted_entity_lock_keys("acme", ["z", "a", "z"]) == tuple(
        sorted({entity_lock_key("acme", "z"), entity_lock_key("acme", "a")})
    )


async def test_workspace_lock_serializes_two_transactions(
    db_pool: DatabasePool,
) -> None:
    acquired_first = asyncio.Event()
    release_first = asyncio.Event()
    acquired_second = asyncio.Event()

    async def first() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, "acme", exclusive=True)
            acquired_first.set()
            await release_first.wait()

    async def second() -> None:
        await acquired_first.wait()
        async with db_pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, "acme", exclusive=True)
            acquired_second.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await acquired_first.wait()
    await asyncio.sleep(0)
    assert not acquired_second.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert acquired_second.is_set()


async def test_entity_locks_are_acquired_in_numeric_order(
    db_pool: DatabasePool,
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        keys = await acquire_entity_locks(conn, "acme", ["b", "a", "b"])
    assert keys == tuple(sorted(keys))
    assert len(keys) == 2


async def test_shared_workspace_locks_coexist_and_block_exclusive(
    db_pool: DatabasePool,
) -> None:
    first_shared = asyncio.Event()
    second_shared = asyncio.Event()
    release_shared = asyncio.Event()
    exclusive = asyncio.Event()

    async def shared(acquired: asyncio.Event) -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, "shared", exclusive=False)
            acquired.set()
            await release_shared.wait()

    async def exclusive_waiter() -> None:
        await first_shared.wait()
        await second_shared.wait()
        async with db_pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, "shared", exclusive=True)
            exclusive.set()

    first = asyncio.create_task(shared(first_shared))
    second = asyncio.create_task(shared(second_shared))
    waiter = asyncio.create_task(exclusive_waiter())
    await asyncio.wait_for(first_shared.wait(), timeout=2)
    await asyncio.wait_for(second_shared.wait(), timeout=2)
    await asyncio.sleep(0)
    assert not exclusive.is_set()
    release_shared.set()
    await asyncio.gather(first, second, waiter)
    assert exclusive.is_set()
