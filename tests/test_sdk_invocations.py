"""Invocation convenience API contracts, without a database or server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from memseek.sdk import InvocationBinding, MemseekClient


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """These transport tests deliberately do not need PostgreSQL."""


async def test_binding_matches_existing_payload_and_program_accepts_false() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"invocation_id": "run-1"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as transport:
        client = MemseekClient("http://test", "key", client=transport)
        binding = client.invocations.bind(
            computer="desk@1", agent="analyst@1", context_policy="budget@1"
        )
        run = await binding.start(entity="acme", prompt="Assess", idempotency_key="once")
        await client.invocations.start(
            entity="acme",
            computer="desk@1",
            executor={"kind": "agent", "agent": "analyst@1", "context_policy": "budget@1"},
            task={"kind": "answer", "prompt": "Assess"},
            idempotency_key="once",
        )
        assert run.id == "run-1"
        assert requests[0] == requests[1]
        await client.invocations.bind(computer="desk@1", program="extract@1").start(
            entity="acme", input=False
        )
        assert requests[2]["task"] == {"kind": "compute", "input": False}


@pytest.mark.parametrize(
    "refs",
    [
        {},
        {"agent": "a@1", "program": "p@1", "context_policy": "c@1"},
        {"agent": "a@1"},
        {"program": "p@1", "context_policy": "c@1"},
        {"program": "p"},
        {"agent": "a@1", "context_policy": "c"},
        {"program": ""},
    ],
)
def test_invalid_bindings(refs: dict[str, Any]) -> None:
    client = MemseekClient("http://test", "key")
    with pytest.raises(ValueError, match=r"requires|forbids|reference|finite"):
        client.invocations.bind(computer="desk@1", **refs)


async def test_invalid_start_shapes() -> None:
    client = MemseekClient("http://test", "key")
    agent = client.invocations.bind(computer="d@1", agent="a@1", context_policy="c@1")
    program = client.invocations.bind(computer="d@1", program="p@1")
    cases: list[tuple[InvocationBinding, dict[str, Any]]] = [
        (agent, {}),
        (agent, {"prompt": "x", "input": {}}),
        (program, {}),
        (program, {"input": None}),
        (program, {"input": {}, "prompt": "x"}),
    ]
    for binding, kwargs in cases:
        with pytest.raises(ValueError, match=r"requires|forbids|reference|finite"):
            await binding.start(entity="acme", **kwargs)


async def test_attach_repeated_pauses_and_handle_operations() -> None:
    requests = []
    turns = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal turns
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/turns"):
            turns += 1
        return httpx.Response(
            200, json={"status": "awaiting_input" if turns < 2 else "succeeded", "events": []}
        )

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as transport:
        client = MemseekClient("http://test", "key", client=transport)
        run = client.invocations.attach("existing")
        assert not requests
        for _ in range(2):
            assert (await run.wait())["status"] == "awaiting_input"
            await run.reply("answer")
        assert (await run.wait())["status"] == "succeeded"
        await run.events(after=4, limit=10)
        await run.cancel()
        assert requests[-2:] == [
            ("GET", "/invocations/existing/events"),
            ("POST", "/invocations/existing/cancel"),
        ]


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "context_exhausted"])
async def test_wait_returns_terminal_state(status: str) -> None:
    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": status})),
    ) as transport:
        client = MemseekClient("http://test", "key", client=transport)
        assert (await client.invocations.attach("id").wait())["status"] == status


@pytest.mark.parametrize("silent", [True, False])
async def test_wait_deadline_covers_requests_and_never_cancels(silent: bool) -> None:
    methods = []

    async def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if silent:
            await asyncio.Event().wait()
        return httpx.Response(200, json={"status": "queued"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as transport:
        client = MemseekClient("http://test", "key", client=transport)
        with pytest.raises(TimeoutError, match="durable-id"):
            await client.invocations.attach("durable-id").wait(timeout_s=0.02)
        assert methods == ["GET"]


@pytest.mark.parametrize("wait_budget", [0, -1, float("inf"), float("nan")])
async def test_wait_rejects_unbounded_timeout(wait_budget: float) -> None:
    client = MemseekClient("http://test", "key")
    with pytest.raises(ValueError, match=r"requires|forbids|reference|finite"):
        await client.invocations.attach("id").wait(timeout_s=wait_budget)
