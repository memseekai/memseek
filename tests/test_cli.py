"""Operational CLI contract tests."""

from __future__ import annotations

import json

import pytest

from memseek import cli, mcp_server
from memseek.config import Settings


def test_parser_exposes_every_operational_command() -> None:
    parser = cli.build_parser()
    action = next(action for action in parser._actions if action.dest == "command")
    assert action.choices is not None
    assert set(action.choices) == {
        "migrate",
        "create-workspace",
        "worker",
        "retry-job",
        "reindex",
        "mcp",
        # Definition evolution.
        "catalog-check",
        "catalog-prune",
        "migrate-collection-hashes",
        "backfill",
        "reembed",
        "rebind-cursor",
    }


def test_mcp_check_is_a_non_server_diagnostic_mode() -> None:
    args = cli.build_parser().parse_args(
        ["mcp", "--url", "http://memseek.test", "--api-key", "secret", "--check"]
    )
    assert args.command == "mcp"
    assert args.check is True


def test_mcp_check_prints_the_safe_inspection_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    async def fake_inspect_mcp(*, base_url: str, api_key: str) -> dict[str, object]:
        assert base_url == "http://memseek.test"
        assert api_key == "secret"
        return {"mcp_protocol": {"latest": "2026-07-28"}, "tools": []}

    monkeypatch.setattr(mcp_server, "inspect_mcp", fake_inspect_mcp)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["mcp", "--url", "http://memseek.test", "--api-key", "secret", "--check"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "mcp_protocol": {"latest": "2026-07-28"},
        "tools": [],
    }
    assert captured.err == ""


def test_migrate_prints_one_json_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    async def fake_apply(database_url: str) -> str:
        assert database_url == settings.database_url
        return "0001_initial"

    monkeypatch.setattr(cli, "apply_migrations", fake_apply)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["migrate"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"revision": "0001_initial"}
