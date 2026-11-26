"""The trace Task: read a run's trace from the store, render it, and judge it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from memseek.computers import FAKE_COMPUTER_PROVIDER, ComputerExecutionError, ComputerResult
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.derive.errors import DerivationError
from memseek.derive.runner import enqueue_derivation_job, process_derivation_job
from memseek.jobs import claim_job
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.trace_store import TraceStoreError, fetch_trace, normalize, render_episode

RENEWAL = Path(__file__).resolve().parents[1] / "examples" / "computer_renewal_catalog"
ENTITY = "account:acme"
STORE = "http://127.0.0.1:10428/select/jaeger"

FAILED_RUNS = """
name: failed_runs
sources:
  runs: {kind: changes, collections: [agent_runs], types: [run_failed], max_records: 5}
limits: {max_llm_calls: 0, max_judgments: 5, max_judgment_tokens: 100000, max_trace_reads: 5}
tasks:
  - id: episodes
    use: trace
    input: "{{runs.records}}"
    with: {max_chars: 4000}
  - id: review
    use: judge
    input: "{{episodes.items}}"
    with:
      model: jev
      questions:
        cause:
          type: choice
          instructions: "What made the run in `item.text` fail?"
          criteria:
            tool: "A tool call failed"
            model: "The model went wrong"
      records:
        text: "{{item.text}}"
        content: {kind: run_failure}
emit: {from: "{{review.records}}", collection: run_lessons, type: lesson, max_records: 5}
"""

RUN_LESSONS = """
collections:
  - name: run_lessons
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, kind]
      properties:
        text: {type: string}
        kind: {const: run_failure}
        judgment: {type: object}
      additionalProperties: false
    required_processors: []
    search_profile: pg_default
