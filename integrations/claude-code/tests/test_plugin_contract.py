from __future__ import annotations

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_mcp_and_hooks_form_one_plugin() -> None:
    manifest = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "memseek-memory"
    assert set(manifest["userConfig"]) == {
        "MEMSEEK_API_KEY",
        "MEMSEEK_CAPTURE_MODE",
        "MEMSEEK_URL",
    }
    assert manifest["userConfig"]["MEMSEEK_API_KEY"]["sensitive"] is True
    assert mcp["mcpServers"]["memseek"]["type"] == "http"
    assert mcp["mcpServers"]["memseek"]["url"] == "${user_config.MEMSEEK_URL}/mcp"
    assert set(hooks["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "PreCompact",
        "SessionEnd",
    }


def test_marketplace_points_at_this_plugin_and_memseekai() -> None:
    marketplace = json.loads(
        (PLUGIN_ROOT.parents[1] / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    plugin = marketplace["plugins"][0]
    assert marketplace["owner"]["url"] == "https://github.com/memseekai"
    assert plugin["name"] == "memseek-memory"
    assert plugin["source"] == "./integrations/claude-code"
    manifest = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert marketplace["metadata"]["version"] == manifest["version"]
    assert plugin["version"] == manifest["version"]


def test_all_hook_commands_use_safe_exec_form_and_existing_entrypoint() -> None:
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook for groups in hooks["hooks"].values() for group in groups for hook in group["hooks"]
    ]
    assert commands
    assert all(hook["command"] == "python3" for hook in commands)
    assert all(
        hook["args"][0] == "${CLAUDE_PLUGIN_ROOT}/scripts/memseek_hook.py" for hook in commands
    )
    assert (PLUGIN_ROOT / "scripts" / "memseek_hook.py").is_file()


def test_expected_skills_are_bundled() -> None:
    names = {path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")}
    assert names == {
        "memseek-explain",
        "memseek-feedback",
        "memseek-remember",
        "memseek-search",
        "memseek-status",
    }
