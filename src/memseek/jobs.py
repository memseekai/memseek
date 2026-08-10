"""Raw SQL job claiming and claim-token-fenced lease transitions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection, errors
from psycopg_pool import AsyncConnectionPool

from memseek.logging import log_event
from memseek.models import ClaimedJob, JobKind, JobTransition, LeaseLost

LOGGER = logging.getLogger(__name__)


class JobNotFound(LookupError):
    """Raised when a job is not owned by the requested workspace."""


class JobRetryConflict(RuntimeError):
    """Raised when retrying would compete with a newer active derive job."""


def _claimed_job(row: dict[str, Any]) -> ClaimedJob:
    lease_until = row["lease_until"]
    token = row["locked_by"]
    if not isinstance(lease_until, datetime) or not isinstance(token, str):
        raise RuntimeError("claimed job did not contain a lease and token")
    return ClaimedJob(
        id=cast(UUID, row["id"]),
        workspace=str(row["workspace"]),
        kind=cast(JobKind, row["kind"]),
        derivation=cast(str | None, row["derivation"]),
        entity=cast(str | None, row["entity"]),
        payload=cast(dict[str, Any], row["payload"]),
        dedupe_key=cast(str | None, row["dedupe_key"]),
        run_after=cast(datetime, row["run_after"]),
        attempts=int(row["attempts"]),
        lease_until=lease_until,
        claim_token=token,
        created_at=cast(datetime, row["created_at"]),
    )


async def _reap_expired_final_attempts_tx(conn: AsyncConnection[Any], max_attempts: int) -> int:
    result = await conn.execute(
        """
        update job
        set dead_at = clock_timestamp(),
            lease_until = null,
            locked_by = null,
            last_error_kind = 'lease_expired',
            last_error = 'lease expired after final attempt'
        where done_at is null
          and dead_at is null
          and attempts >= %s
          and lease_until is not null
          and lease_until < clock_timestamp()
        """,
        (max_attempts,),
    )
    return result.rowcount


async def reap_expired_final_attempts(pool: AsyncConnectionPool[Any], max_attempts: int) -> int:
    """Dead-letter final attempts whose owner let the lease expire."""

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    async with pool.connection() as conn, conn.transaction():
        count = await _reap_expired_final_attempts_tx(conn, max_attempts)
    if count:
        log_event(LOGGER, "warning", "jobs.lease_expired_reaped", count=count)
    return count


async def claim_job(
    pool: AsyncConnectionPool[Any],
    *,
    worker_id: str,
    kinds: tuple[JobKind, ...],
    derivations: tuple[str, ...] | None = None,
    lease_s: int,
    max_attempts: int,
) -> ClaimedJob | None:
    """Claim the oldest runnable job among dispatch lanes with capacity.

    ``derivations`` optionally restricts derive-kind jobs to those names."""

    if not worker_id or ":" in worker_id:
        raise ValueError("worker_id must be non-empty and cannot contain ':'")
    if not kinds:
        return None
    if lease_s <= 0 or max_attempts <= 0:
        raise ValueError("lease and attempt bounds must be positive")
    claim_token = f"{worker_id}:{uuid4()}"
    derive_filter = list(derivations) if derivations is not None else None
    async with pool.connection() as conn, conn.transaction():
        reaped = await _reap_expired_final_attempts_tx(conn, max_attempts)
        result = await conn.execute(
            """
            with candidate as (
              select id
              from job
              where done_at is null
                and dead_at is null
                and kind = any(%s::text[])
                and (
                  %s::text[] is null
                  or kind <> 'derive'
                  or derivation = any(%s::text[])
                )
                and run_after <= clock_timestamp()
                and attempts < %s
                and (lease_until is null or lease_until < clock_timestamp())
              order by run_after, created_at, id
              for update skip locked
              limit 1
            )
            update job j
            set attempts = j.attempts + 1,
                lease_until = clock_timestamp() + make_interval(secs => %s),
                locked_by = %s
            from candidate c
            where j.id = c.id
            returning j.*
            """,
            (
                list(kinds),
                derive_filter,
                derive_filter,
                max_attempts,
                lease_s,
                claim_token,
            ),
        )
        row = await result.fetchone()
    if reaped:
        log_event(LOGGER, "warning", "jobs.lease_expired_reaped", count=reaped)
    if row is None:
        return None
    claimed = _claimed_job(row)
    log_event(
        LOGGER,
        "info",
        "job.claimed",
        job_id=str(claimed.id),
        workspace=claimed.workspace,
        kind=claimed.kind,
        attempts=claimed.attempts,
    )
    return claimed


async def heartbeat_job(
    pool: AsyncConnectionPool[Any], claimed: ClaimedJob, lease_s: int
) -> datetime:
    """Renew a lease only while the exact token still owns an unexpired job."""

    if lease_s <= 0:
        raise ValueError("lease_s must be positive")
    result_row: dict[str, Any] | None
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            update job
            set lease_until = clock_timestamp() + make_interval(secs => %s)
            where id = %s
              and locked_by = %s
              and done_at is null
              and dead_at is null
              and lease_until > clock_timestamp()
            returning lease_until
            """,
            (lease_s, claimed.id, claimed.claim_token),
        )
        result_row = await result.fetchone()
    if result_row is None:
        log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
        raise LeaseLost(f"job lease lost: {claimed.id}")
    return cast(datetime, result_row["lease_until"])