"""


def _span(
    span_id: str, name: str, tags: dict[str, Any], parent: str | None = None
) -> dict[str, Any]:
    return {
        "traceID": "a" * 32,
        "spanID": span_id,
        "operationName": name,
        "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else [],
        "startTime": 1_000 + int(span_id, 16),
        "duration": 10,
        "tags": [{"key": key, "type": "string", "value": value} for key, value in tags.items()],
    }


def _jaeger(agent_run_id: str) -> dict[str, Any]:
    root = "0" * 15 + "1"
    return {
        "data": [
            {
                "traceID": "a" * 32,
                "spans": [
                    _span(
                        root,
                        "invoke_agent renewal_analyst",
                        {
                            "memseek.agent_run.id": agent_run_id,
                            "otel.status_code": "ERROR",
                            "error.type": "provider",
                        },
                    ),
                    _span(
                        "0" * 15 + "2",
                        "chat m",
                        {
                            "gen_ai.operation.name": "chat",
                            "gen_ai.input.messages": json.dumps(
                                [
                                    {
                                        "role": "user",
                                        "parts": [{"type": "text", "content": "Assess Acme."}],
                                    }
                                ]
                            ),
                            "gen_ai.output.messages": json.dumps(
                                [
                                    {
                                        "role": "assistant",
                                        "parts": [{"type": "tool_call", "name": "bash"}],
                                    }
                                ]
                            ),
                        },
                        parent=root,
                    ),
                    _span(
                        "0" * 15 + "3",
                        "execute_tool bash",
                        {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "bash",
                            "gen_ai.tool.call.arguments": '{"cmd": "cat .agents/skills/renewal/SKILL.md"}',
                            "gen_ai.tool.call.result": "permission denied",
                            "error": "true",
                            "otel.status_description": "tool_error",
                            "error.type": "tool_error",
                        },
                        parent=root,
                    ),
                ],
            }
        ],
        "errors": None,
    }


def _store(monkeypatch: pytest.MonkeyPatch, respond: Any) -> list[str]:
    requested: list[str] = []
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return respond(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requested


def _settings(bare_settings: Settings, **update: Any) -> Settings:
    return bare_settings.model_copy(
        update={"trace_store_url": STORE, "trace_store_token": "query", **update}
    )


async def test_a_jaeger_trace_is_fetched_with_the_query_token(
    bare_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_jaeger("run-1"))

    requested = _store(monkeypatch, respond)

    fetched = await fetch_trace(_settings(bare_settings), "a" * 32)

    assert requested == [f"{STORE}/api/traces/{'a' * 32}"]
    assert seen == ["Bearer query"]
    assert fetched.trace is not None
    assert [span.name for span in fetched.trace.spans] == [
        "invoke_agent renewal_analyst",
        "chat m",
        "execute_tool bash",
    ]
    assert fetched.trace.root("run-1") is fetched.trace.spans[0]
    assert fetched.trace.root("another-run") is None


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, {"data": None, "errors": [{"code": 404, "msg": "trace not found"}]}, "not_found"),
        (
            404,
            {
                "data": [],
                "errors": [{"code": 404, "msg": "trace not found"}],
                "limit": 0,
                "offset": 0,
                "total": 0,
            },
            "not_found",
        ),
        (200, {"data": [], "errors": None}, "not_found"),
    ],
)
async def test_a_missing_trace_is_reported_not_raised(
    bare_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, Any],
    expected: str,
) -> None:
    _store(monkeypatch, lambda request: httpx.Response(status, json=body))

    fetched = await fetch_trace(_settings(bare_settings), "a" * 32)

    assert (fetched.trace, fetched.missing) == (None, expected)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(404, text="<html>no such route</html>"), "config"),
        (httpx.Response(200, json={"hello": "world"}), "config"),
        (httpx.Response(503, text="busy"), "transport"),
    ],
)
async def test_a_store_that_cannot_answer_raises_the_right_kind(
    bare_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    kind: str,
) -> None:
    _store(monkeypatch, lambda request: response)

    with pytest.raises(TraceStoreError) as failure:
        await fetch_trace(_settings(bare_settings), "a" * 32)

    assert failure.value.kind == kind


async def test_an_oversized_trace_is_too_large(
    bare_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(monkeypatch, lambda request: httpx.Response(200, json=_jaeger("run-1")))

    fetched = await fetch_trace(_settings(bare_settings, trace_store_max_bytes=100), "a" * 32)

    assert (fetched.trace, fetched.missing) == (None, "too_large")


def test_an_episode_renders_the_task_steps_failure_and_skills_read() -> None:
    body = json.dumps(_jaeger("run-1")).encode()
    trace = normalize(_jaeger("run-1")["data"], body)
    record = {
        "content": {
            "status": "failed",
            "error_kind": "provider",
            "agent": {"ref": "renewal_analyst@2", "harness": None, "model": "renewal_reasoner"},
            "origin": {"kind": "derivation", "derivation": "renewal_assessment"},
        }
    }

    episode = render_episode(record, trace, 4000)

    assert episode.splitlines() == [
        "Agent renewal_analyst@2 (harness built-in, model renewal_reasoner) ended failed "
        "(provider) in derivation renewal_assessment.",
        "Task: Assess Acme.",
        "Steps:",
        "1. model (in ?, out ? tokens) -> calls bash",
        '2. tool bash failed: {"cmd": "cat .agents/skills/renewal/SKILL.md"} => permission denied',
        "Final: no final message",
        "Skills read: renewal",
    ]


def test_a_long_episode_keeps_its_opening_and_its_end() -> None:
    spans = [
        _span(
            f"{index:016x}",
            "execute_tool bash",
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "bash",
                "gen_ai.tool.call.arguments": f"step {index}",
            },
        )
        for index in range(1, 200)
    ]
    trace = normalize([{"spans": spans}], b"{}")

    episode = render_episode({"content": {}}, trace, 1500)

    assert len(episode) <= 1500
    assert "1. tool bash ok: step 1" in episode
    assert "199. tool bash ok: step 199" in episode
    assert "steps omitted" in episode


def _catalog(settings: Settings, tmp_path: Path, derivation: str = FAILED_RUNS) -> Settings:
    root = tmp_path / "renewal"
    shutil.copytree(RENEWAL, root)
    agents = next((root / "agents").glob("*.yaml"))
    agents.write_text(
        agents.read_text().replace(
            "    toolset: renewal@1\n", "    toolset: renewal@1\n    trace: {content: full}\n"
        )
    )
    models = root / "conf" / "models.yaml"
    models.write_text(
        models.read_text()
        .replace(
            "providers:\n",
            "providers:\n  typesafe:\n    adapter: typesafe\n"
            "    base_url: https://api.typesafe.ai\n    api_key_env: TYPESAFE_API_KEY\n"
            "    json_capability: none\n",
        )
        .replace("aliases:\n", 'aliases:\n  jev:\n    targets: ["typesafe:jev-1.13.0"]\n')
    )
    (root / "collections" / "run_lessons.yaml").write_text(RUN_LESSONS)
    (root / "derivations" / "failed_runs.yaml").write_text(derivation)
    catalog = root / "catalog.yaml"
    catalog.write_text(
        catalog.read_text()
        .replace(
            "collections:\n", "collections:\n  run_lessons@1: collections/run_lessons.yaml\n", 1
        )
        .replace("derivations:\n", "derivations:\n  failed_runs: derivations/failed_runs.yaml\n", 1)
    )
    return settings.model_copy(
        update={"catalog_file": catalog, "trace_store_url": STORE, "trace_store_settle_s": 0}
    )


async def _derive(pool: DatabasePool, settings: Settings, workspace: str, name: str) -> None:
    catalog = load_definition_catalog(settings)
    await enqueue_derivation_job(pool, workspace=workspace, derivation=name, entity=ENTITY)
    claimed = await claim_job(
        pool,
        worker_id="trace-worker",
        kinds=("derive",),
        derivations=(name,),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    await process_derivation_job(pool, claimed=claimed, settings=settings, catalog=catalog)


async def _failed_run(pool: DatabasePool, settings: Settings) -> tuple[str, dict[str, Any]]:
    catalog = load_definition_catalog(settings)
    workspace = f"trace-task-{uuid4().hex[:8]}"
    async with pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)", (workspace, "e" * 64)
        )
    await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity=ENTITY,
                    collection="renewal_evidence",
                    type="evidence",
                    text="Uptime was 99.91%.",
                    content={"kind": "incident"},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )

    async def broken(request: Any) -> ComputerResult:
        raise ComputerExecutionError("provider", "the agent crashed")

    FAKE_COMPUTER_PROVIDER.clear()
    FAKE_COMPUTER_PROVIDER.register_agent("renewal_analyst@2", broken)
    try:
        with pytest.raises(DerivationError):
            await _derive(pool, settings, workspace, "renewal_assessment")
    finally:
        FAKE_COMPUTER_PROVIDER.clear()
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "select content from record where workspace = %s and collection = 'agent_runs'",
                (workspace,),
            )
        ).fetchone()
    assert row is not None
    return workspace, row["content"]


async def test_a_failed_run_becomes_a_judged_cited_episode(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traced = _catalog(settings, tmp_path)
    workspace, run = await _failed_run(db_pool, traced)
    requested = _store(
        monkeypatch, lambda request: httpx.Response(200, json=_jaeger(run["agent_run_id"]))
    )

    await _derive(db_pool, traced, workspace, "failed_runs")

    assert requested == [f"{STORE}/api/traces/{run['trace_id']}"]
    async with db_pool.connection() as conn:
        lessons = await (
            await conn.execute(
                "select content, derived_from, depth from record "
                "where workspace = %s and collection = 'run_lessons'",
                (workspace,),
            )
        ).fetchall()
        audit = await (
            await conn.execute(
                "select content from record where workspace = %s and collection = '_system' "
                "and content->>'processor' = 'failed_runs'",
                (workspace,),
            )
        ).fetchone()
    assert len(lessons) == 1
    lesson = lessons[0]["content"]
    assert lesson["kind"] == "run_failure"
    # The run record sits at depth 0, so evidence about a run is always depth 1
    # and the loop back into the skill never runs out of derivation depth.
    assert lessons[0]["depth"] == 1
    assert lesson["text"].startswith("Agent renewal_analyst@2")
    assert "2. tool bash failed" in lesson["text"]
    assert set(lesson["judgment"]) == {"cause"}
    assert audit is not None
    (receipt,) = audit["content"]["trace_reads"]
    assert receipt["outcome"] == "read"
    assert receipt["trace_id"] == run["trace_id"]
    assert receipt["spans"] == 3


async def test_a_trace_without_this_runs_root_span_is_skipped(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traced = _catalog(settings, tmp_path)
    workspace, _ = await _failed_run(db_pool, traced)
    _store(monkeypatch, lambda request: httpx.Response(200, json=_jaeger(str(uuid4()))))

    await _derive(db_pool, traced, workspace, "failed_runs")

    async with db_pool.connection() as conn:
        audit = await (
            await conn.execute(
                "select content from record where workspace = %s and collection = '_system' "
                "and content->>'processor' = 'failed_runs'",
                (workspace,),
            )
        ).fetchone()
        lessons = await (
            await conn.execute(
                "select 1 from record where workspace = %s and collection = 'run_lessons'",
                (workspace,),
            )
        ).fetchall()
    assert audit is not None
    assert [read["outcome"] for read in audit["content"]["trace_reads"]] == ["no_root_span"]
    assert lessons == []


async def test_a_trace_still_arriving_retries_the_run(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traced = _catalog(settings, tmp_path).model_copy(update={"trace_store_settle_s": 3600})
    workspace, _ = await _failed_run(db_pool, traced)
    _store(monkeypatch, lambda request: httpx.Response(404, json={"data": None, "errors": []}))

    with pytest.raises(DerivationError) as failure:
        await _derive(db_pool, traced, workspace, "failed_runs")

    assert failure.value.kind == "transport"


def test_a_trace_task_needs_a_budget_covering_its_source(
    settings: Settings, tmp_path: Path
) -> None:
    traced = _catalog(
        settings, tmp_path, FAILED_RUNS.replace("max_trace_reads: 5", "max_trace_reads: 2")
    )

    with pytest.raises(DefinitionError, match="max_trace_reads of at least its max_records"):
        load_definition_catalog(traced)


def test_the_example_catalog_closes_the_learning_loop(settings: Settings) -> None:
    example = RENEWAL.parent / "agent_trace_learning" / "catalog.yaml"
    catalog = load_definition_catalog(settings.model_copy(update={"catalog_file": example}))

    assert ("agent_runs", 1) in catalog.collections
    assert catalog.agents[("worker", 1)].trace is not None
    assert {task.use for task in catalog.derivations["failed_runs"].tasks} == {"trace", "judge"}
    assert catalog.derivations["skill"].emit.collection == "skills"
    assert catalog.artifacts[("learned_skill", 1)].candidate_processor == "skill"
