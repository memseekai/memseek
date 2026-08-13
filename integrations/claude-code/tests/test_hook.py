from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import memseek_hook
import pytest
from memseek_client import PluginConfig
from memseek_state import SessionStore


def config(
    tmp_path: Path, *, capture_mode: str = "conversation", api_key: str = "test-key"
) -> PluginConfig:
    return PluginConfig(
        base_url="http://memseek.test",
        api_key=api_key,
        entity="project:test",
        skill_entity="skill:project:test:coding",
        context_artifact="agent_context",
        capture_mode=capture_mode,
        timeout=1,
        state_dir=tmp_path / "state",
        project_root=tmp_path,
    )


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, plugin_config: PluginConfig) -> None:
        self.config = plugin_config
        self.record_batches: list[list[dict[str, Any]]] = []
        FakeClient.instances.append(self)

    def health(self) -> dict[str, Any]:
        return {"ok": True, "db": True}

    def bind_context(self, *, task: str) -> dict[str, Any]:
        return {
            "id": "11111111-1111-1111-1111-111111111111",
            "content": f"context for {task}",
            "render": {"truncated": False},
        }

    def records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.record_batches.append(records)
        return {"records": []}

    def feedback(self, use_id: str, feedback: dict[str, Any]) -> dict[str, Any]:
        return {}


def install_fakes(monkeypatch: pytest.MonkeyPatch, plugin_config: PluginConfig) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(memseek_hook, "load_config", lambda cwd: plugin_config)
    monkeypatch.setattr(memseek_hook, "MemseekClient", FakeClient)


def test_prompt_context_binds_use_and_emits_claude_hook_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin_config = config(tmp_path)
    install_fakes(monkeypatch, plugin_config)

    memseek_hook.prompt_context(
        {"session_id": "session-1", "cwd": str(tmp_path), "prompt": "fix the cache"}
    )

    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "UserPromptSubmit"
    assert 'entity="project:test"' in specific["additionalContext"]
    assert "context for fix the cache" in specific["additionalContext"]
    state = SessionStore(plugin_config, "claude:session-1").load()
    assert state["last_use_id"] == "11111111-1111-1111-1111-111111111111"
    assert state["last_task"] == "fix the cache"


def test_capture_user_builds_declared_messages_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_config = config(tmp_path)
    install_fakes(monkeypatch, plugin_config)

    memseek_hook.capture_user({"session_id": "session-1", "turn_id": "turn-1", "prompt": "Use UTC"})

    batch = FakeClient.instances[-1].record_batches[0]
    record = batch[0]
    assert record["entity"] == "project:test"
    assert record["collection"] == "messages"
    assert record["collection_version"] == 1
    assert record["content"]["text"] == "Use UTC"
    assert record["content"]["role"] == "user"
    assert record["content"]["session_id"] == "claude:session-1"
    assert record["content"]["ordinal"] > 0
    assert record["dedupe_key"].endswith(":user:turn-1")


def test_explicit_mode_skips_automatic_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fakes(monkeypatch, config(tmp_path, capture_mode="explicit"))
    memseek_hook.capture_assistant({"session_id": "session-1", "last_assistant_message": "Done"})
    assert FakeClient.instances[-1].record_batches == []


def test_missing_key_does_not_spool_conversation_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_config = config(tmp_path, api_key="")
    install_fakes(monkeypatch, plugin_config)
    memseek_hook.capture_user({"session_id": "session-1", "prompt": "private draft"})

    assert FakeClient.instances[-1].record_batches == []
    assert not (plugin_config.state_dir / "pending").exists()
