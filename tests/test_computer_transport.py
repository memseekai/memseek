"""Offline HTTP pooling, signing, and ownership tests; no Computer runtime executes."""

import asyncio
import hashlib
import hmac
from typing import Any

import httpx
import pytest

from memseek.cloudflare_smoke import build_cloudflare_smoke_plan
from memseek.computers import (
    ComputerExecutionError,
    RemoteComputerProvider,
    computer_provider,
    computer_provider_lifespan,
    configure_remote_computer_provider,
)
from memseek.config import Settings


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "computer_runtime_url": "https://runtime.invalid",
            "computer_runtime_token": "test-secret",
        }
    )


async def test_remote_connections_are_reused_signed_and_closed_by_last_runtime(
    bare_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(bare_settings)
    request = build_cloudflare_smoke_plan(settings, "pooled").read_request
    requests: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        timestamp = request.headers["X-Memseek-Timestamp"]
        expected = hmac.new(
            b"test-secret", timestamp.encode() + b"." + request.content, hashlib.sha256
        ).hexdigest()
        assert request.headers["X-Memseek-Signature"] == expected
        return httpx.Response(200, json={"value": {"ok": True}, "receipt": {}})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        instance = original(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with computer_provider_lifespan(settings):
        provider = computer_provider("cloudflare")
        async with computer_provider_lifespan(settings):
            configure_remote_computer_provider(settings)
            assert computer_provider("cloudflare") is provider
            await asyncio.gather(*(provider.execute(request) for _ in range(4)))
        assert len(clients) == 1
        assert not clients[0].is_closed
        await provider.execute(request)
    assert len(requests) == 5
    assert clients[0].is_closed


async def test_remote_clients_are_separate_across_loops_and_configurations(
    bare_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(bare_settings)
    request = build_cloudflare_smoke_plan(settings, "loops").read_request
    clients: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        instance = original(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"value": {}, "receipt": {}})
            ),
            **kwargs,
        )
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with computer_provider_lifespan(settings):
        provider = computer_provider("cloudflare")
        assert isinstance(provider, RemoteComputerProvider)
        await provider.execute(request)

        async def other_loop() -> None:
            await provider.execute(request)
            await provider.aclose()

        await asyncio.to_thread(asyncio.run, other_loop())
        assert len(clients) == 2
        assert not clients[0].is_closed
        assert clients[1].is_closed
        other_settings = settings.model_copy(update={"computer_runtime_token": "other"})
        async with computer_provider_lifespan(other_settings):
            other = computer_provider("cloudflare")
            assert other is not provider
            await other.execute(request)
        assert clients[2].is_closed
        assert not clients[0].is_closed
    assert clients[0].is_closed


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(503, json={"error": "unavailable"}), "transport"),
        (httpx.Response(200, content=b"x" * 2000), "budget"),
        (httpx.Response(200, json={"invalid": True}), "validation"),
    ],
)
async def test_remote_failures_preserve_codes_and_caller_owned_client(
    bare_settings: Settings,
    response: httpx.Response,
    code: str,
) -> None:
    settings = _settings(bare_settings).model_copy(update={"computer_response_max_bytes": 1024})
    request = build_cloudflare_smoke_plan(settings, "failures").read_request
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        provider = RemoteComputerProvider(settings, client=client)
        with pytest.raises(ComputerExecutionError) as caught:
            await provider.execute(request)
        assert caught.value.code == code
        await provider.aclose()
        assert not client.is_closed