async def complete_job(pool: AsyncConnectionPool[Any], claimed: ClaimedJob) -> datetime:
    """Mark a job successful only under its live claim token."""

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            update job
            set done_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = null, last_error = null
            where id = %s
              and locked_by = %s
              and done_at is null
              and dead_at is null
              and lease_until > clock_timestamp()
            returning done_at
            """,
            (claimed.id, claimed.claim_token),
        )
        row = await result.fetchone()
    if row is None:
        log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
        raise LeaseLost(f"job lease lost: {claimed.id}")
    log_event(LOGGER, "info", "job.completed", job_id=str(claimed.id), kind=claimed.kind)
    return cast(datetime, row["done_at"])


async def retry_or_dead_letter_job(
    pool: AsyncConnectionPool[Any],
    claimed: ClaimedJob,
    *,
    max_attempts: int,
    error_kind: str,
    error: str,
) -> JobTransition:
    """Retry with bounded exponential backoff or dead-letter at the attempt cap."""

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            update job
            set dead_at = case when attempts >= %s then clock_timestamp() else null end,
                run_after = case
                  when attempts >= %s then run_after
                  else clock_timestamp() + make_interval(
                    secs => least(300, (5 * power(4::numeric, attempts - 1))::int)
                  )
                end,
                lease_until = null,
                locked_by = null,
                last_error_kind = %s,
                last_error = %s
            where id = %s
              and locked_by = %s
              and done_at is null
              and dead_at is null
              and lease_until > clock_timestamp()
            returning attempts, run_after, dead_at
            """,
            (
                max_attempts,
                max_attempts,
                error_kind,
                error,
                claimed.id,
                claimed.claim_token,
            ),
        )
        row = await result.fetchone()
    if row is None:
        log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
        raise LeaseLost(f"job lease lost: {claimed.id}")
    transition = JobTransition(
        dead=row["dead_at"] is not None,
        attempts=int(row["attempts"]),
        run_after=cast(datetime | None, row["run_after"]),
        dead_at=cast(datetime | None, row["dead_at"]),
    )
    event = "job.dead_lettered" if transition.dead else "job.retry_scheduled"
    level = "error" if transition.dead else "warning"
    log_event(
        LOGGER,
        level,
        event,
        job_id=str(claimed.id),
        kind=claimed.kind,
        attempts=transition.attempts,
        error_kind=error_kind,
    )
    return transition


async def dead_letter_job(
    pool: AsyncConnectionPool[Any],
    claimed: ClaimedJob,
    *,
    error_kind: str,
    error: str,
) -> datetime:
    """Mark a non-retryable claimed job dead under its live lease."""

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            update job
            set dead_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = %s, last_error = %s
            where id = %s
              and locked_by = %s
              and done_at is null
              and dead_at is null
              and lease_until > clock_timestamp()
            returning dead_at
            """,
            (error_kind, error, claimed.id, claimed.claim_token),
        )
        row = await result.fetchone()
    if row is None:
        log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
        raise LeaseLost(f"job lease lost: {claimed.id}")
    log_event(
        LOGGER,
        "error",
        "job.dead_lettered",
        job_id=str(claimed.id),
        kind=claimed.kind,
        attempts=claimed.attempts,
        error_kind=error_kind,
    )
    return cast(datetime, row["dead_at"])


async def release_not_ready_job(
    pool: AsyncConnectionPool[Any], claimed: ClaimedJob, retry_s: int
) -> datetime:
    """Release an enrichment barrier without charging an execution attempt."""

    if retry_s <= 0:
        raise ValueError("retry_s must be positive")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            update job
            set run_after = clock_timestamp() + make_interval(secs => %s),
                attempts = greatest(attempts - 1, 0),
                lease_until = null,
                locked_by = null,
                last_error_kind = null,
                last_error = null
            where id = %s
              and locked_by = %s
              and done_at is null
              and dead_at is null
              and lease_until > clock_timestamp()
            returning run_after
            """,
            (retry_s, claimed.id, claimed.claim_token),
        )
        row = await result.fetchone()
    if row is None:
        log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
        raise LeaseLost(f"job lease lost: {claimed.id}")
    log_event(LOGGER, "info", "job.not_ready_released", job_id=str(claimed.id))
    return cast(datetime, row["run_after"])


