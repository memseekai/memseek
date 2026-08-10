"""Lease claiming, fencing, retry, and dead-letter integration tests."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

import pytest

from memseek.auth import create_workspace
from memseek.db import DatabasePool
from memseek.jobs import (
    claim_job,
    complete_job,
    dead_letter_job,
    heartbeat_job,
    reap_expired_final_attempts,
    release_not_ready_job,
    retry_or_dead_letter_job,
)
from memseek.models import ClaimedJob, JobKind, LeaseLost


async def _insert_job(
    pool: DatabasePool,
    *,
    kind: JobKind = "derive",
    attempts: int = 0,
) -> UUID:
    if kind == "derive":
        derivation, entity = "profile_v1", "agent-1"
    elif kind == "cron_scan":
        derivation, entity = "profile_v1", None
    else:
        derivation, entity = None, None
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            insert into job (workspace, kind, derivation, entity, attempts)
            values ('jobs', %s, %s, %s, %s)
            returning id
            """,
            (kind, derivation, entity, attempts),
        )
        row = await result.fetchone()
    assert row is not None
    return row["id"]  # type: ignore[return-value]


async def _setup(pool: DatabasePool) -> None:
    await create_workspace(pool, "jobs")


async def test_concurrent_claim_is_unique_and_token_is_per_attempt(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    job_id = await _insert_job(db_pool)

    async def claim(worker: str) -> ClaimedJob | None:
        return await claim_job(
            db_pool,
            worker_id=worker,
            kinds=("derive",),
            lease_s=30,
            max_attempts=3,
        )

    claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert re.fullmatch(r"worker-[ab]:[0-9a-f-]{36}", claimed[0].claim_token)


async def test_heartbeat_and_success_clear_lease(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    claimed = await claim_job(
        db_pool, worker_id="worker", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    renewed_until = await heartbeat_job(db_pool, claimed, 60)
    assert renewed_until > claimed.lease_until
    await complete_job(db_pool, claimed)
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select done_at, lease_until, locked_by from job where id = %s", (claimed.id,)
        )
        row = await result.fetchone()
    assert row is not None
    assert row["done_at"] is not None
    assert row["lease_until"] is None
    assert row["locked_by"] is None


async def test_expired_claim_is_reclaimed_and_stale_token_is_fenced(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    stale = await claim_job(
        db_pool, worker_id="stale", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert stale is not None
    async with db_pool.connection() as conn:
        await conn.execute(
            "update job set lease_until = now() - interval '1 second' where id = %s",
            (stale.id,),
        )
    with pytest.raises(LeaseLost):
        await complete_job(db_pool, stale)
    current = await claim_job(
        db_pool, worker_id="current", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert current is not None
    assert current.id == stale.id
    assert current.attempts == 2
    assert current.claim_token != stale.claim_token
    with pytest.raises(LeaseLost):
        await complete_job(db_pool, stale)
    await complete_job(db_pool, current)


async def test_expired_final_attempt_is_reaped_instead_of_reclaimed(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    job_id = await _insert_job(db_pool, attempts=3)
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update job
            set lease_until = now() - interval '1 second', locked_by = 'gone:token'
            where id = %s
            """,
            (job_id,),
        )
    assert await reap_expired_final_attempts(db_pool, 3) == 1
    assert (
        await claim_job(db_pool, worker_id="worker", kinds=("derive",), lease_s=30, max_attempts=3)
        is None
    )
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select dead_at, last_error_kind, locked_by from job where id = %s", (job_id,)
        )
        row = await result.fetchone()
    assert row is not None
    assert row["dead_at"] is not None
    assert row["last_error_kind"] == "lease_expired"
    assert row["locked_by"] is None


async def test_retry_backoff_then_dead_letters_at_attempt_cap(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool, attempts=2)
    claimed = await claim_job(
        db_pool, worker_id="worker", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    assert claimed.attempts == 3
    transition = await retry_or_dead_letter_job(
        db_pool,
        claimed,
        max_attempts=3,
        error_kind="provider",
        error="bounded failure",
    )
    assert transition.dead
    assert transition.dead_at is not None
    assert transition.attempts == 3


async def test_retry_schedules_bounded_backoff(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    claimed = await claim_job(
        db_pool, worker_id="worker", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    transition = await retry_or_dead_letter_job(
        db_pool, claimed, max_attempts=3, error_kind="transport", error="retry"
    )
    assert not transition.dead
    assert transition.run_after is not None
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select run_after > now() + interval '4 seconds' as delayed from job where id = %s",
            (claimed.id,),
        )
        row = await result.fetchone()
    assert row == {"delayed": True}


async def test_not_ready_release_refunds_attempt_and_clears_errors(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    claimed = await claim_job(
        db_pool, worker_id="worker", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    assert claimed.attempts == 1
    await release_not_ready_job(db_pool, claimed, retry_s=2)
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select attempts, locked_by, lease_until, last_error_kind,
                   run_after > now() as delayed
            from job where id = %s
            """,
            (claimed.id,),
        )
        row = await result.fetchone()
    assert row == {
        "attempts": 0,
        "locked_by": None,
        "lease_until": None,
        "last_error_kind": None,
        "delayed": True,
    }


async def test_expired_lease_fences_every_claimed_transition(
    db_pool: DatabasePool,
) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    claimed = await claim_job(
        db_pool, worker_id="expired", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    async with db_pool.connection() as conn:
        await conn.execute(
            "update job set lease_until = clock_timestamp() where id = %s",
            (claimed.id,),
        )
    with pytest.raises(LeaseLost):
        await heartbeat_job(db_pool, claimed, 30)
    with pytest.raises(LeaseLost):
        await retry_or_dead_letter_job(
            db_pool,
            claimed,
            max_attempts=3,
            error_kind="transport",
            error="must remain fenced",
        )
    with pytest.raises(LeaseLost):
        await dead_letter_job(
            db_pool,
            claimed,
            error_kind="config",
            error="must remain fenced",
        )
    with pytest.raises(LeaseLost):
        await release_not_ready_job(db_pool, claimed, retry_s=2)
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select locked_by, done_at, dead_at, attempts from job where id = %s",
            (claimed.id,),
        )
        row = await result.fetchone()
    assert row == {
        "locked_by": claimed.claim_token,
        "done_at": None,
        "dead_at": None,
        "attempts": 1,
    }


async def test_job_transition_bounds_are_positive(db_pool: DatabasePool) -> None:
    await _setup(db_pool)
    await _insert_job(db_pool)
    claimed = await claim_job(
        db_pool, worker_id="bounds", kinds=("derive",), lease_s=30, max_attempts=3
    )
    assert claimed is not None
    with pytest.raises(ValueError, match="lease_s"):
        await heartbeat_job(db_pool, claimed, 0)
    with pytest.raises(ValueError, match="max_attempts"):
        await retry_or_dead_letter_job(
            db_pool,
            claimed,
            max_attempts=0,
            error_kind="transport",
            error="invalid bound",
        )
    with pytest.raises(ValueError, match="retry_s"):
        await release_not_ready_job(db_pool, claimed, retry_s=0)
    with pytest.raises(ValueError, match="max_attempts"):
        await reap_expired_final_attempts(db_pool, 0)


async def test_claim_can_restrict_derive_processors(db_pool: DatabasePool) -> None:
    await _setup(db_pool)
    unrelated = await _insert_job(db_pool)
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            insert into job (workspace, kind, derivation, entity)
            values ('jobs', 'derive', 'reflection', 'agent-2')
            returning id
            """
        )
        row = await result.fetchone()
    assert row is not None
    reflection_id = row["id"]

    claimed = await claim_job(
        db_pool,
        worker_id="reflection-only",
        kinds=("derive",),
        derivations=("reflection",),
        lease_s=30,
        max_attempts=3,
    )
    assert claimed is not None
    assert claimed.id == reflection_id
    await complete_job(db_pool, claimed)
    async with db_pool.connection() as conn:
        result = await conn.execute("select attempts from job where id = %s", (unrelated,))
        row = await result.fetchone()
    assert row == {"attempts": 0}
