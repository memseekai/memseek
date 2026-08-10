"""Enrichment, projection, derivation, and M5 cron worker runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from memseek.config import Settings, get_settings
from memseek.db import (
    DatabasePool,
    close_pool,
    create_pool,
    open_pool,
    verify_storage_compatibility,
)
from memseek.logging import configure_logging, log_event

if TYPE_CHECKING:
    from collections.abc import Mapping

    from memseek.definitions import DefinitionCatalog
    from memseek.search.registry import SearchBackend
    from memseek.workspace_catalog import WorkspaceCatalogRegistry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Resources shared by the M1 dispatch lanes."""

    settings: Settings
    catalog: DefinitionCatalog
    pool: DatabasePool
    catalog_registry: WorkspaceCatalogRegistry | None = None


@dataclass(frozen=True, slots=True)
class WorkerPassResult:
    """Bounded enrichment work plus every queue item runnable at pass time."""

    enrichment_selected: int
    enrichment_ready: int
    projection_jobs: int
    derivation_jobs: int
    retention_jobs: int
    not_ready_jobs: int
    expired_artifact_uses: int = 0
    backfill_batches: int = 0
    backfilled_annotations: int = 0
    # How many lanes this pass contained a failure for.  Reported rather than
    # raised so the pass stays a complete account of what a worker accomplished,
    # and so the poll loop can back off instead of spinning on a poison pill.
    lane_failures: int = 0

    @property
    def busy(self) -> bool:
        return any(
            (
                self.enrichment_selected,
                self.projection_jobs,
                self.derivation_jobs,
                self.retention_jobs,
                self.not_ready_jobs,
                # A purged page did work, so the next pass runs without a poll
                # delay until the expired backlog is drained.
                self.expired_artifact_uses,
                # A claimed backfill batch is progress even when it wrote nothing
                # — a confirming sweep writes no annotations but must continue.
                self.backfill_batches,
            )
        )


def _load_catalog(settings: Settings) -> DefinitionCatalog:
    from memseek.definitions import load_definition_catalog

    return load_definition_catalog(settings)


@asynccontextmanager
async def worker_lifespan(
    settings: Settings | None = None,
    *,
    catalog: DefinitionCatalog | None = None,
    pool: DatabasePool | None = None,
    verify_storage: bool = True,
) -> AsyncIterator[WorkerRuntime]:
    """Load definitions, open/wait the pool, verify storage, then close it."""

    runtime_settings = settings or get_settings()
    from memseek.derive.tasks import import_task_modules

    import_task_modules(runtime_settings.task_modules)
    configure_logging(logging.DEBUG if runtime_settings.llm_debug else logging.INFO)
    runtime_pool = pool or create_pool(runtime_settings)
    try:
        runtime_catalog = catalog or _load_catalog(runtime_settings)
        await open_pool(runtime_pool)
        if verify_storage:
            # A worker serves many workspace packages; semantic metadata is
            # checked against the catalog selected for each claimed job.
            await verify_storage_compatibility(
                runtime_pool, runtime_settings, runtime_catalog, semantics=False
            )
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        runtime = WorkerRuntime(
            runtime_settings,
            runtime_catalog,
            runtime_pool,
            WorkspaceCatalogRegistry(runtime_pool, runtime_settings, runtime_catalog),
        )
        log_event(LOGGER, "info", "worker.started")
        yield runtime
    except BaseException as exc:
        log_event(
            LOGGER,
            "error",
            "worker.lifecycle_failed",
            exception_type=type(exc).__name__,
        )
        raise
    finally:
        try:
            from memseek.llm.openai_compat import openai_compat

            await openai_compat.aclose()
        finally:
            if not runtime_pool.closed:
                await close_pool(runtime_pool)
        log_event(LOGGER, "info", "worker.stopped")


