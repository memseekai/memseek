"""Static guardrails for the optional Pydantic AI MCP example."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = ROOT / "examples/pydantic_ai_mcp_showcase.py"


def test_pydantic_ai_showcase_compiles_without_importing_optional_dependency() -> None:
    compile(SHOWCASE.read_text(), str(SHOWCASE), "exec")


def test_pydantic_ai_client_and_mcp_v2_server_use_isolated_environments() -> None:
    source = SHOWCASE.read_text()
    assert "uv run --no-project" in source
    assert '["run", "--project", str(project_root), "memseek", "mcp"]' in source
