from __future__ import annotations

import json

import memseek_doctor
import pytest


def report(*, ready: bool = True) -> dict[str, object]:
    return {
        "ok": ready,
        "url": "https://memory.example.test",
        "api_key": "configured",
        "entity": "project:payments",
        "capture_mode": "conversation",
        "pending_writes": 0,
        "quarantined_writes": 0,
        "tools": sorted(memseek_doctor.EXPECTED_TOOLS),
    }


def test_human_status_is_short_and_actionable(capsys: pytest.CaptureFixture[str]) -> None:
    memseek_doctor._print_status(report(), as_json=False)

    output = capsys.readouterr().out
    assert output.startswith("Memseek is ready.")
    assert "Project memory: project:payments" in output
    assert "Memory tools: 7/7 available" in output
    assert "workspace-key" not in output


def test_json_status_remains_available(capsys: pytest.CaptureFixture[str]) -> None:
    memseek_doctor._print_status(report(), as_json=True)

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["api_key"] == "configured"
