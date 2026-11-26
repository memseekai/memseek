"""Traced Agent runs: who receives the trace variables, and the root span the worker exports."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from site_scrape_fixture import ScrapeWorkspace, site_scrape_settings
from test_local_computer import _pi_harness

from memseek import computers, tracing
from memseek.agent_runs import AgentRun
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions.models import AgentDefinition
from memseek.trace_store import fetch_trace, render_episode

ENTITY = "site:news.ycombinator.com"
URL = "https://news.ycombinator.com/"
EXPORT_URL = "http://127.0.0.1:10428/insert/opentelemetry/v1/traces"


def _trace_agent(settings: Settings, content: str = "full") -> None:
    assert settings.catalog_file is not None
    agent = settings.catalog_file.parent / "scraping" / "agent.yaml"
    text = agent.read_text(encoding="utf-8")
    agent.write_text(
        text.replace(
            "    harness: echo\n", f"    harness: echo\n    trace: {{content: {content}}}\n"
        ),
        encoding="utf-8",
    )


async def _workspace(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path, *, traced: bool
) -> ScrapeWorkspace:
    scrape = site_scrape_settings(settings, tmp_path).model_copy(
        update={"trace_export_url": EXPORT_URL, "trace_export_token": "ingest-only"}
    )
    if traced:
        _trace_agent(scrape)
    workspace = ScrapeWorkspace(db_pool, scrape, f"trace-{uuid4().hex[:8]}")
    await workspace.create()
    return workspace


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    original = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


def _span(request: dict[str, Any]) -> dict[str, Any]:
    return request["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attributes(span: dict[str, Any]) -> dict[str, str]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


async def test_a_traced_agent_hands_its_harness_the_root_span_and_exports_it(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    exported: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHO_MODEL_KEY", "k-123")
    site = await _workspace(settings, db_pool, tmp_path, traced=True)
    await site.write_task(ENTITY, URL, "top 30 stories")

    invocation = await site.invoke(ENTITY, "Scrape the front page.")
    await tracing.drain_exports()

    assert invocation["status"] == "succeeded", invocation["error"]
    value = invocation["result"]["value"]
    assert value["trace_content"] == "full"
    assert value["trace_headers"] == "authorization=Bearer ingest-only"
    version, trace_id, span_id, flags = value["traceparent"].split("-")
    assert (version, len(trace_id), len(span_id), flags) == ("00", 32, 16, "01")

    assert len(exported) == 1
    assert exported[0]["url"] == EXPORT_URL
    assert exported[0]["authorization"] == "Bearer ingest-only"
    span = _span(exported[0])
    assert (span["traceId"], span["spanId"]) == (trace_id, span_id)
    assert span["name"] == "invoke_agent site_scraper"
    assert span["status"] == {"code": 1}
    attributes = _attributes(span)
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert attributes["memseek.workspace"] == site.workspace
    assert attributes["memseek.mode"] == "invocation"
    assert attributes["memseek.origin.invocation_id"] == str(invocation["invocation_id"])

    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "select type, depth, derived_from, content from record "
                "where workspace = %s and collection = 'agent_runs'",
                (site.workspace,),
            )
        ).fetchall()
    assert [(row["type"], row["depth"]) for row in rows] == [("run_ok", 0)]
    assert rows[0]["content"]["trace_id"] == trace_id
    assert rows[0]["content"]["agent_run_id"] == attributes["memseek.agent_run.id"]
    assert rows[0]["content"]["origin"]["invocation_id"] == str(invocation["invocation_id"])
    assert [str(item) for item in rows[0]["derived_from"]] == invocation["result"]["citation_ids"]


async def test_an_untraced_agent_exports_nothing_and_its_harness_sees_no_trace(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    exported: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHO_MODEL_KEY", "k-123")
    site = await _workspace(settings, db_pool, tmp_path, traced=False)
    await site.write_task(ENTITY, URL, "top 30 stories")

    invocation = await site.invoke(ENTITY, "Scrape the front page.")
    await tracing.drain_exports()

    assert invocation["status"] == "succeeded", invocation["error"]
    value = invocation["result"]["value"]
    assert (value["traceparent"], value["trace_content"], value["trace_headers"]) == (
        None,
        None,
        None,
    )
    assert exported == []


async def test_a_failed_traced_run_still_exports_its_root_span_with_the_error(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    exported: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHO_MODEL_KEY", "k-123")
    site = await _workspace(settings, db_pool, tmp_path, traced=True)
    await site.write_task(ENTITY, URL, "top 30 stories")

    invocation = await site.invoke(
        ENTITY,
        "Scrape the front page.",
        output_schema={"type": "object", "required": ["missing"]},
    )
    await tracing.drain_exports()

    assert invocation["status"] == "failed"
    span = _span(exported[0])
    assert span["status"] == {"code": 2, "message": "validation"}
    assert _attributes(span)["error.type"] == "validation"


def test_an_agent_without_trace_keeps_its_definition_hash() -> None:
    fields = {
        "name": "a",
        "version": 1,
        "model": "m",
        "instructions": "prompt@1",
        "computers": ["c@1"],
        "context_policy": "p@1",
    }
    plain = AgentDefinition.model_validate(fields)
    traced = AgentDefinition.model_validate({**fields, "trace": {"content": "metadata"}})

    assert "trace" not in plain.model_dump(mode="json")
    assert traced.model_dump(mode="json")["trace"] == {"content": "metadata"}
    with pytest.raises(ValueError, match="content"):
        AgentDefinition.model_validate({**fields, "trace": {}})


async def test_an_export_failure_is_logged_and_never_raised(
    bare_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    started = datetime.now(UTC)
    run = AgentRun(
        agent_run_id=uuid4(),
        trace_id="a" * 32,
        root_span_id="b" * 16,
        workspace="w",
        entity="e",
        mode="derivation",
        origin={"kind": "derivation", "derivation": "d"},
        agent={"ref": "a@1", "hash": "h", "harness": None, "model": "m"},
        skills=(),
        content="metadata",
        started_at=started,
        ended_at=started + timedelta(seconds=2),
        status="failed",
        error_kind="cancelled",
    )

    await tracing.export_root_span(
        run, bare_settings.model_copy(update={"trace_export_url": EXPORT_URL})
    )


async def test_a_run_cancelled_by_a_derivation_timeout_is_still_recorded_and_exported(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    exported: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = await _workspace(settings, db_pool, tmp_path, traced=True)

    class Stuck:
        async def execute(self, request: Any) -> Any:
            await asyncio.sleep(60)

    monkeypatch.setattr(computers, "_provider_for", lambda settings, name: Stuck())
    runs: list[AgentRun] = []

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await computers.execute_agent(
                settings=site.settings,
                catalog=site.catalog,
                workspace=site.workspace,
                entity=ENTITY,
                run_key="job-1",
                task_id="scrape",
                computer_ref="scrape_workspace@1",
                agent_ref="site_scraper@1",
                context_policy_ref="scrape_budget@1",
                input_value={},
                source_ids=frozenset(),
                citation_ids=frozenset(),
                output_path="/outbox/out.json",
                output_schema={"type": "object"},
                context_files={},
                max_output_bytes=1024,
                max_steps=4,
                origin={"kind": "derivation", "derivation": "run_task"},
                on_run=runs.append,
            )
    await tracing.drain_exports()

    assert [(run.status, run.error_kind) for run in runs] == [("failed", "cancelled")]
    assert runs[0].origin == {"kind": "derivation", "derivation": "run_task"}
    span = _span(exported[0])
    assert span["spanId"] == runs[0].root_span_id
    assert _attributes(span)["memseek.origin.derivation"] == "run_task"


TRACED_PI = """#!/bin/sh
env > ../.harness/pi-env
cat <<'JSONL'
{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"Scrape the front page."}]}}
{"type":"message_start","message":{"role":"assistant"}}
{"type":"message_end","message":{"role":"assistant","model":"gpt-6-luna","content":[{"type":"toolCall","id":"1","name":"bash","arguments":{"cmd":"curl x"}}],"usage":{"input":100,"output":20},"stopReason":"toolUse"}}
{"type":"tool_execution_start","toolCallId":"1","toolName":"bash","args":{"cmd":"curl x"}}
{"type":"tool_execution_end","toolCallId":"1","toolName":"bash","result":{"stderr":"curl: not found"},"isError":true}
{"type":"message_start","message":{"role":"assistant"}}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"{\\"value\\": {\\"items\\": []}, \\"citation_ids\\": []}"}],"usage":{"input":200,"output":40},"stopReason":"stop"}}
JSONL
"""


@pytest.fixture
def collector() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"auth": self.headers.get("authorization"), **json.loads(body)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/traces", received
    server.shutdown()


def _spans(received: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        span
        for request in received
        for resource in request["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="the pi harness needs node on PATH")
@pytest.mark.parametrize("content", ["full", "metadata"])
async def test_the_pi_harness_exports_model_and_tool_spans_under_the_root(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collector: tuple[str, list[dict[str, Any]]],
    content: str,
) -> None:
    url, received = collector
    _pi_harness(tmp_path, monkeypatch, TRACED_PI)
    scrape = site_scrape_settings(settings, tmp_path).model_copy(
        update={"trace_export_url": url, "trace_export_token": "ingest-only"}
    )
    _trace_agent(scrape, content)
    site = ScrapeWorkspace(db_pool, scrape, f"pi-trace-{content}")
    await site.create()
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, "Scrape the front page.")
    await tracing.drain_exports()

    assert run["status"] == "succeeded", run["error"]
    spans = _spans(received)
    root = next(span for span in spans if span["name"] == "invoke_agent site_scraper")
    children = [span for span in spans if span is not root]
    assert sorted(span["name"] for span in children) == [
        "chat gpt-6-luna",
        "chat gpt-6-luna",
        "execute_tool bash",
    ]
    assert {span["traceId"] for span in spans} == {root["traceId"]}
    assert {span["parentSpanId"] for span in children} == {root["spanId"]}
    assert {request["auth"] for request in received} == {"Bearer ingest-only"}
    tool = next(span for span in children if span["name"] == "execute_tool bash")
    assert tool["status"] == {"code": 2, "message": "tool_error"}
    tool_attributes = _attributes(tool)
    first_chat = min(
        (span for span in children if span["name"].startswith("chat")),
        key=lambda span: int(span["startTimeUnixNano"]),
    )
    chat_attributes = _attributes(first_chat)
    assert chat_attributes["gen_ai.usage.input_tokens"] == "100"
    if content == "full":
        assert json.loads(tool_attributes["gen_ai.tool.call.arguments"]) == {"cmd": "curl x"}
        assert "gen_ai.system_instructions" in chat_attributes
        assert "Scrape the front page." in chat_attributes["gen_ai.input.messages"]
    else:
        assert "gen_ai.tool.call.arguments" not in tool_attributes
        assert "gen_ai.output.messages" not in chat_attributes
    pi_env = (Path(run["result"]["receipt"]["root"]) / ".harness/pi-env").read_text()
    assert "TRACEPARENT" not in pi_env
    assert "OTEL_EXPORTER" not in pi_env


LIVE_TRACES = os.environ.get("MEMSEEK_LIVE_TRACES", "").rstrip("/")


@pytest.mark.skipif(not LIVE_TRACES, reason="set MEMSEEK_LIVE_TRACES to a running VictoriaTraces")
@pytest.mark.skipif(shutil.which("node") is None, reason="the pi harness needs node on PATH")
async def test_live_a_pi_run_can_be_read_back_from_victoriatraces(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pi_harness(tmp_path, monkeypatch, TRACED_PI)
    scrape = site_scrape_settings(settings, tmp_path).model_copy(
        update={
            "trace_export_url": f"{LIVE_TRACES}/insert/opentelemetry/v1/traces",
            "trace_store_url": f"{LIVE_TRACES}/select/jaeger",
        }
    )
    _trace_agent(scrape, "full")
    site = ScrapeWorkspace(db_pool, scrape, f"live-trace-{uuid4().hex[:8]}")
    await site.create()
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, "Scrape the front page.")
    await tracing.drain_exports()

    assert run["status"] == "succeeded", run["error"]
    async with db_pool.connection() as conn:
        (record,) = await (
            await conn.execute(
                "select content from record where workspace = %s and collection = 'agent_runs'",
                (site.workspace,),
            )
        ).fetchall()
    content = record["content"]
    trace = None
    for _ in range(30):
        fetched = await fetch_trace(scrape, content["trace_id"])
        if (
            fetched.trace is not None
            and fetched.trace.root(content["agent_run_id"])
            and len(fetched.trace.spans) == 4
        ):
            trace = fetched.trace
            break
        await asyncio.sleep(1)
    assert trace is not None, "the store never returned the run's four spans"
    episode = render_episode({"content": content}, trace, 4_000)
    print(
        f"\n--- episode ---\n{episode}\n--- timeline in VMUI ---\n{LIVE_TRACES}/select/vmui/#/trace?trace_id={content['trace_id']}"
    )

    assert episode.splitlines()[0].startswith("Agent site_scraper@")
    assert "Task: Scrape the front page." in episode
    steps = episode.split("Steps:\n")[1].splitlines()[:3]
    assert steps == [
        "1. model (in 100, out 20 tokens) -> calls bash",
        '2. tool bash failed: {"cmd":"curl x"} => {"stderr":"curl: not found"}',
        '3. model (in 200, out 40 tokens): {"value": {"items": []}, "citation_ids": []}',
    ]
