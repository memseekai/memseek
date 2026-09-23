"""Writeback tools: a harness validates records when the agent writes them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from memseek.harnesses.writeback import WRITEBACK_SCHEMAS_PATH, writeback_tools

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CITATION = "3d7b39bd-b7c0-4175-915f-d2a4888284cc"
GOOD = {
    "text": "[browser-harness/extraction] Stories are tr.athing rows.",
    "content": {
        "pack": "browser-harness",
        "kind": "extraction",
        "detail": "Stories are tr.athing rows.",
        "helper_code": None,
    },
    "citations": [CITATION],
}
# The shape a live run wrote by hand before these tools existed.
BAD = {"learning": "HN rows are tr.athing", "site": "news.ycombinator.com"}


def _learnings_schema() -> dict[str, Any]:
    catalog = yaml.safe_load(
        (REPOSITORY_ROOT / "examples/site_scrape_catalog/collections/scraping.yaml").read_text()
    )
    return next(c for c in catalog["collections"] if c["name"] == "skill_learnings")["schema"]


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    schemas = json.dumps(
        [
            {
                "collection": "skill_learnings@1",
                "mode": "event",
                "path": "/outbox/learnings.jsonl",
                "schema": _learnings_schema(),
            }
        ]
    )
    (root / ".memseek").mkdir(parents=True)
    (root / ".memseek/writeback-schemas.json").write_text(schemas)
    (root / "outbox").mkdir()
    (root / "workspace").mkdir()
    tools = writeback_tools(
        [{"path": "/outbox/learnings.jsonl", "type": "observations"}],
        {WRITEBACK_SCHEMAS_PATH: schemas},
        [CITATION],
    )
    (root / ".harness").mkdir()
    (root / ".harness/input.json").write_text(
        json.dumps(
            {
                "task": "Scrape it.",
                "system_prompt": "Record what you learn with the tool.",
                "output_schema": {"type": "object"},
                "model": {"provider": "stub", "model": "stub-model", "params": {}},
                "limits": {"max_steps": 10, "max_wall_s": 60},
                "tools": [],
                "skills": [],
                "learning": "read_write",
                "citation_ids": [CITATION],
                "writeback_tools": [tool.model_dump(mode="json") for tool in tools],
                "writeback_command": [
                    sys.executable,
                    "-m",
                    "memseek.harnesses.writeback",
                    str(root),
                ],
            }
        )
    )
    return root


def _call(root: Path, arguments: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "memseek.harnesses.writeback", str(root), "record_skill_learnings"],
        input=json.dumps(arguments),
        capture_output=True,
        text=True,
        check=False,
    )


def test_each_observations_writeback_becomes_a_tool_bound_to_the_runs_citations() -> None:
    schemas = json.dumps(
        [
            {
                "collection": "skill_learnings@1",
                "path": "/outbox/learnings.jsonl",
                "schema": _learnings_schema(),
            }
        ]
    )

    tools = writeback_tools(
        [
            {"path": "/outbox/learnings.jsonl", "type": "observations"},
            {"path": "/outbox/final-result.json", "type": "final_result"},
        ],
        {WRITEBACK_SCHEMAS_PATH: schemas},
        [CITATION],
    )

    assert [(tool.name, tool.path) for tool in tools] == [
        ("record_skill_learnings", "/outbox/learnings.jsonl")
    ]
    entry = tools[0].input_schema["properties"]["records"]["items"]
    assert entry["required"] == ["text", "content", "citations"]
    assert entry["properties"]["citations"]["items"] == {"type": "string", "enum": [CITATION]}
    assert entry["properties"]["content"]["required"] == ["pack", "kind", "detail"]


def test_a_rejected_call_writes_nothing_and_says_what_to_fix(tmp_path: Path) -> None:
    root = _root(tmp_path)

    rejected = _call(root, {"records": [BAD]})

    assert rejected.returncode == 1
    assert rejected.stdout == (
        "Nothing was written. Fix these and call again:\n"
        "- records[0] unknown keys ['learning', 'site']; an entry has only text, content, and "
        "citations\n"
        "- records[0] citations must be a non-empty list of authorized UUIDs\n"
        "- records[0] text must be a string\n"
    )
    assert not (root / "outbox/learnings.jsonl").exists()


def test_an_accepted_call_appends_the_record_without_null_fields(tmp_path: Path) -> None:
    root = _root(tmp_path)

    accepted = _call(root, {"records": [GOOD]})

    assert (accepted.returncode, accepted.stdout) == (0, "Recorded 1 entry.\n")
    assert [
        json.loads(line) for line in (root / "outbox/learnings.jsonl").read_text().splitlines()
    ] == [
        {
            "text": "[browser-harness/extraction] Stories are tr.athing rows.",
            "citations": [CITATION],
            "content": {
                "pack": "browser-harness",
                "kind": "extraction",
                "detail": "Stories are tr.athing rows.",
            },
        }
    ]


def test_an_unauthorized_citation_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    stranger = "00000000-0000-4000-8000-000000000000"

    rejected = _call(root, {"records": [{**GOOD, "citations": [stranger]}]})

    assert rejected.returncode == 1
    assert f"citation '{stranger}' is not authorized; use one of ['{CITATION}']" in rejected.stdout


class _StubModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that scripts a bad call, a fix, and an answer."""

    script: ClassVar[list[tuple[dict[str, Any], str]]] = []
    seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        turn = len(self.seen)
        self.seen.append(body)
        delta, finish = self.script[turn]
        chunks = [
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": "stub-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": "stub-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _tool_call(turn: int, records: list[Any]) -> tuple[dict[str, Any], str]:
    call = {
        "index": 0,
        "id": f"call{turn}",
        "type": "function",
        "function": {
            "name": "record_skill_learnings",
            "arguments": json.dumps({"records": records}),
        },
    }
    return {"role": "assistant", "tool_calls": [call]}, "tool_calls"


@pytest.mark.skipif(
    shutil.which("pi") is None or shutil.which("node") is None,
    reason="needs pi and node on PATH",
)
def test_real_pi_offers_the_tool_and_the_agent_recovers_from_a_rejection(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    envelope = {"value": {"items": []}, "citation_ids": [CITATION], "awaiting_input": False}
    _StubModel.script = [
        _tool_call(0, [BAD]),
        _tool_call(1, [GOOD]),
        ({"role": "assistant", "content": json.dumps(envelope)}, "stop"),
    ]
    _StubModel.seen = []
    server = HTTPServer(("127.0.0.1", 0), _StubModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "stub": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "apiKey": "stub",
                        "models": [{"id": "stub-model"}],
                    }
                }
            }
        )
    )
    try:
        completed = subprocess.run(
            ["node", str(REPOSITORY_ROOT / "harnesses/pi/run.mjs")],
            cwd=root,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(tmp_path),
                "PI_CODING_AGENT_DIR": str(agent_dir),
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        server.shutdown()

    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout.splitlines()[-1])
    assert output["value"] == {"items": []}
    offered = [tool["function"]["name"] for tool in _StubModel.seen[0]["tools"]]
    assert "record_skill_learnings" in offered
    rejection = _StubModel.seen[1]["messages"][-1]
    assert rejection["role"] == "tool"
    assert "records.0" in json.dumps(rejection["content"])
    assert _StubModel.seen[2]["messages"][-1]["content"] in (
        "Recorded 1 entry.",
        [{"type": "text", "text": "Recorded 1 entry."}],
    )
    assert [
        json.loads(line)["text"]
        for line in (root / "outbox/learnings.jsonl").read_text().splitlines()
    ] == ["[browser-harness/extraction] Stories are tr.athing rows."]
