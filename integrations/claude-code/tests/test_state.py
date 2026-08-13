from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memseek_client import PluginConfig
from memseek_state import PendingSpool, SessionStore, hook_session_id


def config(tmp_path: Path, *, capture_mode: str = "conversation") -> PluginConfig:
    return PluginConfig(
        base_url="http://memseek.test",
        api_key="test-key",
        entity="project:test",
        skill_entity="skill:project:test:coding",
        context_artifact="agent_context",
        capture_mode=capture_mode,
        timeout=1,
        state_dir=tmp_path / "state",
        project_root=tmp_path,
    )


def test_session_allocations_are_ordered_and_retry_safe(tmp_path: Path) -> None:
    store = SessionStore(config(tmp_path), "claude:session-1")
    first = store.allocate_event(role="user", text="Remember UTC")
    retry = store.allocate_event(role="user", text="Remember UTC")
    second = store.allocate_event(role="assistant", text="I will remember it", turn_id="turn-2")
    second_retry = store.allocate_event(
        role="assistant", text="I will remember it", turn_id="turn-2"
    )

    assert first.ordinal > 0
    assert second.ordinal > first.ordinal
    assert retry.replay is True
    assert retry.dedupe_key == first.dedupe_key
    assert second_retry.replay is True
    assert second_retry.dedupe_key == second.dedupe_key


def test_long_native_session_id_is_bounded() -> None:
    session = hook_session_id({"session_id": "x" * 500})
    assert session.startswith("claude:sha256:")
    assert len(session) <= 128


class FakeClient:
    def __init__(self) -> None:
        self.record_batches: list[list[dict[str, Any]]] = []
        self.feedback_calls: list[tuple[str, dict[str, Any]]] = []

    def records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.record_batches.append(records)
        return {"records": []}

    def feedback(self, use_id: str, feedback: dict[str, Any]) -> dict[str, Any]:
        self.feedback_calls.append((use_id, feedback))
        return {"record_id": "signal-1"}


def test_spool_flushes_records_and_feedback(tmp_path: Path) -> None:
    spool = PendingSpool(config(tmp_path))
    spool.enqueue(
        kind="records",
        dedupe_key="record-1",
        payload={"records": [{"dedupe_key": "record-1"}]},
    )
    spool.enqueue(
        kind="feedback",
        dedupe_key="feedback-1",
        payload={"use_id": "use-1", "feedback": {"kind": "thumbs_up"}},
    )
    client = FakeClient()

    result = spool.flush(client)  # type: ignore[arg-type]

    assert result == {"sent": 2, "remaining": 0, "errors": []}
    assert client.record_batches == [[{"dedupe_key": "record-1"}]]
    assert client.feedback_calls == [("use-1", {"kind": "thumbs_up"})]


def test_spool_quarantines_invalid_envelope(tmp_path: Path) -> None:
    spool = PendingSpool(config(tmp_path))
    path = spool.enqueue(kind="unknown", dedupe_key="bad", payload={})
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "unknown"

    result = spool.flush(FakeClient())  # type: ignore[arg-type]

    assert result["sent"] == 0
    assert result["remaining"] == 0
    assert list(spool.failed_dir.glob("*.json"))
