"""examples/support_triage.py end to end, over HTTP, against a fake Jev."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from support_inbox import script_inbox

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.llm.fake import fake
from memseek.sdk import MemseekClient
from memseek.worker import run_worker_once, worker_lifespan

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "support_triage.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("support_triage_example", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _work_forever(settings: Settings) -> None:
    async with worker_lifespan(
        settings, catalog=load_definition_catalog(settings), pool=create_pool(settings)
    ) as runtime:
        while True:
            await run_worker_once(runtime, worker_id="support-triage")
            await asyncio.sleep(0.05)


async def test_demo_prints_urgency_signals_routes_and_escalations(
    settings: Settings,
    db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = await create_workspace(db_pool, "support-triage-demo")
    app = create_app(
        settings, catalog=load_definition_catalog(settings), pool=create_pool(settings)
    )
    fake.reset()
    script_inbox(monkeypatch)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            worker = asyncio.create_task(_work_forever(settings))
            try:
                await _script().demo(client, escalation_timeout_s=30)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("published support_triage@1.0.0 (")
    assert lines[1:] == [
        "wrote the account profile and 5 tickets for customer:northwind",
        "",
        "tickets",
        "  T1  urgency  8.8 · needs_reply 0.97 · topic billing (0.88)"
        " · frustration 1.9 · Charged twice this month",
        "  T2  urgency  1.6 · needs_reply 0.90 · topic technical (0.70)"
        " · frustration 0.1 · Exporting orders",
        "  T3  urgency  9.7 · needs_reply 0.99 · topic technical (0.95)"
        " · frustration 1.2 · API down",
        "  T4  urgency  3.4 · needs_reply 0.90 · topic account (0.60) · frustration 0.3 · SSO",
        "  T5  urgency  4.0 · needs_reply 0.60 · topic other (0.35) · frustration 0.8 · hmm",
        "",
        "route_tickets: 4 routed, 1 left for a human",
        "  T1  → billing   confidence 0.91 · covered by plan 0.85",
        "  T2  → technical confidence 0.80 · covered by plan 0.95",
        "  T3  → technical confidence 0.97 · covered by plan 0.90",
        "  T4  → sales     confidence 0.74 · covered by plan 0.10",
        "  T5  left for a human: no team reached confidence 0.6",
        "",
        "escalate: frustration sum 4.3, threshold 3",
        "  the trigger fired; 2 escalation notes",
        "  T1  [critical] Page the on-call lead and call the customer back today.",
        "  T3  [critical] Page the on-call lead and call the customer back today.",
    ]
