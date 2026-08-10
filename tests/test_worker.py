"""Worker resource ownership tests."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import memseek.artifact_uses as artifact_uses_module
import memseek.derive.runner as derive_runner_module
import memseek.enrichment as enrichment_module
import memseek.jobs as jobs_module
import memseek.projections as projections_module
import memseek.worker as worker_module
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.derive.runner import DerivationError, DerivationJobResult
from memseek.llm.openai_compat import openai_compat
from memseek.logging import configure_logging
from memseek.models import ClaimedJob
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.worker import (
    WorkerPassResult,
    WorkerRuntime,
    run_worker_once,
    worker_lifespan,
)


class TrackingPool:
    def __init__(self) -> None:
        self.opened = False
        self.waited = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def wait(self, **kwargs: float) -> None:
        assert kwargs == {"timeout": 30.0}
        self.waited = True

    async def close(self) -> None:
        self.closed = True


async def test_worker_lifespan_opens_waits_and_closes_runtime_resources(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracking = TrackingPool()
    provider_closed = False

    async def close_provider() -> None:
        nonlocal provider_closed
        provider_closed = True

    monkeypatch.setattr(openai_compat, "aclose", close_provider)
    async with worker_lifespan(
        settings,
        catalog=cast(Any, object()),
        pool=cast(DatabasePool, tracking),
        verify_storage=False,
    ) as runtime:
        assert runtime.pool is tracking
        assert tracking.opened
        assert tracking.waited
        assert not tracking.closed
    assert tracking.closed
    assert provider_closed


class FailingTrackingPool(TrackingPool):
    async def wait(self, **kwargs: float) -> None:
        await super().wait(**kwargs)
        raise RuntimeError("pool unavailable")


async def test_worker_failed_wait_closes_pool(settings: Settings) -> None:
    tracking = FailingTrackingPool()
    with pytest.raises(RuntimeError, match="pool unavailable"):
        async with worker_lifespan(
            settings,
            catalog=cast(Any, object()),
            pool=cast(DatabasePool, tracking),
            verify_storage=False,
        ):
            pytest.fail("worker lifespan must not start")
    assert tracking.closed


async def test_run_worker_once_enriches_and_drains_projection_jobs(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "worker-once")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maria",
                    collection="main",
                    type="event",
                    text="Important update [importance=8]",
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    record_id = inserted.inserted[0].id
    assert inserted.inserted[0].ready is False

    result = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="test-worker",
    )

    assert result.enrichment_selected == 1
    assert result.enrichment_ready == 1
    assert result.projection_jobs >= 1
    async with db_pool.connection() as conn:
        record_result = await conn.execute(
            """
            select enriched_at, embedding is not null as embedded, scores, annotations
            from record where id = %s
            """,
            (record_id,),
        )
        row = await record_result.fetchone()
        jobs_result = await conn.execute(
            "select count(*) as pending from job where done_at is null and dead_at is null"
        )
        jobs = await jobs_result.fetchone()
    assert row is not None
    assert row["enriched_at"] is not None
    assert row["embedded"] is True
    assert row["scores"]["importance"] == 8
    assert set(row["annotations"]) >= {"embedding_v1", "importance"}
    assert jobs is not None
    assert jobs["pending"] == 0


def _claimed_projection(*, payload: dict[str, Any] | None = None) -> ClaimedJob:
    now = datetime.now(UTC)
    return ClaimedJob(
        id=uuid4(),
        workspace="safe-workspace",
        kind="index_upsert",
        derivation=None,
        entity=None,
        payload=payload or {"records": []},
        dedupe_key=None,
        run_after=now - timedelta(seconds=2),
        attempts=1,
        lease_until=now + timedelta(minutes=5),
        claim_token=f"test-worker:{uuid4()}",
        created_at=now - timedelta(seconds=3),
    )


def _claimed_derivation(*, entity: str) -> ClaimedJob:
    now = datetime.now(UTC)
    return ClaimedJob(
        id=uuid4(),
        workspace="safe-workspace",
        kind="derive",
        derivation="contradiction",
        entity=entity,
        payload={"high_seq": 42},
        dedupe_key=None,
        run_after=now - timedelta(seconds=2),
        attempts=1,
        lease_until=now + timedelta(minutes=5),
        claim_token=f"test-worker:{uuid4()}",
        created_at=now - timedelta(seconds=3),
    )


def _worker_runtime(settings: Settings) -> WorkerRuntime:
    return WorkerRuntime(
        settings=settings,
        catalog=cast(Any, object()),
        pool=cast(DatabasePool, object()),
    )


def _derivation_worker_runtime(settings: Settings) -> WorkerRuntime:
    catalog = SimpleNamespace(derivations={"contradiction": object()})
    return WorkerRuntime(
        settings=settings,
        catalog=cast(Any, catalog),
        pool=cast(DatabasePool, object()),
    )


async def test_heartbeat_wrapper_preserves_handler_exception_type(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def waiting_heartbeat(
        runtime: WorkerRuntime, claimed: object, stop: asyncio.Event
    ) -> None:
        del runtime, claimed
        await stop.wait()

    async def failed_operation() -> None:
        raise ValueError("configuration failure")

    monkeypatch.setattr(worker_module, "_heartbeat_loop", waiting_heartbeat)
    with pytest.raises(ValueError, match="configuration failure"):
        await worker_module._with_heartbeat(_worker_runtime(settings), object(), failed_operation())


def _json_log_documents(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_projection_completion_logs_safe_queue_lag(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    claimed = _claimed_projection()
    monkeypatch.setattr(
        jobs_module,
        "claim_job",
        AsyncMock(side_effect=(claimed, None)),
    )
    monkeypatch.setattr(jobs_module, "complete_job", AsyncMock())
    monkeypatch.setattr(projections_module, "execute_projection_job", AsyncMock())

    completed = await worker_module._drain_projection_jobs(
        _worker_runtime(settings), worker_id="test-worker", backends=None
    )

    assert completed == 1
    documents = _json_log_documents(stream)
    lag = next(document for document in documents if document["event"] == "projection.lag")
    completion = next(
        document for document in documents if document["event"] == "projection.completed"
    )
    assert lag["job_id"] == str(claimed.id)
    assert lag["queue_lag_ms"] >= 2_000
    assert completion["queue_lag_ms"] == lag["queue_lag_ms"]
    assert completion["kind"] == "index_upsert"


async def test_projection_backend_failure_log_excludes_payload_and_exception_message(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    secret = "do-not-log-record-or-provider-data"
    claimed = _claimed_projection(payload={"records": [{"text": secret}]})
    monkeypatch.setattr(
        jobs_module,
        "claim_job",
        AsyncMock(side_effect=(claimed, None)),
    )
    monkeypatch.setattr(
        projections_module,
        "execute_projection_job",
        AsyncMock(side_effect=RuntimeError(secret)),
    )
    retry = AsyncMock()
    monkeypatch.setattr(worker_module, "_retry_claim", retry)

    async def await_direct(
        runtime: WorkerRuntime, claimed_job: object, operation: Awaitable[Any]
    ) -> Any:
        del runtime, claimed_job
        return await operation

    monkeypatch.setattr(worker_module, "_with_heartbeat", await_direct)

    completed = await worker_module._drain_projection_jobs(
        _worker_runtime(settings), worker_id="test-worker", backends=None
    )

    assert completed == 0
    retry.assert_awaited_once()
    rendered = stream.getvalue()
    assert secret not in rendered
    failure = next(
        document
        for document in _json_log_documents(stream)
        if document["event"] == "projection.backend_failed"
    )
    assert failure["exception_type"] == "RuntimeError"
    assert failure["job_id"] == str(claimed.id)


async def test_worker_pass_logs_only_bounded_counters(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    monkeypatch.setattr(
        enrichment_module,
        "enrich_once",
        AsyncMock(return_value=SimpleNamespace(selected=4, ready=3)),
    )
    monkeypatch.setattr(
        worker_module,
        "_drain_projection_jobs",
        AsyncMock(side_effect=(2, 1)),
    )
    monkeypatch.setattr(
        worker_module,
        "_drain_derivation_jobs",
        AsyncMock(return_value=(1, 1)),
    )
    monkeypatch.setattr(
        artifact_uses_module,
        "purge_expired_artifact_uses",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        worker_module,
        "_drain_backfill_jobs",
        AsyncMock(return_value=(0, 0)),
    )

    result = await run_worker_once(_worker_runtime(settings), worker_id="test-worker")

    assert result.projection_jobs == 3
    document = next(
        document for document in _json_log_documents(stream) if document["event"] == "worker.pass"
    )
    assert document == {
        "busy": True,
        "enrichment_ready": 3,
        "enrichment_selected": 4,
        "event": "worker.pass",
        "level": "info",
        "logger": "memseek.worker",
        "not_ready_jobs": 1,
        "projection_jobs": 3,
        "derivation_jobs": 1,
        "retention_jobs": 0,
        "expired_artifact_uses": 0,
        "backfill_batches": 0,
        "backfilled_annotations": 0,
        "lane_failures": 0,
        "timestamp": document["timestamp"],
    }


async def test_derivation_completion_and_not_ready_logs_hash_entity(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    raw_entity = "private-entity-identifier"
    completed_claim = _claimed_derivation(entity=raw_entity)
    not_ready_claim = _claimed_derivation(entity=raw_entity)
    monkeypatch.setattr(
        jobs_module,
        "claim_job",
        AsyncMock(side_effect=(completed_claim, not_ready_claim, None)),
    )
    release = AsyncMock()
    monkeypatch.setattr(jobs_module, "release_not_ready_job", release)
    monkeypatch.setattr(
        derive_runner_module,
        "process_derivation_job",
        AsyncMock(
            side_effect=(
                DerivationJobResult("done", high_seq=40, output_count=2),
                DerivationJobResult("not_ready", high_seq=42),
            )
        ),
    )

    completed, not_ready = await worker_module._drain_derivation_jobs(
        _derivation_worker_runtime(settings), worker_id="test-worker"
    )

    assert (completed, not_ready) == (1, 1)
    release.assert_awaited_once()
    rendered = stream.getvalue()
    assert raw_entity not in rendered
    documents = _json_log_documents(stream)
    completion = next(document for document in documents if document["event"] == "derive.completed")
    deferred = next(document for document in documents if document["event"] == "derive.not_ready")
    assert completion["entity_sha256"] == deferred["entity_sha256"]
    assert len(completion["entity_sha256"]) == 64
    assert completion["output_count"] == 2
    assert completion["high_seq"] == 40
    assert deferred["high_seq"] == 42


async def test_derivation_config_failure_is_dead_lettered_without_exposing_detail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    raw_entity = "another-private-entity"
    secret = "provider-message-that-must-not-be-logged"
    claimed = _claimed_derivation(entity=raw_entity)
    monkeypatch.setattr(
        jobs_module,
        "claim_job",
        AsyncMock(side_effect=(claimed, None)),
    )
    dead_letter = AsyncMock()
    monkeypatch.setattr(jobs_module, "dead_letter_job", dead_letter)
    monkeypatch.setattr(
        derive_runner_module,
        "process_derivation_job",
        AsyncMock(side_effect=DerivationError("config", secret)),
    )

    async def await_direct(
        runtime: WorkerRuntime, claimed_job: object, operation: Awaitable[Any]
    ) -> Any:
        del runtime, claimed_job
        return await operation

    monkeypatch.setattr(worker_module, "_with_heartbeat", await_direct)

    completed, not_ready = await worker_module._drain_derivation_jobs(
        _derivation_worker_runtime(settings), worker_id="test-worker"
    )

    assert (completed, not_ready) == (0, 0)
    dead_letter.assert_awaited_once()
    rendered = stream.getvalue()
    assert raw_entity not in rendered
    assert secret not in rendered
    await_args = dead_letter.await_args
    assert await_args is not None
    assert await_args.kwargs == {
        "error_kind": "config",
        "error": "derive: config",
    }


async def test_run_worker_survives_failing_passes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising pass is logged and retried; only the stop event ends the loop."""

    stream = io.StringIO()
    configure_logging(stream=stream)
    stop = asyncio.Event()
    passes = 0

    @asynccontextmanager
    async def fake_lifespan(*_args: Any, **_kwargs: Any) -> AsyncIterator[WorkerRuntime]:
        yield _worker_runtime(settings)

    async def failing_pass(_runtime: WorkerRuntime, **_kwargs: Any) -> WorkerPassResult:
        nonlocal passes
        passes += 1
        if passes >= 3:
            stop.set()
        raise RuntimeError("transient database outage")

    monkeypatch.setattr(worker_module, "worker_lifespan", fake_lifespan)
    monkeypatch.setattr(worker_module, "run_worker_once", failing_pass)
    monkeypatch.setattr(worker_module, "_failure_backoff_s", lambda *_args: 0.0)

    await worker_module.run_worker(settings, stop_event=stop)

    assert passes == 3
    failures = [
        document
        for document in _json_log_documents(stream)
        if document["event"] == "worker.pass_failed"
    ]
    assert [document["consecutive_failures"] for document in failures] == [1, 2, 3]
    assert {document["exception_type"] for document in failures} == {"RuntimeError"}
    assert "transient database outage" not in stream.getvalue()


