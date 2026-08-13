from __future__ import annotations

import json
from pathlib import Path

import pytest
from memseek_client import ConfigurationError, load_config, project_entity


def _clear_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MEMSEEK_API_KEY",
        "MEMSEEK_CAPTURE_MODE",
        "MEMSEEK_CONTEXT_ARTIFACT",
        "MEMSEEK_ENTITY",
        "MEMSEEK_PLUGIN_STATE_DIR",
        "MEMSEEK_SKILL_ENTITY",
        "MEMSEEK_TIMEOUT_SECONDS",
        "MEMSEEK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"CLAUDE_PLUGIN_OPTION_{name}", raising=False)


def test_project_entity_respects_explicit_override(tmp_path: Path) -> None:
    assert project_entity(tmp_path, "project:shared") == "project:shared"


def test_load_config_reads_non_secret_project_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    (tmp_path / ".memseek-project.json").write_text(
        json.dumps({"entity": "project:payments", "skill_entity": "skill:payments:review"}),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.entity == "project:payments"
    assert config.skill_entity == "skill:payments:review"
    assert config.capture_mode == "conversation"
    assert config.base_url == "http://127.0.0.1:8000"
    assert "redacted" in repr(config)


def test_environment_identity_wins_over_project_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    (tmp_path / ".memseek-project.json").write_text(
        json.dumps({"entity": "project:file"}), encoding="utf-8"
    )
    monkeypatch.setenv("MEMSEEK_ENTITY", "project:environment")
    assert load_config(tmp_path).entity == "project:environment"


def test_claude_plugin_answers_configure_hook_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMSEEK_URL", "https://memory.example.test")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMSEEK_API_KEY", "workspace-key")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMSEEK_CAPTURE_MODE", "explicit")

    config = load_config(tmp_path)

    assert config.base_url == "https://memory.example.test"
    assert config.api_key == "workspace-key"
    assert config.capture_mode == "explicit"


def test_claude_plugin_answers_win_over_shell_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    monkeypatch.setenv("MEMSEEK_URL", "https://shell.example.test")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMSEEK_URL", "https://plugin.example.test")

    assert load_config(tmp_path).base_url == "https://plugin.example.test"


def test_base_url_rejects_embedded_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    monkeypatch.setenv("MEMSEEK_URL", "https://token@example.test")
    with pytest.raises(ConfigurationError, match="cannot contain credentials"):
        load_config(tmp_path)


@pytest.mark.parametrize("value", ["everything", "", "CONVERSATION "])
def test_capture_mode_validation(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_config(monkeypatch)
    monkeypatch.setenv("MEMSEEK_CAPTURE_MODE", value)
    if value.strip().lower() == "conversation" or not value:
        assert load_config(tmp_path).capture_mode == "conversation"
    else:
        with pytest.raises(ConfigurationError, match="MEMSEEK_CAPTURE_MODE"):
            load_config(tmp_path)
