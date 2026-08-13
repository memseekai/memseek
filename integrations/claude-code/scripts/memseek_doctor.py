#!/usr/bin/env python3
"""User-facing diagnostics, queue control, and feedback for the Memseek plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from memseek_client import MemseekClient, MemseekError, load_config
from memseek_state import PendingSpool

EXPECTED_TOOLS = {
    "answer",
    "context",
    "recall",
    "record",
    "remember",
    "replay_session",
    "standing_rules",
}
FEEDBACK_KINDS = {
    "correction",
    "evaluation",
    "exception",
    "task_failure",
    "task_success",
    "thumbs_down",
    "thumbs_up",
}


def _latest_session(config: Any) -> dict[str, Any] | None:
    directory = config.state_dir / "sessions"
    if not directory.is_dir():
        return None
    candidates = sorted(
        directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("entity") == config.entity
            and value.get("project_root") == str(config.project_root)
        ):
            return value
    return None


def _exact_session(config: Any, session_id: str) -> dict[str, Any] | None:
    key = hashlib.sha256(session_id.encode()).hexdigest()
    path = config.state_dir / "sessions" / f"{key}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        isinstance(value, dict)
        and value.get("session_id") == session_id
        and value.get("entity") == config.entity
        and value.get("project_root") == str(config.project_root)
    ):
        return value
    return None


def _print_status(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return
    ready = bool(report["ok"])
    print("Memseek is ready." if ready else "Memseek needs attention.")
    print(f"  Service: {report['url']}")
    print(f"  Project memory: {report['entity']}")
    print(f"  Conversation capture: {report['capture_mode']}")
    print(f"  Workspace key: {report['api_key']}")
    tools = report.get("tools", [])
    if isinstance(tools, list):
        print(f"  Memory tools: {len(tools)}/{len(EXPECTED_TOOLS)} available")
    print(
        f"  Retry queue: {report['pending_writes']} pending, "
        f"{report['quarantined_writes']} need inspection"
    )
    if report.get("error"):
        print(f"\nProblem: {report['error']}")
    if report.get("missing_recommended_tools"):
        missing = ", ".join(report["missing_recommended_tools"])
        print(f"\nMissing memory tools: {missing}")
    if report.get("hint"):
        print(f"Next step: {report['hint']}")


def status(cwd: Path, *, as_json: bool = False) -> int:
    config = load_config(cwd)
    client = MemseekClient(config)
    spool = PendingSpool(config)
    pending, failed = spool.counts()
    report: dict[str, Any] = {
        "ok": False,
        "url": config.base_url,
        "api_key": "configured" if config.api_key else "missing",
        "entity": config.entity,
        "skill_entity": config.skill_entity,
        "context_artifact": config.context_artifact,
        "capture_mode": config.capture_mode,
        "state_dir": str(config.state_dir),
        "pending_writes": pending,
        "quarantined_writes": failed,
    }
    if not config.api_key:
        report["error"] = (
            "No workspace key is configured. Reconfigure the plugin and enter the key "
            "provided by your Memseek administrator."
        )
        _print_status(report, as_json=as_json)
        return 1
    try:
        report["health"] = client.health()
        tools_payload = client.tools()
        tools = {
            item.get("name")
            for item in tools_payload.get("tools", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        report["package"] = tools_payload.get("package")
        report["tools"] = sorted(tools)
        missing = sorted(EXPECTED_TOOLS - tools)
        report["missing_recommended_tools"] = missing
        report["ok"] = bool(report["health"].get("ok")) and not missing
        if missing:
            report["hint"] = (
                "Publish and select examples/agent_memory_catalog, or provide an equivalent "
                "package exposing the listed MCP tools and agent_context artifact."
            )
    except MemseekError as exc:
        report["error"] = str(exc)
    _print_status(report, as_json=as_json)
    return 0 if report["ok"] else 1


def flush(cwd: Path) -> int:
    config = load_config(cwd)
    result = PendingSpool(config).flush(MemseekClient(config), limit=500)
    print(json.dumps(result, indent=2))
    return 0 if not result["errors"] else 1


def feedback(
    cwd: Path,
    *,
    kind: str,
    comment: str,
    label: str | None,
    session_id: str | None,
) -> int:
    config = load_config(cwd)
    session = _exact_session(config, session_id) if session_id else _latest_session(config)
    use_id = session.get("last_use_id") if session else None
    if not isinstance(use_id, str) or not use_id:
        print(
            "No bound artifact use was found for this project. Submit a prompt with Memseek "
            "connected, then retry feedback.",
            file=sys.stderr,
        )
        return 1
    event_hash = hashlib.sha256(f"{use_id}:{kind}:{label or ''}:{comment}".encode()).hexdigest()[
        :32
    ]
    body: dict[str, Any] = {
        "kind": kind,
        "source": "end_user",
        "comment": comment,
        "dedupe_key": f"claude-feedback:{event_hash}",
    }
    if label:
        body["label"] = label
    spool = PendingSpool(config)
    spool.enqueue(
        kind="feedback",
        dedupe_key=body["dedupe_key"],
        payload={"use_id": use_id, "feedback": body},
    )
    result = spool.flush(MemseekClient(config), limit=50)
    print(json.dumps({"artifact_use_id": use_id, **result}, indent=2))
    return 0 if not result["errors"] else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--cwd", type=Path, default=Path.cwd(), help="project directory")
    commands = root.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser(
        "status", help="check configuration, health, and memory tools"
    )
    status_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    commands.add_parser("flush", help="retry locally queued writes")
    feedback_parser = commands.add_parser(
        "feedback", help="attach outcome feedback to the most recent context artifact"
    )
    feedback_parser.add_argument("--kind", choices=sorted(FEEDBACK_KINDS), required=True)
    feedback_parser.add_argument("--comment", required=True)
    feedback_parser.add_argument("--label")
    feedback_parser.add_argument(
        "--session-id",
        help="exact conversation session from SessionStart; defaults to the latest project session",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "status":
            return status(arguments.cwd, as_json=arguments.json)
        if arguments.command == "flush":
            return flush(arguments.cwd)
        return feedback(
            arguments.cwd,
            kind=arguments.kind,
            comment=arguments.comment,
            label=arguments.label,
            session_id=arguments.session_id,
        )
    except MemseekError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