async def test_run_worker_stops_on_cancellation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation is an exit request, not a fault the loop absorbs."""

    @asynccontextmanager
    async def fake_lifespan(*_args: Any, **_kwargs: Any) -> AsyncIterator[WorkerRuntime]:
        yield _worker_runtime(settings)

    async def cancelled_pass(_runtime: WorkerRuntime, **_kwargs: Any) -> WorkerPassResult:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_module, "worker_lifespan", fake_lifespan)
    monkeypatch.setattr(worker_module, "run_worker_once", cancelled_pass)

    with pytest.raises(asyncio.CancelledError):
        await worker_module.run_worker(settings, stop_event=asyncio.Event())


async def test_run_worker_once_contains_one_failing_lane(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poisoned lane is counted and skipped; every other lane still drains."""

    stream = io.StringIO()
    configure_logging(stream=stream)

    async def failing_enrichment(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("stored overlay will not compile")

    async def drained_projections(*_args: Any, **_kwargs: Any) -> int:
        return 2

    monkeypatch.setattr(enrichment_module, "enrich_once", failing_enrichment)
    monkeypatch.setattr(worker_module, "_drain_cron_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(worker_module, "_drain_retention_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(worker_module, "_drain_projection_jobs", drained_projections)
    monkeypatch.setattr(worker_module, "_drain_derivation_jobs", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(worker_module, "_drain_backfill_jobs", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(
        artifact_uses_module, "purge_expired_artifact_uses", AsyncMock(return_value=0)
    )

    result = await run_worker_once(_worker_runtime(settings), worker_id="test-worker")

    assert result.lane_failures == 1
    # The lanes after the failure still ran, so the pass is not lost to one fault.
    assert result.projection_jobs == 4
    assert result.enrichment_selected == 0
    lanes = [
        document
        for document in _json_log_documents(stream)
        if document["event"] == "worker.lane_failed"
    ]
    assert [document["lane"] for document in lanes] == ["enrichment"]
    assert "stored overlay will not compile" not in stream.getvalue()