def _json_time(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def get_job_status(
    pool: AsyncConnectionPool[Any], *, workspace: str, job_id: UUID
) -> dict[str, Any]:
    """Return a workspace-scoped, content-free job status projection."""

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select id, kind, derivation, entity, payload, dedupe_key, run_after,
                   attempts, lease_until, done_at, dead_at, last_error_kind,
                   clock_timestamp() as observed_at
            from job
            where workspace = %s and id = %s
            """,
            (workspace, job_id),
        )
        row = await result.fetchone()
        if row is None:
            raise JobNotFound(f"job does not exist: {job_id}")
        runs_result = await conn.execute(
            """
            select id, content->>'status' as status
            from record
            where workspace = %s and type = 'run' and content->>'job_id' = %s
            order by seq
            """,
            (workspace, str(job_id)),
        )
        runs = await runs_result.fetchall()
    attempt_runs = [str(item["id"]) for item in runs]
    successful = next(
        (str(item["id"]) for item in reversed(runs) if item["status"] in {"ok", "noop"}),
        None,
    )
    if row["dead_at"] is not None:
        state = "dead"
    elif row["done_at"] is not None:
        state = "done"
    elif row["lease_until"] is not None and row["lease_until"] > row["observed_at"]:
        state = "running"
    elif row["run_after"] <= row["observed_at"]:
        state = "enqueued"
    else:
        state = "queued"
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "state": state,
        "derivation": row["derivation"],
        "entity": row["entity"],
        "reasons": sorted(
            str(key) for key, value in (row["payload"] or {}).items() if value is True
        ),
        "dedupe_key": row["dedupe_key"],
        "run_after": _json_time(row["run_after"]),
        "attempts": int(row["attempts"]),
        "lease_until": _json_time(row["lease_until"]),
        "done_at": _json_time(row["done_at"]),
        "dead_at": _json_time(row["dead_at"]),
        "last_error_kind": row["last_error_kind"],
        "attempt_run_ids": attempt_runs,
        "successful_run_id": successful,
    }


async def retry_dead_job(
    pool: AsyncConnectionPool[Any], *, workspace: str, job_id: UUID
) -> dict[str, Any]:
    """Requeue one dead job while fencing newer active derive work."""

    try:
        async with pool.connection() as conn, conn.transaction():
            result = await conn.execute(
                """
            select id, kind, derivation, entity, dead_at
            from job
            where workspace = %s and id = %s
            for update
            """,
                (workspace, job_id),
            )
            row = await result.fetchone()
            if row is None:
                raise JobNotFound(f"job does not exist: {job_id}")
            if row["dead_at"] is None:
                raise JobRetryConflict("only dead jobs can be retried")
            if row["kind"] == "derive":
                active = await conn.execute(
                    """
                select 1 from job
                where workspace = %s and kind = 'derive'
                  and derivation = %s and entity = %s and id <> %s
                  and done_at is null and dead_at is null
                limit 1
                """,
                    (workspace, row["derivation"], row["entity"], job_id),
                )
                if await active.fetchone() is not None:
                    raise JobRetryConflict("a newer active derive job owns this partition")
            updated = await conn.execute(
                """
            update job
            set dead_at = null, lease_until = null, locked_by = null,
                attempts = 0, run_after = clock_timestamp(),
                last_error_kind = null, last_error = null
            where workspace = %s and id = %s and dead_at is not null
            returning id
            """,
                (workspace, job_id),
            )
            if await updated.fetchone() is None:
                raise JobRetryConflict("job changed before retry")
    except errors.UniqueViolation as exc:
        raise JobRetryConflict("a newer active job owns this partition") from exc
    log_event(LOGGER, "info", "job.retry_requested", job_id=str(job_id), workspace=workspace)
    return await get_job_status(pool, workspace=workspace, job_id=job_id)


async def retry_job(
    pool: AsyncConnectionPool[Any], *, workspace: str, job_id: UUID
) -> dict[str, Any]:
    """Compatibility alias for the dead-job retry operation."""

    return await retry_dead_job(pool, workspace=workspace, job_id=job_id)


__all__ = [
    "JobNotFound",
    "JobRetryConflict",
    "claim_job",
    "complete_job",
    "dead_letter_job",
    "get_job_status",
    "heartbeat_job",
    "reap_expired_final_attempts",
    "release_not_ready_job",
    "retry_dead_job",
    "retry_job",
    "retry_or_dead_letter_job",
]
