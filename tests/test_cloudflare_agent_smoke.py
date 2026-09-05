"""Offline contract tests for the operator-run Cloudflare canary."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest

from memseek.cloudflare_smoke import (
    PROOF_PATH,
    CloudflareSmokePlan,
    SmokeFailure,
    build_cloudflare_smoke_plan,
    validate_cloudflare_smoke_result,
)
from memseek.computers import ComputerResult
from memseek.config import Settings


def _result(
    plan: CloudflareSmokePlan,
    phase: Literal["write", "read"],
    *,
    include_tool_result: bool = True,
) -> ComputerResult:
    events: list[dict[str, Any]] = [
        {
            "kind": "tool_call",
            "payload": {"tool_name": phase, "input": {"path": PROOF_PATH}},
        }
    ]
    if include_tool_result:
        events.append(
            {
                "kind": "tool_result",
                "payload": {"tool_name": phase, "output": {"ok": True}},
            }
        )
    return ComputerResult(
        value={
            "phase": phase,
            "proof_path": PROOF_PATH,
            "marker": plan.marker,
            "evidence_id": str(plan.evidence_id),
        },
        citation_ids=frozenset({plan.evidence_id}),
        steps=2,
        awaiting_input=False,
        receipt={
            "provider": "cloudflare",
            "backend": "worker-javascript",
            "session_key": plan.session_key,
            "task_id": f"task-{phase}",
            "output_sha256": "a" * 64,
            "resumed": False,
            "events": events,
            "files": [{"path": PROOF_PATH}] if phase == "write" else [],
        },
    )


def test_smoke_plan_proves_state_across_two_distinct_tasks(bare_settings: Settings) -> None:
    plan = build_cloudflare_smoke_plan(bare_settings, "offline-test")

    assert plan.write_request.session_key == plan.read_request.session_key
    assert plan.write_request.task_id != plan.read_request.task_id
    assert plan.write_request.computer.provider == "cloudflare"
    assert plan.write_request.computer.runtime.default == "worker-javascript"
    assert plan.model_target.startswith("workers_ai:@cf/")
    assert plan.write_request.citation_ids == plan.read_request.citation_ids

    # The second prompt cannot echo a supplied marker: it has to reopen durable
    # session state, and the receipt must prove that the read tool was called.
    assert plan.marker not in json.dumps(plan.read_request.input, sort_keys=True)
    assert plan.marker not in json.dumps(plan.read_request.context_files, sort_keys=True)


def test_smoke_result_requires_audited_write_and_read_round_trips(
    bare_settings: Settings,
) -> None:
    plan = build_cloudflare_smoke_plan(bare_settings, "receipt-test")

    assert (
        validate_cloudflare_smoke_result(_result(plan, "write"), plan, "write")["tool"] == "write"
    )
    assert validate_cloudflare_smoke_result(_result(plan, "read"), plan, "read")["tool"] == "read"

    with pytest.raises(SmokeFailure, match="no read tool result"):
        validate_cloudflare_smoke_result(
            _result(plan, "read", include_tool_result=False),
            plan,
            "read",
        )
