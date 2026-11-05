"""Per-reason deadlines in the derive coalescing mailbox (spec 11.4).

The job may run at the earliest time some pending stimulus allows, so a settle
stimulus extends only its own deadline and never pushes another reason's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from memseek.auth import create_workspace
from memseek.db import DatabasePool
from memseek.derive.runner import enqueue_derivation_job
from memseek.triggers import enqueue_derive_tx

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


async def _job(pool: DatabasePool) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select payload, run_after,
                   (select coalesce(jsonb_object_agg(key, (value->>'due')::timestamptz), '{}')
                    from jsonb_each(stimuli)) as dues
            from job
            where workspace = 'ws' and kind = 'derive' and done_at is null and dead_at is null
            """
        )
        rows = await result.fetchall()
    assert len(rows) == 1
    row = rows[0]
    return {
        "payload": row["payload"],
        "run_after": row["run_after"].astimezone(UTC),
        "dues": {
            key: datetime.fromisoformat(value).astimezone(UTC) for key, value in row["dues"].items()
        },
    }


async def _enqueue(
    pool: DatabasePool,
    reason: str,
    when: datetime,
    coalesce: Literal["earliest", "extend"] = "earliest",
) -> datetime:
    async with pool.connection() as conn, conn.transaction():
        _id, _coalesced, run_after = await enqueue_derive_tx(
            conn,
            workspace="ws",
            derivation="profile",
            entity="user:mailbox",
            reason=reason,
            run_after=when,
            coalesce=coalesce,
        )
    return run_after.astimezone(UTC)


async def test_settle_stimulus_extends_only_its_own_deadline(db_pool: DatabasePool) -> None:
    await create_workspace(db_pool, "ws")
    write = "trigger:profile.plain:write"
    quiet = "trigger:profile.session:quiet"

    assert await _enqueue(db_pool, write, T0 + timedelta(seconds=10)) == T0 + timedelta(seconds=10)
    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=100), "extend") == T0 + timedelta(
        seconds=10
    )
    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=200), "extend") == T0 + timedelta(
        seconds=10
    )

    job = await _job(db_pool)
    assert job["payload"] == {write: True, quiet: True}
    assert job["dues"] == {
        write: T0 + timedelta(seconds=10),
        quiet: T0 + timedelta(seconds=200),
    }
    assert job["run_after"] == T0 + timedelta(seconds=10)


async def test_earlier_stimulus_pulls_a_settling_job_in(db_pool: DatabasePool) -> None:
    await create_workspace(db_pool, "ws")
    quiet = "trigger:profile.session:quiet"
    write = "trigger:profile.debounced:write"

    await _enqueue(db_pool, quiet, T0 + timedelta(seconds=120), "extend")
    # A debounced write is also settle-style, but its own window ends first.
    assert await _enqueue(db_pool, write, T0 + timedelta(seconds=60), "extend") == T0 + timedelta(
        seconds=60
    )
    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=300), "extend") == T0 + timedelta(
        seconds=60
    )
    assert await _enqueue(db_pool, write, T0 + timedelta(seconds=90), "extend") == T0 + timedelta(
        seconds=90
    )

    job = await _job(db_pool)
    assert job["dues"] == {
        quiet: T0 + timedelta(seconds=300),
        write: T0 + timedelta(seconds=90),
    }


async def test_manual_request_is_never_extended(db_pool: DatabasePool) -> None:
    await create_workspace(db_pool, "ws")
    quiet = "trigger:profile.session:quiet"

    await _enqueue(db_pool, quiet, T0 + timedelta(seconds=120), "extend")
    _id, coalesced, run_after = await enqueue_derivation_job(
        db_pool,
        workspace="ws",
        derivation="profile",
        entity="user:mailbox",
        run_after=T0 + timedelta(seconds=5),
    )
    assert coalesced is True
    assert run_after.astimezone(UTC) == T0 + timedelta(seconds=5)
    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=400), "extend") == T0 + timedelta(
        seconds=5
    )

    job = await _job(db_pool)
    assert job["payload"] == {quiet: True, "manual": True}
    assert job["dues"] == {
        quiet: T0 + timedelta(seconds=400),
        "manual": T0 + timedelta(seconds=5),
    }


async def test_legacy_job_without_stimuli_keeps_its_deadline(db_pool: DatabasePool) -> None:
    await create_workspace(db_pool, "ws")
    write = "trigger:profile.plain:write"
    quiet = "trigger:profile.session:quiet"
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into job (workspace, kind, derivation, entity, run_after, payload)
            values ('ws', 'derive', 'profile', 'user:mailbox', %s, %s::jsonb)
            """,
            (T0 + timedelta(seconds=30), f'{{"{write}": true}}'),
        )

    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=100), "extend") == T0 + timedelta(
        seconds=30
    )
    assert await _enqueue(db_pool, quiet, T0 + timedelta(seconds=200), "extend") == T0 + timedelta(
        seconds=30
    )

    job = await _job(db_pool)
    assert job["dues"] == {
        write: T0 + timedelta(seconds=30),
        quiet: T0 + timedelta(seconds=200),
    }
