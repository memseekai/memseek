"""Async adapter for OpenAI-compatible completion and embedding APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Self

import httpx

from .registry import (
    JSON_OBJECT_OUTPUT,
    TEXT_OUTPUT,
    Completion,
    CompletionOutput,
    EmbeddingResult,
    LLMTransportError,
    ProviderEndpoint,
    materialize_json_schema,
)


class _ProviderResponseError(LLMTransportError):
    """A retryable malformed response from the remote provider."""

    kind = "response"


class OpenAICompatibleProvider:
    """Protocol adapter owning one bounded, reusable HTTP client per endpoint.

    A deployment may now call several endpoints through this one adapter — an
    embedding service and a chat service, say — so clients are pooled per
    endpoint rather than singly.  Sharing one client across base URLs would send
    one endpoint's `Authorization` header to another.
    """

    NAME = "openai_compat"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._http_clients: dict[tuple[str, str, int], httpx.AsyncClient] = {}
        self._client_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        await self.aclose()

    async def aclose(self) -> None:
        """Close every owned connection pool; later calls recreate them lazily."""

        async with self._client_lock:
            clients = list(self._http_clients.values())
            self._http_clients.clear()
        for client in clients:
            await client.aclose()

    @staticmethod
    def _client_key(endpoint: ProviderEndpoint) -> tuple[str, str, int]:
        return (
            endpoint.base_url.rstrip("/") + "/",
            endpoint.api_key,
            endpoint.max_concurrency,
        )

    async def _client(self, endpoint: ProviderEndpoint) -> httpx.AsyncClient:
        key = self._client_key(endpoint)
        async with self._client_lock:
            client = self._http_clients.get(key)
            if client is None:
                client = self._build_client(endpoint)
                self._http_clients[key] = client
            return client

    def _build_client(self, endpoint: ProviderEndpoint) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        return httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=60.0,
            transport=self._transport,
            limits=httpx.Limits(
                max_connections=endpoint.max_concurrency,
                max_keepalive_connections=endpoint.max_concurrency,
            ),
        )

    async def complete(
        self,
        endpoint: ProviderEndpoint,
        model: str,
        system: str,
        prompt: str,
        *,
        params: dict[str, object],
        output: CompletionOutput,
        max_output_tokens: int,
    ) -> Completion:
        output = _effective_output(endpoint, output)
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            endpoint.token_limit_field: max_output_tokens,
            **params,
        }
        if output.mode == "json_schema":
            assert output.name is not None
            assert output.schema is not None
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output.name,
                    "strict": endpoint.json_schema_strict,
                    "schema": materialize_json_schema(output.schema),
                },
            }
        elif output.mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        response = await self._post(endpoint, "chat/completions", body)
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise _ProviderResponseError("completion response has an invalid shape") from exc
        if not isinstance(text, str):
            raise _ProviderResponseError("completion content must be a string")
        return Completion(
            text,
            prompt_tokens if isinstance(prompt_tokens, int) else None,
            completion_tokens if isinstance(completion_tokens, int) else None,
            output.mode,
        )

    async def embed(
        self,
        endpoint: ProviderEndpoint,
        model: str,
        texts: list[str],
        *,
        dimensions: int,
        params: Mapping[str, Any],
    ) -> EmbeddingResult:
        # ``dimensions`` is the declared contract the runtime validates the
        # response against; it is not sent, because endpoints disagree on
        # whether it is even accepted.  Declare it in ``embedding.params`` when
        # your endpoint wants to be told.
        del dimensions
        body: dict[str, Any] = {"model": model, "input": texts, **params}
        response = await self._post(endpoint, "embeddings", body)
        try:
            payload = response.json()
            data = payload["data"]
            indices = [item["index"] for item in data]
            if any(isinstance(index, bool) or not isinstance(index, int) for index in indices):
                raise TypeError
            if sorted(indices) != list(range(len(texts))):
                raise ValueError
            data = sorted(data, key=lambda item: item["index"])
            vectors = tuple(tuple(float(value) for value in item["embedding"]) for item in data)
            usage = payload.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens")
        except (KeyError, TypeError, ValueError) as exc:
            raise _ProviderResponseError("embedding response has an invalid shape") from exc
        return EmbeddingResult(
            vectors,
            prompt_tokens if isinstance(prompt_tokens, int) else None,
        )

    async def _post(
        self, endpoint: ProviderEndpoint, path: str, body: dict[str, Any]
    ) -> httpx.Response:
        try:
            client = await self._client(endpoint)
            response = await client.post(path, json=body)
        except httpx.TransportError as exc:
            raise LLMTransportError(f"OpenAI-compatible transport failure: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise LLMTransportError(
                f"OpenAI-compatible retryable HTTP status {response.status_code}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _provider_error_detail(response)
            raise ValueError(
                f"OpenAI-compatible request rejected with HTTP {response.status_code}: {detail}"
            ) from exc
        return response


def _provider_error_detail(response: httpx.Response) -> str:
    """Extract only bounded, non-secret fields from a provider error body."""

    try:
        payload = response.json()
    except ValueError:
        return "provider returned no structured error"
    if not isinstance(payload, dict):
        return "provider returned an invalid error shape"
    error = payload.get("error")
    if not isinstance(error, dict):
        return "provider returned an invalid error shape"
    details = {
        key: value
        for key in ("message", "param", "code", "type")
        if isinstance(value := error.get(key), (str, int, float, bool))
    }
    return str(details) if details else "provider returned no error details"


def _effective_output(endpoint: ProviderEndpoint, requested: CompletionOutput) -> CompletionOutput:
    """Select the strongest output this endpoint is declared to support."""

    capability = endpoint.json_capability
    if requested.mode == "text" or capability == "none":
        return TEXT_OUTPUT
    if requested.mode == "json_schema" and capability == "json_schema":
        return requested
    return JSON_OBJECT_OUTPUT


openai_compat = OpenAICompatibleProvider()
