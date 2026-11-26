"""Derivation jobs as traces: spans from the run audit, links between runs, and log lines."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from test_trace_task import _catalog, _derive, _failed_run, _jaeger

from memseek import tracing
from memseek.agent_runs import AgentRun
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.logging import JsonFormatter

COLLECTOR = "http://collector.test/v1/traces"


@pytest.fixture
def logged() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("memseek")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _spans(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        span
        for body in posts
        for resource in body["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def _links(span: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (link["traceId"], link["spanId"], _attributes(link)["memseek.link"])
        for link in span.get("links", ())
    ]


async def _run_id(pool: DatabasePool, workspace: str, derivation: str) -> UUID:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "select id from record where workspace = %s and collection = '_system' "
                "and content->>'processor' = %s",
                (workspace, derivation),
            )
        ).fetchone()
    assert row is not None
    return row["id"]


async def test_a_judged_failure_is_a_chain_of_linked_traces_and_logged_under_them(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logged: io.StringIO,
) -> None:
    traced = _catalog(settings, tmp_path).model_copy(update={"trace_export_url": COLLECTOR})
    posts: list[dict[str, Any]] = []
    agent_run_ids: list[str] = []
    original = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(200, json=_jaeger(agent_run_ids[0]))

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )

    workspace, run = await _failed_run(db_pool, traced)
    agent_run_ids.append(run["agent_run_id"])
    await _derive(db_pool, traced, workspace, "failed_runs")
    await tracing.drain_exports()

    assessment = await _run_id(db_pool, workspace, "renewal_assessment")
    judging = await _run_id(db_pool, workspace, "failed_runs")
    spans = _spans(posts)
    by_name = {span["name"]: span for span in spans}

    failed = by_name["derive renewal_assessment"]
    assert (failed["traceId"], failed["spanId"]) == (assessment.hex, assessment.hex[16:])
    assert failed["status"] == {"code": 2, "message": "provider"}
    assert _attributes(failed)["error.type"] == "provider"

    agent = by_name["invoke_agent renewal_analyst"]
    assert agent["traceId"] == run["trace_id"]
    assert _links(agent) == [(assessment.hex, assessment.hex[16:], "derivation")]

    judged = by_name["derive failed_runs"]
    assert judged["traceId"] == judging.hex
    assert judged["status"] == {"code": 1}
    assert _links(judged) == [(assessment.hex, assessment.hex[16:], "input")]
    assert _attributes(judged)["memseek.trace_reads"] == "1"

    episodes, review = by_name["task episodes"], by_name["task review"]
    assert {episodes["parentSpanId"], review["parentSpanId"]} == {judging.hex[16:]}
    assert _links(episodes) == [(run["trace_id"], "0" * 15 + "1", "trace_read")]
    assert int(episodes["startTimeUnixNano"]) <= int(review["startTimeUnixNano"])
    assert _attributes(episodes)["memseek.task.use"] == "trace"

    lines = [json.loads(line) for line in logged.getvalue().splitlines()]
    retried = next(line for line in lines if line["event"] == "job.retry_scheduled")
    completed = next(line for line in lines if line["event"] == "run.completed")
    assert retried["trace_id"] == assessment.hex
    assert (completed["trace_id"], completed["span_id"]) == (judging.hex, judging.hex[16:])


def test_a_derivation_span_without_task_start_times_lays_tasks_end_to_end() -> None:
    run_id = uuid4()
    started = datetime(2026, 11, 26, 19, 0, tzinfo=UTC)
    audit = {
        "processor": "summary",
        "status": "ok",
        "started_at": "2026-11-26T19:00:00Z",
        "completed_at": "2026-11-26T19:00:03Z",
        "task_trace": [
            {"task": "draft", "use": "llm", "ms": 1000},
            {"task": "check", "use": "judge", "ms": 500},
        ],
    }

    payload = tracing.derive_spans_payload(
        audit, run_id=run_id, workspace="w", entity="account:acme"
    )

    assert payload is not None
    draft, check = _spans([payload])[1:]
    assert int(draft["startTimeUnixNano"]) == int(started.timestamp() * 1e9)
    assert int(check["startTimeUnixNano"]) == int(
        (started + timedelta(seconds=1)).timestamp() * 1e9
    )


def test_an_agent_run_outside_a_derivation_links_nothing() -> None:
    now = datetime.now(UTC)
    run = AgentRun(
        agent_run_id=uuid4(),
        trace_id="a" * 32,
        root_span_id="b" * 16,
        workspace="w",
        entity="e",
        mode="invocation",
        origin={"kind": "invocation"},
        agent={"ref": "worker@1"},
        skills=(),
        content="metadata",
        started_at=now,
        ended_at=now,
        status="ok",
    )

    (span,) = _spans([tracing.root_span_payload(run)])

    assert "links" not in span