async def run_worker(
    settings: Settings | None = None,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run M1 passes until a signal or caller-owned stop event requests exit.

    Only a stop request or a lifespan failure ends this loop.  A pass that raises
    is logged and retried under backoff: the queues it serves are durable, so the
    same work is still claimable on the next pass, and exiting the process would
    merely convert a transient fault — a database restart, a pool timeout, a
    provider outage — into an outage of every lane until something restarted it.
    """

    own_event = stop_event is None
    event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if own_event:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, event.set)
            except NotImplementedError, RuntimeError:
                continue
            installed_signals.append(signum)
    try:
        async with worker_lifespan(settings) as runtime:
            worker_id = _worker_id()
            consecutive_failures = 0
            while not event.is_set():
                try:
                    result = await run_worker_once(runtime, worker_id=worker_id)
                except Exception as exc:
                    # BaseException — cancellation, SIGINT, SystemExit — still
                    # ends the loop; those are exit requests, not faults.
                    consecutive_failures += 1
                    log_event(
                        LOGGER,
                        "error",
                        "worker.pass_failed",
                        exception_type=type(exc).__name__,
                        consecutive_failures=consecutive_failures,
                    )
                    delay = _failure_backoff_s(runtime.settings, consecutive_failures)
                else:
                    consecutive_failures = consecutive_failures + 1 if result.lane_failures else 0
                    if result.busy:
                        # Some lane made progress, so keep the pass rate up even
                        # while another lane is failing.
                        continue
                    delay = (
                        _failure_backoff_s(runtime.settings, consecutive_failures)
                        if consecutive_failures
                        else runtime.settings.worker_poll_ms / 1_000
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=delay)
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


# A persistently failing worker must stay responsive to recovery without filling
# the log at the poll rate, so backoff grows from the poll interval to this cap.
_MAX_FAILURE_BACKOFF_S = 30.0


def _failure_backoff_s(settings: Settings, consecutive_failures: int) -> float:
    """Return the capped exponential delay owed after N consecutive failed passes."""

    base = settings.worker_poll_ms / 1_000
    return min(base * 2 ** min(consecutive_failures, 8), _MAX_FAILURE_BACKOFF_S)


def _worker_id() -> str:
    return f"worker-{os.getpid()}-{uuid4().hex}"


async def _catalog_for(runtime: WorkerRuntime, workspace: str) -> DefinitionCatalog:
    """Resolve a workspace package, retaining the shipped catalog fallback."""

    if runtime.catalog_registry is None:
        return runtime.catalog
    return await runtime.catalog_registry.get(workspace)


async def _heartbeat_loop(
    runtime: WorkerRuntime,
    claimed: object,
    stop: asyncio.Event,
) -> None:
    from memseek.jobs import heartbeat_job
    from memseek.models import ClaimedJob, LeaseLost

    assert isinstance(claimed, ClaimedJob)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=runtime.settings.job_heartbeat_s)
        except TimeoutError:
            try:
                await heartbeat_job(runtime.pool, claimed, runtime.settings.job_lease_s)
            except LeaseLost:
                if stop.is_set():
                    return
                raise


async def _with_heartbeat[T](
    runtime: WorkerRuntime,
    claimed: object,
    operation: Awaitable[T],
) -> T:
    stop = asyncio.Event()
    operation_task = asyncio.ensure_future(operation)
    heartbeat_task = asyncio.create_task(_heartbeat_loop(runtime, claimed, stop))
    try:
        done, _pending = await asyncio.wait(
            (operation_task, heartbeat_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            return await operation_task
        # Propagate lease loss as its original exception type.  TaskGroup would
        # wrap handler/configuration failures in an ExceptionGroup, preventing
        # the dispatcher's explicit LeaseLost and config classifications.
        await heartbeat_task
        return await operation_task
    finally:
        stop.set()
        if not operation_task.done():
            operation_task.cancel()
        await asyncio.gather(operation_task, heartbeat_task, return_exceptions=True)


def _safe_job_error(kind: str, exc: BaseException) -> str:
    """Keep durable queue diagnostics useful without persisting record/provider bodies."""

    return f"{kind}: {type(exc).__name__}"


def _queue_lag_ms(run_after: datetime) -> int:
    """Return bounded scheduling lag without logging payload or record data."""

    return max(0, round((datetime.now(UTC) - run_after).total_seconds() * 1_000))


def _entity_log_hash(workspace: str, entity: str | None) -> str | None:
    """Hash an entity in its workspace before it crosses the logging boundary."""

    if entity is None:
        return None
    digest = hashlib.sha256()
    digest.update(b"memseek.operational.entity.v1\x00")
    digest.update(workspace.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(entity.encode("utf-8"))
    return digest.hexdigest()


async def _retry_claim(runtime: WorkerRuntime, claimed: object, exc: BaseException) -> None:
    from memseek.jobs import retry_or_dead_letter_job
    from memseek.models import ClaimedJob

    assert isinstance(claimed, ClaimedJob)
    await retry_or_dead_letter_job(
        runtime.pool,
        claimed,
        max_attempts=runtime.settings.job_max_attempts,
        error_kind=type(exc).__name__,
        error=_safe_job_error("handler", exc),
    )


async def _drain_projection_jobs(
    runtime: WorkerRuntime,
    *,
    worker_id: str,
    backends: Mapping[str, SearchBackend] | None,
) -> int:
    from memseek.jobs import claim_job, complete_job
    from memseek.models import LeaseLost
    from memseek.projections import (
        ProjectionInvariantError,
        ProjectionPayloadError,
        execute_projection_job,
    )

    completed = 0
    while True:
        claimed = await claim_job(
            runtime.pool,
            worker_id=worker_id,
            kinds=("index_upsert", "index_delete"),
            lease_s=runtime.settings.job_lease_s,
            max_attempts=runtime.settings.job_max_attempts,
        )
        if claimed is None:
            return completed
        queue_lag_ms = _queue_lag_ms(claimed.run_after)
        log_event(
            LOGGER,
            "info",
            "projection.lag",
            job_id=str(claimed.id),
            workspace=claimed.workspace,
            kind=claimed.kind,
            attempts=claimed.attempts,
            queue_lag_ms=queue_lag_ms,
        )
        stage = "backend"
        try:
            catalog = await _catalog_for(runtime, claimed.workspace)
            await _with_heartbeat(
                runtime,
                claimed,
                execute_projection_job(
                    runtime.pool,
                    runtime.settings,
                    catalog,
                    claimed,
                    backends=backends,
                ),
            )
            stage = "completion"
            await complete_job(runtime.pool, claimed)
            completed += 1
            log_event(
                LOGGER,
                "info",
                "projection.completed",
                job_id=str(claimed.id),
                workspace=claimed.workspace,
                kind=claimed.kind,
                attempts=claimed.attempts,
                queue_lag_ms=queue_lag_ms,
            )
        except LeaseLost:
            continue
        except Exception as exc:
            event = (
                "projection.invalid"
                if isinstance(exc, (ProjectionInvariantError, ProjectionPayloadError))
                else f"projection.{stage}_failed"
            )
            log_event(
                LOGGER,
                "error",
                event,
                job_id=str(claimed.id),
                workspace=claimed.workspace,
                kind=claimed.kind,
                attempts=claimed.attempts,
                queue_lag_ms=queue_lag_ms,
                exception_type=type(exc).__name__,
            )
            try:
                await _retry_claim(runtime, claimed, exc)
            except LeaseLost:
                continue


async def _drain_derivation_jobs(
    runtime: WorkerRuntime,
    *,
    worker_id: str,
) -> tuple[int, int]:
    """Run configured derivations through one shared job lane."""

    from memseek.derive.runner import DerivationError, process_derivation_job
    from memseek.jobs import claim_job, dead_letter_job, release_not_ready_job
    from memseek.models import LeaseLost

    names = (
        None
        if runtime.catalog_registry is not None
        else tuple(sorted(getattr(runtime.catalog, "derivations", {})))
    )
    if names == ():
        return 0, 0
    completed = 0
    not_ready = 0
    while True:
        claimed = await claim_job(
            runtime.pool,
            worker_id=worker_id,
            kinds=("derive",),
            derivations=names,
            lease_s=runtime.settings.job_lease_s,
            max_attempts=runtime.settings.job_max_attempts,
        )
        if claimed is None:
            return completed, not_ready
        try:
            catalog = await _catalog_for(runtime, claimed.workspace)
            if claimed.derivation not in getattr(catalog, "derivations", {}):
                await release_not_ready_job(
                    runtime.pool,
                    claimed,
                    runtime.settings.unready_retry_s,
                )
                not_ready += 1
                continue
            result = await _with_heartbeat(
                runtime,
                claimed,
                process_derivation_job(
                    runtime.pool,
                    claimed=claimed,
                    settings=runtime.settings,
                    catalog=catalog,
                ),
            )
            if result.disposition == "not_ready":
                await release_not_ready_job(
                    runtime.pool,
                    claimed,
                    runtime.settings.unready_retry_s,
                )
                not_ready += 1
                log_event(
                    LOGGER,
                    "info",
                    "derive.not_ready",
                    job_id=str(claimed.id),
                    workspace=claimed.workspace,
                    derivation=claimed.derivation,
                    entity_sha256=_entity_log_hash(claimed.workspace, claimed.entity),
                    attempts=claimed.attempts,
                    high_seq=result.high_seq,
                )
                continue
            completed += 1
            log_event(
                LOGGER,
                "info",
                "derive.completed",
                job_id=str(claimed.id),
                workspace=claimed.workspace,
                derivation=claimed.derivation,
                entity_sha256=_entity_log_hash(claimed.workspace, claimed.entity),
                attempts=claimed.attempts,
                high_seq=result.high_seq,
                output_count=result.output_count,
            )
        except LeaseLost:
            continue
        except DerivationError as exc:
            try:
                if exc.kind in {"config", "budget", "erased"}:
                    await dead_letter_job(
                        runtime.pool,
                        claimed,
                        error_kind=exc.kind,
                        error=f"derive: {exc.kind}",
                    )
                else:
                    await _retry_claim(runtime, claimed, exc)
            except LeaseLost:
                continue
        except Exception as exc:
            try:
                await _retry_claim(runtime, claimed, exc)
            except LeaseLost:
                continue


async def _cron_entities(
    conn: Any,
    *,
    workspace: str,
    definition: Any,
    mode: str,
    cursor: str | None,
) -> tuple[list[str], bool, str | None]:
    """Return one bounded lexical cron page without exposing record content."""

    clauses = [
        "workspace = %s",
        "entity <> ''",
        "collection <> '_system'",
    ]
    params: list[object] = [workspace]
    if cursor is not None:
        clauses.append("entity > %s")
        params.append(cursor)
    result = await conn.execute(
        f"""
        select distinct entity
        from record
        where {" and ".join(clauses)}
        order by entity
        limit 501
        """,
        params,
    )
    candidates = [str(row["entity"]) for row in await result.fetchall()]
    has_more = len(candidates) > 500
    page_cursor = candidates[499] if has_more else None
    if mode == "any":
        # A provenance-repair driver is already a bounded, per-entity selector.
        # Reuse that selector before queueing cron work so an hourly repair does
        # not create one noop run for every unrelated entity in the workspace.
        if definition.driver.kind == "stale_citations":
            from memseek.derive.basis import adapter_for

            adapter = adapter_for(definition.driver.kind)
            selected: list[str] = []
            for entity in candidates:
                basis = await adapter.resolve(
                    conn,
                    workspace=workspace,
                    entity=entity,
                    definition=definition,
                )
                if basis is not None and basis.input_rows:
                    selected.append(entity)
            return selected, has_more, page_cursor
        return candidates, has_more, page_cursor
    from memseek.triggers import _scope_clauses, _watermark

    selected: list[str] = []
    for entity in candidates:
        watermark = await _watermark(
            conn,
            workspace=workspace,
            entity=entity,
            derivation=definition.name,
        )
        clauses, params = _scope_clauses(
            definition.driver,
            workspace=workspace,
            entity=entity,
            watermark=watermark,
        )
        query = f"select exists(select 1 from record row where {' and '.join(clauses)}) as dirty"
        result = await conn.execute(
            query,
            params,
        )
        row = await result.fetchone()
        if row is not None and row["dirty"]:
            selected.append(entity)
    return selected, has_more, page_cursor


async def _process_cron_scan(runtime: WorkerRuntime, claimed: object) -> bool:
    """Process one persisted cron page and optionally chain its next page."""

    from psycopg.types.json import Jsonb

    from memseek.locks import acquire_workspace_lock
    from memseek.models import ClaimedJob, LeaseLost
    from memseek.triggers import _cooldown_due, claim_owned_tx, enqueue_derive_tx

    assert isinstance(claimed, ClaimedJob)
    if claimed.kind != "cron_scan" or claimed.derivation is None:
        raise ValueError("cron claim is malformed")
    trigger_name = claimed.payload.get("trigger")
    mode = claimed.payload.get("entities")
    cursor = claimed.payload.get("cursor")
    if not isinstance(trigger_name, str) or mode not in {"any", "dirty"}:
        raise ValueError("cron payload is malformed")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("cron cursor is malformed")
    catalog = await _catalog_for(runtime, claimed.workspace)
    trigger = getattr(catalog, "triggers", {}).get(trigger_name)
    definition = getattr(catalog, "derivations", {}).get(claimed.derivation)
    if trigger is None or trigger.cron is None or definition is None:
        raise ValueError("cron trigger or derivation is not configured")
    async with runtime.pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, claimed.workspace)
        await claim_owned_tx(conn, claimed)
        entities, has_more, page_cursor = await _cron_entities(
            conn,
            workspace=claimed.workspace,
            definition=definition,
            mode=mode,
            cursor=cursor,
        )
        for entity in entities[:500]:
            due = await _cooldown_due(
                conn,
                workspace=claimed.workspace,
                entity=entity,
                trigger=trigger,
            )
            await enqueue_derive_tx(
                conn,
                workspace=claimed.workspace,
                derivation=definition.name,
                entity=entity,
                reason=f"trigger:{trigger.name}:cron",
                run_after=due,
            )
        if has_more and page_cursor is not None:
            scheduled_at = claimed.payload.get("scheduled_at")
            if not isinstance(scheduled_at, str):
                raise ValueError("cron scheduled_at is malformed")
            dedupe = (
                f"{claimed.dedupe_key or f'cron:{definition.name}:{scheduled_at}'}:{page_cursor}"
            )
            await conn.execute(
                """
                insert into job (workspace, kind, derivation, dedupe_key, payload)
                values (%s, 'cron_scan', %s, %s, %s)
                on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
                """,
                (
                    claimed.workspace,
                    definition.name,
                    dedupe,
                    Jsonb(
                        {
                            "trigger": trigger.name,
                            "scheduled_at": scheduled_at,
                            "entities": mode,
                            "cursor": page_cursor,
                        }
                    ),
                ),
            )
        result = await conn.execute(
            """
            update job
            set done_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = null, last_error = null
            where id = %s and locked_by = %s and done_at is null and dead_at is null
              and lease_until > clock_timestamp()
            returning id
            """,
            (claimed.id, claimed.claim_token),
        )
        if await result.fetchone() is None:
            raise LeaseLost(f"job lease lost: {claimed.id}")
    log_event(
        LOGGER,
        "info",
        "cron.scan_completed",
        job_id=str(claimed.id),
        workspace=claimed.workspace,
        derivation=claimed.derivation,
        entity_count=min(len(entities), 500),
        chained=has_more,
    )
    return True


async def _drain_cron_jobs(runtime: WorkerRuntime, *, worker_id: str) -> int:
    from memseek.jobs import claim_job, dead_letter_job, retry_or_dead_letter_job
    from memseek.models import LeaseLost

    if not getattr(runtime.catalog, "triggers", None) and runtime.catalog_registry is None:
        return 0
    completed = 0
    while True:
        claimed = await claim_job(
            runtime.pool,
            worker_id=worker_id,
            kinds=("cron_scan",),
            lease_s=runtime.settings.job_lease_s,
            max_attempts=runtime.settings.job_max_attempts,
        )
        if claimed is None:
            return completed
        try:
            await _with_heartbeat(runtime, claimed, _process_cron_scan(runtime, claimed))
            completed += 1
        except LeaseLost:
            continue
        except ValueError as exc:
            try:
                await dead_letter_job(
                    runtime.pool,
                    claimed,
                    error_kind="config",
                    error=_safe_job_error("cron", exc),
                )
            except LeaseLost:
                continue
        except Exception as exc:
            try:
                await retry_or_dead_letter_job(
                    runtime.pool,
                    claimed,
                    max_attempts=runtime.settings.job_max_attempts,
                    error_kind=type(exc).__name__,
                    error=_safe_job_error("cron", exc),
                )
            except LeaseLost:
                continue


async def _process_retention_purge(runtime: WorkerRuntime, claimed: object) -> int:
    """Run one catalog-declared tombstone retention policy under its job lease."""

    from memseek.definitions.base import split_exact_reference
    from memseek.erase import purge_tombstoned_pages_tx
    from memseek.models import ClaimedJob, LeaseLost
    from memseek.triggers import claim_owned_tx

    assert isinstance(claimed, ClaimedJob)
    if (
        claimed.kind != "retention_purge"
        or claimed.derivation is not None
        or claimed.entity is not None
    ):
        raise ValueError("retention claim is malformed")
    package_name = claimed.payload.get("package_name")
    package_version = claimed.payload.get("package_version")
    retention_name = claimed.payload.get("retention")
    if (
        not isinstance(package_name, str)
        or not isinstance(package_version, str)
        or not isinstance(retention_name, str)
    ):
        raise ValueError("retention payload is malformed")
    catalog = await _catalog_for(runtime, claimed.workspace)
    try:
        package = catalog.resolve_package(package_name, package_version)
    except KeyError as exc:
        raise ValueError("retention package is not configured") from exc
    retention = next(
        (item for item in package.retentions if item.name == retention_name),
        None,
    )
    if retention is None:
        raise ValueError("retention policy is not configured")
    collection, version = split_exact_reference(retention.collection)

    async with runtime.pool.connection() as conn, conn.transaction():
        await claim_owned_tx(conn, claimed)
        erasure = await purge_tombstoned_pages_tx(
            conn,
            workspace=claimed.workspace,
            collection=collection,
            collection_version=int(version),
            after_days=retention.after_days,
            max_pages=retention.max_pages,
            settings=runtime.settings,
            catalog=catalog,
        )
        result = await conn.execute(
            """
            update job
            set done_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = null, last_error = null
            where id = %s and locked_by = %s and done_at is null and dead_at is null
              and lease_until > clock_timestamp()
            returning id
            """,
            (claimed.id, claimed.claim_token),
        )
        if await result.fetchone() is None:
            raise LeaseLost(f"job lease lost: {claimed.id}")
    deleted = erasure.deleted if erasure is not None else 0
    log_event(
        LOGGER,
        "info",
        "retention.purge_completed",
        job_id=str(claimed.id),
        workspace=claimed.workspace,
        retention=retention.name,
        deleted=deleted,
    )
    return deleted


async def _drain_retention_jobs(runtime: WorkerRuntime, *, worker_id: str) -> int:
    """Drain trusted internal retention jobs; there is no public purge route."""

    from memseek.erase import ErasureError
    from memseek.jobs import claim_job, dead_letter_job, retry_or_dead_letter_job
    from memseek.models import LeaseLost

    if runtime.catalog_registry is None and not any(
        package.retentions for package in getattr(runtime.catalog, "packages", {}).values()
    ):
        return 0
    completed = 0
    while True:
        claimed = await claim_job(
            runtime.pool,
            worker_id=worker_id,
            kinds=("retention_purge",),
            lease_s=runtime.settings.job_lease_s,
            max_attempts=runtime.settings.job_max_attempts,
        )
        if claimed is None:
            return completed
        try:
            await _with_heartbeat(runtime, claimed, _process_retention_purge(runtime, claimed))
            completed += 1
        except LeaseLost:
            continue
        except ValueError as exc:
            try:
                await dead_letter_job(
                    runtime.pool,
                    claimed,
                    error_kind="config",
                    error=_safe_job_error("retention", exc),
                )
            except LeaseLost:
                continue
        except ErasureError as exc:
            try:
                await dead_letter_job(
                    runtime.pool,
                    claimed,
                    error_kind=exc.code,
                    error=_safe_job_error("retention", exc),
                )
            except LeaseLost:
                continue
        except Exception as exc:
            try:
                await retry_or_dead_letter_job(
                    runtime.pool,
                    claimed,
                    max_attempts=runtime.settings.job_max_attempts,
                    error_kind=type(exc).__name__,
                    error=_safe_job_error("retention", exc),
                )
            except LeaseLost:
                continue


async def _release_backfill_claim(
    runtime: WorkerRuntime,
    claimed: Any,
    *,
    successor: Any | None = None,
) -> None:
    """Complete a backfill claim, optionally queueing its successor atomically.

    Completion and hand-off share one transaction so the lane can never lose a
    successor or leave a finished claim holding its lease.
    """

    from memseek.models import LeaseLost

    async with runtime.pool.connection() as conn, conn.transaction():
        result = await conn.execute(
            """
            update job
            set done_at = clock_timestamp(), lease_until = null, locked_by = null,
                last_error_kind = null, last_error = null
            where id = %s and locked_by = %s and done_at is null and dead_at is null
              and lease_until > clock_timestamp()
            returning id
            """,
            (claimed.id, claimed.claim_token),
        )
        if await result.fetchone() is None:
            log_event(LOGGER, "warning", "job.lease_lost", job_id=str(claimed.id))
            raise LeaseLost(f"job lease lost: {claimed.id}")
        if successor is not None:
            await conn.execute(
                """
                insert into job (workspace, kind, payload)
                values (%s, 'annotation_backfill', %s)
                """,
                (successor.workspace, Jsonb({"backfill_id": str(successor.id)})),
            )


async def _process_annotation_backfill(runtime: WorkerRuntime, claimed: object) -> int:
    """Run exactly one bounded batch of a backfill, then hand the lane back.

    One batch of at most ``BACKFILL_BATCH`` records per claim, and one claim per
    worker pass, is what lets an unbudgeted whole-corpus backfill run to
    completion without starving any other lane: every pass does a bounded amount
    of backfill work and then services cron, retention, projection, and derivation
    again.  A pass that backfilled anything is marked busy, so the next pass starts
    immediately rather than waiting on the poll interval.

    Reaching the end of the table is not by itself proof of completion: row
    selection skips records another lane currently holds locked, and the cursor
    moves past them.  So an exhausted sweep rewinds to the start, and the backfill
    is only ``done`` once a sweep from the first record finds nothing left to do.
    """

    from memseek.backfill import claim_state, record_progress, rewind
    from memseek.enrichment import backfill_annotations
    from memseek.models import ClaimedJob

    assert isinstance(claimed, ClaimedJob)
    if claimed.kind != "annotation_backfill":
        raise ValueError("backfill claim is malformed")
    raw_id = claimed.payload.get("backfill_id")
    if not isinstance(raw_id, str):
        raise ValueError("backfill payload is malformed")
    backfill_id = UUID(raw_id)
    handle = await claim_state(runtime.pool, workspace=claimed.workspace, backfill_id=backfill_id)
    if handle is None:
        # Cancelled or already finished: the claim is a no-op, not a failure.
        await _release_backfill_claim(runtime, claimed)
        return 0

    limit = runtime.settings.backfill_batch
    if handle.max_rows is not None:
        remaining = handle.max_rows - handle.scanned
        if remaining <= 0:
            # The row budget is spent. Re-request the same target to take another
            # slice; presence-based selection resumes where this one stopped.
            await _finish_backfill(runtime, backfill_id, handle, reason="budget")
            await _release_backfill_claim(runtime, claimed)
            return 0
        limit = min(limit, remaining)

    catalog = await _catalog_for(runtime, claimed.workspace)
    swept_from_start = handle.cursor_seq == 0
    batch = await backfill_annotations(
        runtime.pool,
        runtime.settings,
        catalog,
        workspace=handle.workspace,
        collection=handle.collection,
        version=handle.collection_version,
        processor=handle.processor,
        after_seq=handle.cursor_seq,
        limit=limit,
    )
    if batch.scanned:
        handle = await record_progress(
            runtime.pool,
            backfill_id=backfill_id,
            cursor_seq=batch.last_seq,
            scanned=batch.scanned,
            annotated=batch.annotated,
        )
    if batch.exhausted and swept_from_start and batch.scanned == 0:
        await _finish_backfill(runtime, backfill_id, handle, reason="complete")
        await _release_backfill_claim(runtime, claimed)
        return batch.annotated
    if batch.exhausted:
        # Rewind and confirm: anything skipped under a concurrent row lock is
        # still eligible, and a fresh sweep from the start is what finds it.
        handle = await rewind(runtime.pool, backfill_id=backfill_id)
    if not handle.live:
        await _release_backfill_claim(runtime, claimed)
        return batch.annotated
    # More work remains: complete this claim and queue its successor in one step.
    await _release_backfill_claim(runtime, claimed, successor=handle)
    return batch.annotated


async def _finish_backfill(
    runtime: WorkerRuntime,
    backfill_id: UUID,
    handle: Any,
    *,
    reason: str,
) -> None:
    from memseek.backfill import finish

    await finish(runtime.pool, backfill_id=backfill_id, state="done")
    log_event(
        LOGGER,
        "info",
        "backfill.completed",
        workspace=handle.workspace,
        backfill_id=str(backfill_id),
        collection=handle.collection,
        version=handle.collection_version,
        processor=handle.processor,
        scanned=handle.scanned,
        annotated=handle.annotated,
        reason=reason,
    )


async def _drain_backfill_jobs(runtime: WorkerRuntime, *, worker_id: str) -> tuple[int, int]:
    """Run one bounded backfill batch, then let the rest of the pass proceed.

    Deliberately *not* a drain loop.  A backfill's successor is queued the moment
    its predecessor completes, so looping here would let one whole-corpus backfill
    occupy the worker until it finished.  Taking a single claim per pass bounds that
    to ``BACKFILL_BATCH`` records, and the busy flag brings the next pass round
    immediately, so throughput stays high while every other lane keeps its turn.
    """

    from memseek.backfill import BackfillError, finish
    from memseek.jobs import claim_job, dead_letter_job, retry_or_dead_letter_job
    from memseek.models import LeaseLost

    claimed = await claim_job(
        runtime.pool,
        worker_id=worker_id,
        kinds=("annotation_backfill",),
        lease_s=runtime.settings.job_lease_s,
        max_attempts=runtime.settings.job_max_attempts,
    )
    if claimed is None:
        return 0, 0
    try:
        return 1, await _with_heartbeat(
            runtime, claimed, _process_annotation_backfill(runtime, claimed)
        )
    except LeaseLost:
        # Another worker owns it now; the next pass picks up whatever is queued.
        return 1, 0
    except (BackfillError, ValueError) as exc:
        # A malformed or impossible request is terminal; record it on the handle so
        # the author sees why rather than only in worker logs.
        raw_id = claimed.payload.get("backfill_id")
        if isinstance(raw_id, str):
            await finish(
                runtime.pool,
                backfill_id=UUID(raw_id),
                state="failed",
                error=_safe_job_error("backfill", exc),
            )
        with suppress(LeaseLost):
            await dead_letter_job(
                runtime.pool,
                claimed,
                error_kind="config",
                error=_safe_job_error("backfill", exc),
            )
        return 1, 0
    except Exception as exc:
        with suppress(LeaseLost):
            await retry_or_dead_letter_job(
                runtime.pool,
                claimed,
                max_attempts=runtime.settings.job_max_attempts,
                error_kind=type(exc).__name__,
                error=_safe_job_error("backfill", exc),
            )
        return 1, 0


class _LaneTally:
    """Run each pass lane so a failure is contained to the lane that raised it.

    The lanes of a pass are independent queues.  One workspace whose stored
    catalog will not compile, one search backend that is refusing writes, one
    provider outage — before containment any of those aborted the whole pass, so
    a single poisoned workspace starved cron, retention, projection, derivation
    and backfill for every other workspace on the deployment.  A contained lane
    keeps its fallback value and is counted, and the count is what makes the
    poll loop back off instead of retrying the same poison at the poll rate.
    """

    def __init__(self) -> None:
        self.failures = 0

    async def run[T](self, lane: str, operation: Awaitable[T], fallback: T) -> T:
        try:
            return await operation
        except Exception as exc:
            self.failures += 1
            log_event(
                LOGGER,
                "error",
                "worker.lane_failed",
                lane=lane,
                exception_type=type(exc).__name__,
            )
            return fallback


async def run_worker_once(
    runtime: WorkerRuntime,
    *,
    worker_id: str | None = None,
    backends: Mapping[str, SearchBackend] | None = None,
) -> WorkerPassResult:
    """Run one enrichment unit and drain all currently runnable M1 jobs."""

    from memseek.enrichment import EnrichmentSweepResult, enrich_once

    identity = worker_id or _worker_id()
    lanes = _LaneTally()
    if (
        runtime.catalog_registry is not None
        or getattr(runtime.catalog, "triggers", None)
        or any(package.retentions for package in getattr(runtime.catalog, "packages", {}).values())
    ):
        from memseek.triggers import schedule_cron_jobs

        cron_jobs = await lanes.run(
            "cron_schedule",
            schedule_cron_jobs(
                runtime.pool,
                catalog=runtime.catalog,
                catalog_for_workspace=(
                    (lambda workspace: _catalog_for(runtime, workspace))
                    if runtime.catalog_registry is not None
                    else None
                ),
                max_catchup=runtime.settings.max_cron_catchup,
            ),
            0,
        )
        if cron_jobs:
            log_event(
                LOGGER,
                "info",
                "cron.jobs_scheduled",
                count=cron_jobs,
            )
    enrichment = await lanes.run(
        "enrichment",
        enrich_once(
            runtime.pool,
            runtime.settings,
            runtime.catalog,
            catalog_for_workspace=(
                (lambda workspace: _catalog_for(runtime, workspace))
                if runtime.catalog_registry is not None
                else None
            ),
        ),
        EnrichmentSweepResult("none", 0, 0, 0),
    )
    await lanes.run("cron_jobs", _drain_cron_jobs(runtime, worker_id=identity), 0)
    retention_jobs = await lanes.run(
        "retention_jobs", _drain_retention_jobs(runtime, worker_id=identity), 0
    )
    # Expiry is a deployment setting rather than a package policy, and the rows
    # are operational metadata with no provenance closure, so this needs no job
    # lane: one bounded page per pass across every workspace is enough.
    from memseek.artifact_uses import purge_expired_artifact_uses

    expired_artifact_uses = await lanes.run(
        "artifact_use_expiry",
        purge_expired_artifact_uses(runtime.pool, runtime.settings),
        0,
    )
    if expired_artifact_uses:
        log_event(
            LOGGER,
            "info",
            "artifact_uses.expired_purged",
            count=expired_artifact_uses,
        )
    projection_jobs = await lanes.run(
        "projection_jobs",
        _drain_projection_jobs(runtime, worker_id=identity, backends=backends),
        0,
    )
    derivation_jobs, not_ready_jobs = await lanes.run(
        "derivation_jobs",
        _drain_derivation_jobs(runtime, worker_id=identity),
        (0, 0),
    )
    # Backfills run after ingest-path enrichment so improving history never
    # delays admitting new records.
    backfill_batches, backfilled = await lanes.run(
        "backfill_jobs",
        _drain_backfill_jobs(runtime, worker_id=identity),
        (0, 0),
    )
    projection_jobs += await lanes.run(
        "projection_jobs",
        _drain_projection_jobs(runtime, worker_id=identity, backends=backends),
        0,
    )
    result = WorkerPassResult(
        enrichment_selected=enrichment.selected,
        enrichment_ready=enrichment.ready,
        projection_jobs=projection_jobs,
        derivation_jobs=derivation_jobs,
        retention_jobs=retention_jobs,
        not_ready_jobs=not_ready_jobs,
        expired_artifact_uses=expired_artifact_uses,
        backfill_batches=backfill_batches,
        backfilled_annotations=backfilled,
        lane_failures=lanes.failures,
    )
    log_event(
        LOGGER,
        "info",
        "worker.pass",
        busy=result.busy,
        enrichment_selected=result.enrichment_selected,
        enrichment_ready=result.enrichment_ready,
        projection_jobs=result.projection_jobs,
        derivation_jobs=result.derivation_jobs,
        retention_jobs=result.retention_jobs,
        not_ready_jobs=result.not_ready_jobs,
        expired_artifact_uses=result.expired_artifact_uses,
        backfill_batches=result.backfill_batches,
        backfilled_annotations=result.backfilled_annotations,
        lane_failures=result.lane_failures,
    )
    return result
