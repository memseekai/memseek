"""Example orchestration contracts; these tests create no workspaces."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest


async def test_writeback_record_error_retains_the_validation_detail(monkeypatch):
    from memseek import records
    from memseek.invocations import InvocationError, _ingest_outbox_tx

    async def reject(*args, **kwargs):
        raise records.RecordValidationError("content_schema", "content: discount is not allowed")

    monkeypatch.setattr(records, "insert_records_tx", reject)
    citation = uuid4()
    declaration = SimpleNamespace(
        type="observations",
        path="/outbox/observations.jsonl",
        collection="observations@1",
        record_type="observation",
        review=False,
    )
    catalog = Mock(resolve_computer=lambda ref: SimpleNamespace(writeback=[declaration]))
    with pytest.raises(InvocationError, match=r"invalid writeback record:.*discount"):
        await _ingest_outbox_tx(
            Mock(),
            workspace="demo",
            invocation_id=uuid4(),
            entity="account:acme",
            computer_ref="workspace@1",
            visible_citations=frozenset({citation}),
            catalog=catalog,
            settings=Mock(),
            outbox=[
                {
                    "path": declaration.path,
                    "type": declaration.type,
                    "content": json.dumps(
                        {"text": "fact", "content": {"discount": 12}, "citations": [str(citation)]}
                    ),
                }
            ],
        )


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """No database is needed for example orchestration tests."""


@pytest.fixture
def support(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "examples"))
    return importlib.import_module("_computer_renewal_support")


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTER_RUNTIME_LOG_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("run_computer_demo")


async def test_silent_stream_times_out_and_closes(support):
    closed = False

    async def stream(*args, **kwargs):
        nonlocal closed
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            closed = True

    demo = support.Demo(SimpleNamespace(invocations=SimpleNamespace(stream=stream)))
    demo.invocation = "silent-run"
    with pytest.raises(TimeoutError, match="silent-run"):
        await demo.settle(timeout_s=0.02)
    assert closed


async def test_disconnected_stream_fallback_shares_deadline(support):
    async def stream(*args, **kwargs):
        await asyncio.sleep(0.03)
        raise httpx.ReadError("disconnected")
        yield {}

    async def retrieve(*args):
        await asyncio.sleep(0.03)
        return {"status": "succeeded"}

    demo = support.Demo(
        SimpleNamespace(invocations=SimpleNamespace(stream=stream, retrieve=retrieve))
    )
    demo.invocation = "fallback-run"
    with pytest.raises(TimeoutError, match="fallback-run"):
        await demo.settle(timeout_s=0.05)


async def test_disconnected_stream_can_finish_from_journal(support):
    async def stream(*args, **kwargs):
        raise httpx.ReadError("disconnected")
        yield {}

    async def retrieve(*args):
        return {"status": "succeeded"}

    async def events(*args, **kwargs):
        return {"events": []}

    demo = support.Demo(
        SimpleNamespace(
            invocations=SimpleNamespace(stream=stream, retrieve=retrieve, events=events)
        )
    )
    demo.invocation = "done"
    assert (await demo.settle(timeout_s=1))["status"] == "succeeded"


@pytest.mark.parametrize(
    ("cloudflare", "provider"), [(False, "cloudflare"), (True, "local-standin"), (False, "unknown")]
)
async def test_wrong_provider_is_rejected_before_workspace(
    support, monkeypatch, cloudflare, provider
):
    monkeypatch.setattr(
        support,
        "get_settings",
        lambda: SimpleNamespace(
            computer_runtime_url="http://localhost:8799", computer_runtime_token="test"
        ),
    )

    async def health(url):
        return {"provider": provider}

    monkeypatch.setattr(support, "runtime_health", health)
    with pytest.raises(SystemExit, match="Expected"):
        await support.main(["--cloudflare"] if cloudflare else [])


def test_occupied_port_has_actionable_error(launcher, monkeypatch):
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.bind.side_effect = OSError("in use")
    monkeypatch.setattr(launcher.socket, "socket", lambda: sock)
    monkeypatch.setattr(launcher, "health", lambda url: "cloudflare")
    with pytest.raises(RuntimeError, match=r"occupied.*cloudflare"):
        launcher.check_port(8799)


def test_startup_failure_and_timeout(launcher, monkeypatch):
    process = Mock(returncode=1)
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="exited"):
        launcher.wait_runtime(process, "http://test", "cloudflare")
    process.poll.return_value = None
    monkeypatch.setattr(launcher, "health", lambda url: None)
    with pytest.raises(RuntimeError, match="healthy"):
        launcher.wait_runtime(process, "http://test", "cloudflare", timeout=0)


def test_cleanup_signals_only_owned_process_group(launcher, monkeypatch):
    kill = Mock()
    monkeypatch.setattr(launcher.os, "killpg", kill)
    process = Mock(pid=123)
    process.wait.side_effect = [subprocess.TimeoutExpired("runtime", 5), 0]
    launcher.stop_owned(process)
    assert [call.args[0] for call in kill.call_args_list] == [123, 123]


def test_missing_remote_token_precedes_startup(launcher, monkeypatch):
    monkeypatch.setenv("COMPUTER_RUNTIME_URL", "https://runtime.test")
    monkeypatch.delenv("COMPUTER_RUNTIME_TOKEN", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/docker")
    run = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="COMPUTER_RUNTIME_TOKEN"):
        launcher.launch(SimpleNamespace(mode="cloudflare", scripted=True, advanced=False))
    assert all("up" not in call.args[0] for call in run.call_args_list)


@pytest.mark.parametrize("remote", [True, False])
def test_launcher_wires_urls_and_cleans_up_only_owned_runtime(launcher, monkeypatch, remote):
    monkeypatch.setenv("COMPUTER_RUNTIME_PORT", "18799")
    monkeypatch.setenv("MEMSEEK_API_KEY", "existing-key")
    monkeypatch.setenv("COMPUTER_RUNTIME_TOKEN", "test-secret")
    # A stale exported deployment URL must not block or redirect local mode.
    monkeypatch.setenv("COMPUTER_RUNTIME_URL", "https://runtime.test")
    monkeypatch.setenv("COMPUTER_RUNTIME_SECRET", "local-secret")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(launcher, "health", lambda _: "cloudflare")
    monkeypatch.setattr(launcher, "check_port", Mock())
    wait = Mock()
    monkeypatch.setattr(launcher, "wait_runtime", wait)
    process = Mock()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    stop = Mock()
    monkeypatch.setattr(launcher, "stop_owned", stop)
    calls = []

    def run(command, **kwargs):
        calls.append((command, dict(kwargs.get("env", {}))))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.launch(
        SimpleNamespace(mode="cloudflare" if remote else "local", scripted=True, advanced=False)
    )
    stack_env = next(env for command, env in calls if "up" in command)
    command, app_env = calls[-1]
    assert "--scripted" in command
    assert "MEMSEEK_API_KEY" not in app_env
    assert stack_env["COMPUTER_RUNTIME_TOKEN"] == app_env["COMPUTER_RUNTIME_TOKEN"]
    assert stack_env["COMPUTER_RUNTIME_URL"] == (
        "https://runtime.test" if remote else "http://host.docker.internal:18799"
    )
    assert app_env["COMPUTER_RUNTIME_URL"] == (
        "https://runtime.test" if remote else "http://127.0.0.1:18799"
    )
    assert app_env["COMPUTER_RUNTIME_TOKEN"] == ("test-secret" if remote else "local-secret")
    assert spawn.call_count == (0 if remote else 1)
    assert stop.call_count == (0 if remote else 1)


def test_failed_startup_still_cleans_owned_runtime(launcher, monkeypatch):
    monkeypatch.delenv("COMPUTER_RUNTIME_URL", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(launcher.subprocess, "run", Mock())
    monkeypatch.setattr(launcher, "check_port", Mock())
    process = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(launcher, "wait_runtime", Mock(side_effect=RuntimeError("startup failed")))
    stop = Mock()
    monkeypatch.setattr(launcher, "stop_owned", stop)
    with pytest.raises(RuntimeError, match="startup failed"):
        launcher.launch(SimpleNamespace(mode="local", scripted=True, advanced=False))
    stop.assert_called_once_with(process)


def test_retains_full_private_log_and_redacts_secret_on_failure(launcher, monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTER_RUNTIME_SECRET", "test-runtime-secret")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(launcher.subprocess, "run", Mock())
    monkeypatch.setattr(launcher, "check_port", Mock())
    monkeypatch.setattr(launcher, "stop_owned", Mock())
    monkeypatch.setattr(launcher, "wait_runtime", Mock(side_effect=RuntimeError("startup failed")))

    def spawn(command, **kwargs):
        kwargs["stdout"].write(b"FIRST EXCEPTION test-runtime-secret\n" + b"x" * 10000)
        kwargs["stdout"].flush()
        return Mock()

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="startup failed"):
        launcher.launch(SimpleNamespace(mode="local", scripted=True, advanced=False))
    logs = list(tmp_path.glob("*.log"))
    assert len(logs) == 1
    content = logs[0].read_text()
    assert content.startswith("FIRST EXCEPTION [redacted]")
    assert len(content) > 10000
    assert "test-runtime-secret" not in content
    assert logs[0].stat().st_mode & 0o777 == 0o600


def test_smoke_uses_existing_runtime_without_starting_database_stack(launcher, monkeypatch):
    monkeypatch.setenv("COMPUTER_RUNTIME_URL", "https://runtime.test")
    monkeypatch.setenv("COMPUTER_RUNTIME_TOKEN", "secret")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(launcher, "health", lambda _: "cloudflare")
    run = Mock()
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    launcher.launch(
        SimpleNamespace(
            mode="cloudflare", scripted=False, advanced=False, smoke=True, run_id="diagnostic-run"
        )
    )
    commands = [call.args[0] for call in run.call_args_list]
    assert all("up" not in command for command in commands)
    assert commands[-1][1:] == ["-m", "memseek.cloudflare_smoke", "--run-id", "diagnostic-run"]
    spawn.assert_not_called()


def test_runtime_error_detail_preserves_diagnostic_identifiers():
    from memseek.computers import _runtime_error_detail

    response = httpx.Response(
        422,
        json={
            "error": "model rejected request",
            "stage": "agent_execution",
            "request_id": "debug-123",
        },
    )
    assert (
        _runtime_error_detail(response)
        == ": model rejected request [stage=agent_execution, request_id=debug-123]"
    )
    response = httpx.Response(
        422, json={"error": "bad", "stage": {"secret": "do not print"}, "request_id": "\ninvalid"}
    )
    assert _runtime_error_detail(response) == ": bad"
