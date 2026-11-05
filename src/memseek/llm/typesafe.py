"""Async adapter for TypeSafe's System One judgment API (Jev models)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

from memseek.definitions.judgment import parse_answers, questions_json

from .registry import (
    Judgment,
    JudgmentRequest,
    LLMTransportError,
    ProviderEndpoint,
    RateLimited,
)


class _JudgmentResponseError(LLMTransportError):
    """A retryable response that does not answer the questions that were asked."""

    kind = "response"


class TypeSafeProvider:
    """One pooled HTTP client per endpoint, as in the OpenAI-compatible adapter."""

    NAME = "typesafe"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._http_clients: dict[tuple[str, str, int], httpx.AsyncClient] = {}
        self._client_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._client_lock:
            clients = list(self._http_clients.values())
            self._http_clients.clear()
        for client in clients:
            await client.aclose()

    async def _client(self, endpoint: ProviderEndpoint) -> httpx.AsyncClient:
        base_url = endpoint.base_url.rstrip("/") + "/"
        key = (base_url, endpoint.api_key, endpoint.max_concurrency)
        async with self._client_lock:
            client = self._http_clients.get(key)
            if client is None:
                headers = {"Content-Type": "application/json"}
                if endpoint.api_key:
                    headers["Authorization"] = f"Bearer {endpoint.api_key}"
                client = httpx.AsyncClient(
                    base_url=base_url,
                    headers=headers,
                    timeout=60.0,
                    transport=self._transport,
                    limits=httpx.Limits(
                        max_connections=endpoint.max_concurrency,
                        max_keepalive_connections=endpoint.max_concurrency,
                    ),
                )
                self._http_clients[key] = client
            return client

    async def judge(
        self, endpoint: ProviderEndpoint, model: str, request: JudgmentRequest
    ) -> Judgment:
        body = {
            "state": request.state,
            "model": model,
            "questions": questions_json(request.questions),
        }
        try:
            client = await self._client(endpoint)
            response = await client.post("v1/systemone", json=body)
        except httpx.TransportError as exc:
            raise LLMTransportError(f"TypeSafe transport failure: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited(
                "TypeSafe rate limited the request",
                _retry_after_s(response.headers.get("retry-after")),
            )
        if response.status_code >= 500:
            raise LLMTransportError(f"TypeSafe retryable HTTP status {response.status_code}")
        if response.status_code >= 400:
            raise ValueError(
                f"TypeSafe request rejected with HTTP {response.status_code}: "
                f"{_error_detail(response)}"
            )
        try:
            payload = response.json()
            resolved = payload["model"]
            answers = parse_answers(request.questions, payload["answers"])
            usage = payload.get("usage") or {}
        except (KeyError, TypeError, ValueError) as exc:
            raise _JudgmentResponseError(f"judgment response is invalid: {exc}") from exc
        if not isinstance(resolved, str) or not resolved:
            raise _JudgmentResponseError("judgment response names no model")
        return Judgment(
            answers=answers,
            model=resolved,
            input_tokens=_count(usage.get("input_tokens")),
            output_tokens=_count(usage.get("output_tokens")),
        )


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _retry_after_s(header: str | None) -> float | None:
    """Read a delta-seconds retry-after; an HTTP-date falls back to the default delay."""

    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _error_detail(response: httpx.Response) -> str:
    """Extract only bounded, non-secret fields from an error body."""

    try:
        payload = response.json()
    except ValueError:
        return "provider returned no structured error"
    if not isinstance(payload, dict):
        return "provider returned an invalid error shape"
    error = payload.get("error", payload)
    if isinstance(error, str):
        return error[:500]
    if not isinstance(error, dict):
        return "provider returned an invalid error shape"
    details = {
        key: value
        for key in ("message", "code", "type", "detail")
        if isinstance(value := error.get(key), (str, int, float, bool))
    }
    return str(details) if details else "provider returned no error details"


typesafe = TypeSafeProvider()
