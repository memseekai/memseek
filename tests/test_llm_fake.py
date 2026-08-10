from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Literal, cast

import httpx
import pytest

from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.llm.fake import FakeLLMProvider, FakeProviderFailure, fake
from memseek.llm.openai_compat import OpenAICompatibleProvider
from memseek.llm.registry import (
    JSON_OBJECT_OUTPUT,
    TEXT_OUTPUT,
    Completion,
    CompletionOutput,
    EmbeddingResult,
    LLMTransportError,
    ProviderEndpoint,
    provider_descriptor,
)
from memseek.llm.runtime import ModelAttemptsExhausted, complete, embed


def endpoint(
    *,
    adapter: str = "openai_compat",
    base_url: str = "https://provider.example/v1",
    api_key: str = "",
    json_capability: Literal["json_schema", "json_object", "none"] = "json_schema",
    json_schema_strict: bool = False,
    token_limit_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens",
) -> ProviderEndpoint:
    """Build the resolved connection an adapter is handed at call time."""

    return ProviderEndpoint(
        provider="testing",
        adapter=adapter,
        base_url=base_url,
        api_key=api_key,
        json_capability=json_capability,
        json_schema_strict=json_schema_strict,
        token_limit_field=token_limit_field,
        max_concurrency=4,
    )


async def test_fake_embeddings_are_deterministic_normalized_and_exact_dimension() -> None:
    provider = FakeLLMProvider()

    first = await provider.embed(
        endpoint(adapter="fake"),
        "embedding-model",
        ["alpha", "alpha", "beta"],
        dimensions=1_536,
        params={},
    )
    second = await provider.embed(
        endpoint(adapter="fake"), "embedding-model", ["alpha"], dimensions=1_536, params={}
    )

    assert len(first.vectors) == 3
    assert len(first.vectors[0]) == 1_536
    assert first.vectors[0] == first.vectors[1] == second.vectors[0]
    assert first.vectors[0] != first.vectors[2]
    assert math.isclose(math.sqrt(sum(value**2 for value in first.vectors[0])), 1.0)
    assert first.prompt_tokens is not None


def test_provider_descriptors_advertise_json_capabilities() -> None:
    assert provider_descriptor("openai_compat").json_capabilities == {
        "json_object",
        "json_schema",
    }
    assert provider_descriptor("fake").json_capabilities == {"json_object", "json_schema"}


async def test_fake_completion_fifo_failure_and_scorer_markers() -> None:
    provider = FakeLLMProvider()
    provider.fail_next(1)
    provider.enqueue(Completion('{"queued":true}', prompt_tokens=7, completion_tokens=2))

    with pytest.raises(FakeProviderFailure, match="transport"):
        await provider.complete(
            endpoint(adapter="fake"),
            "model",
            "system",
            "prompt",
            params={},
            output=JSON_OBJECT_OUTPUT,
            max_output_tokens=20,
        )
    queued = await provider.complete(
        endpoint(adapter="fake"),
        "model",
        "system",
        "prompt",
        params={},
        output=JSON_OBJECT_OUTPUT,
        max_output_tokens=20,
    )
    assert queued == Completion(
        '{"queued":true}',
        prompt_tokens=7,
        completion_tokens=2,
        output_mode="json_object",
    )

    scored = await provider.complete(
        endpoint(adapter="fake"),
        "model",
        "system",
        "SCORER: importance\nDEFAULT importance: 5\n"
        '<record id="a">plain</record>\n'
        '<record id="b">marked [importance=8]</record>',
        params={},
        output=TEXT_OUTPUT,
        max_output_tokens=20,
    )
    assert json.loads(scored.text) == [5.0, 8.0]


async def test_embeddings_and_completions_resolve_to_their_own_endpoints(
    settings: Settings,
) -> None:
    """The point of named providers: one deployment, two different services."""

    from memseek.definitions.models import ProviderConnection
    from memseek.llm.runtime import _endpoint, _split_target

    catalog = load_definition_catalog(settings)
    models = catalog.models.model_copy(
        update={
            "providers": {
                **catalog.models.providers,
                "vectors": ProviderConnection(
                    adapter="openai_compat",
                    base_url="https://vectors.example/v1",
                    api_key_env="VECTORS_API_KEY",
                    json_capability="none",
                ),
            },
            "embedding": catalog.models.embedding.model_copy(update={"provider": "vectors"}),
        }
    )
    catalog = replace(catalog, models=models)
    live = settings.model_copy(update={"llm_fake": False})

    embedding = catalog.models.embedding
    _provider, embed_endpoint, embed_resolved = _endpoint(
        live, catalog, embedding.provider, embedding.model
    )
    completion_provider, completion_model = _split_target(
        catalog.models.aliases["strong"].targets[0]
    )
    _provider, chat_endpoint, chat_resolved = _endpoint(
        live, catalog, completion_provider, completion_model
    )

    assert embed_endpoint.base_url == "https://vectors.example/v1"
    assert chat_endpoint.base_url == "https://api.openai.com/v1"
    # Endpoint capabilities travel with the endpoint, not the process.
    assert embed_endpoint.json_capability == "none"
    assert chat_endpoint.json_capability == "json_schema"
    # The persisted identity names the provider, so two endpoints serving the
    # same model name stay distinguishable.
    assert embed_resolved == f"vectors:{embedding.model}"
    assert chat_resolved == f"openai:{completion_model}"


async def test_runtime_retries_falls_back_and_audits_effective_parameters(
    settings: Settings,
) -> None:
    catalog = load_definition_catalog(settings)
    strong = catalog.models.aliases["strong"]
    patched_models = catalog.models.model_copy(
        update={
            "aliases": {
                **catalog.models.aliases,
                "strong": strong.model_copy(
                    update={"targets": (strong.targets[0], "openai:cheap-model")}
                ),
            }
        }
    )
    catalog = replace(catalog, models=patched_models)
    fake.reset()
    fake.fail_next(2)

    async def no_sleep(_seconds: float) -> object:
        return None

    resolved = await complete(
        settings,
        catalog,
        "strong",
        "trusted system",
        "hello",
        params={"top_p": 0.75},
        max_output_tokens=321,
        _sleep=no_sleep,
    )

    assert resolved.resolved == "fake:cheap-model"
    assert resolved.effective_params == {
        "temperature": 0,
        "top_p": 0.75,
        "max_output_tokens": 321,
    }
    assert [attempt.outcome for attempt in resolved.attempts] == ["error", "error", "ok"]
    assert resolved.attempts[-1].usage is not None
    assert fake.completion_calls[-1].params == {"temperature": 0, "top_p": 0.75}
    assert fake.completion_calls[-1].max_output_tokens == 321


async def test_runtime_estimates_missing_queued_usage(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    fake.reset()
    fake.enqueue("{}")

    resolved = await complete(settings, catalog, "cheap", "system", "prompt")

    assert resolved.attempts[-1].usage is not None
    assert resolved.attempts[-1].usage.estimated is True


async def test_runtime_retries_a_malformed_embedding_response(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_definition_catalog(settings)
    responses = iter(
        (
            EmbeddingResult((), prompt_tokens=3),
            EmbeddingResult(((0.0,) * catalog.models.embedding.dimensions,), prompt_tokens=5),
        )
    )

    async def scripted_embed(
        _endpoint: ProviderEndpoint,
        _model: str,
        _texts: list[str],
        *,
        dimensions: int,
        params: dict[str, object],
    ) -> EmbeddingResult:
        return next(responses)

    async def no_sleep(_seconds: float) -> object:
        return None

    monkeypatch.setattr(fake, "embed", scripted_embed)
    resolved = await embed(settings, catalog, ["alpha"], _sleep=no_sleep)

    assert [attempt.outcome for attempt in resolved.attempts] == ["error", "ok"]
    assert resolved.attempts[0].error_kind == "response"
    assert resolved.attempts[0].usage is not None
    assert resolved.attempts[0].usage.prompt_tokens == 3


async def test_runtime_audits_every_invalid_embedding_attempt(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_definition_catalog(settings)
    responses = iter(
        (
            EmbeddingResult((), prompt_tokens=7),
            EmbeddingResult(((0.0,),), prompt_tokens=11),
        )
    )

    async def scripted_embed(
        _endpoint: ProviderEndpoint,
        _model: str,
        _texts: list[str],
        *,
        dimensions: int,
        params: dict[str, object],
    ) -> EmbeddingResult:
        return next(responses)

    async def no_sleep(_seconds: float) -> object:
        return None

    monkeypatch.setattr(fake, "embed", scripted_embed)
    with pytest.raises(ModelAttemptsExhausted) as caught:
        await embed(settings, catalog, ["alpha"], _sleep=no_sleep)

    assert [attempt.outcome for attempt in caught.value.attempts] == ["error", "error"]
    assert [attempt.error_kind for attempt in caught.value.attempts] == ["response", "response"]
    assert [attempt.usage.prompt_tokens for attempt in caught.value.attempts if attempt.usage] == [
        7,
        11,
    ]


async def test_openai_compatible_adapter_uses_native_json_schema_by_default() -> None:
    request_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
        "additionalProperties": False,
    }
    async with provider:
        completion = await provider.complete(
            endpoint(json_schema_strict=True),
            "model-a",
            "trusted",
            "untrusted",
            params={"temperature": 0},
            output=CompletionOutput.json_schema("pipeline_result", schema),
            max_output_tokens=50,
        )

    assert completion == Completion(
        "{}", prompt_tokens=3, completion_tokens=1, output_mode="json_schema"
    )
    assert request_bodies == [
        {
            "model": "model-a",
            "messages": [
                {"role": "system", "content": "trusted"},
                {"role": "user", "content": "untrusted"},
            ],
            "max_completion_tokens": 50,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "pipeline_result",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
    ]


async def test_openai_compatible_adapter_selects_configured_json_capability() -> None:
    request_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    schema = {"type": "object", "additionalProperties": False}
    async with provider:
        object_result = await provider.complete(
            endpoint(json_capability="json_object"),
            "model-a",
            "trusted",
            "untrusted",
            params={},
            output=CompletionOutput.json_schema("result", schema),
            max_output_tokens=25,
        )
        text_result = await provider.complete(
            endpoint(json_capability="none"),
            "model-a",
            "trusted",
            "untrusted",
            params={},
            output=CompletionOutput.json_schema("result", schema),
            max_output_tokens=25,
        )

    assert request_bodies[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in request_bodies[1]
    assert object_result.output_mode == "json_object"
    assert text_result.output_mode == "text"


async def test_openai_compatible_schema_rejection_is_not_downgraded() -> None:
    request_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={"error": {"message": "schema unsupported", "type": "invalid_request"}},
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    async with provider:
        with pytest.raises(ValueError, match="schema unsupported"):
            await provider.complete(
                endpoint(),
                "model-a",
                "trusted",
                "untrusted",
                params={},
                output=CompletionOutput.json_schema(
                    "result", {"type": "object", "additionalProperties": False}
                ),
                max_output_tokens=25,
            )

    assert len(request_bodies) == 1
    response_format = cast(dict[str, object], request_bodies[0]["response_format"])
    assert response_format["type"] == "json_schema"


async def test_openai_compatible_adapter_supports_legacy_token_limit_field() -> None:
    request_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    async with provider:
        await provider.complete(
            endpoint(token_limit_field="max_tokens"),
            "legacy-model",
            "trusted",
            "untrusted",
            params={},
            output=TEXT_OUTPUT,
            max_output_tokens=25,
        )

    assert request_bodies[0]["max_tokens"] == 25
    assert "max_completion_tokens" not in request_bodies[0]


async def test_openai_compatible_request_error_keeps_provider_detail_but_not_unknown_fields() -> (
    None
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Unsupported value for this model",
                    "param": "temperature",
                    "type": "invalid_request_error",
                },
                "secret": "must-not-be-repeated",
            },
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    async with provider:
        with pytest.raises(ValueError, match="temperature") as caught:
            await provider.complete(
                endpoint(),
                "reasoning-model",
                "trusted",
                "untrusted",
                params={},
                output=TEXT_OUTPUT,
                max_output_tokens=25,
            )

    assert "must-not-be-repeated" not in str(caught.value)


async def test_openai_compatible_embedding_restores_index_order() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 1]},
                    {"index": 0, "embedding": [1, 0]},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    async with provider:
        result = await provider.embed(
            endpoint(), "embed-model", ["a", "b"], dimensions=2, params={}
        )

    assert result.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert result.prompt_tokens == 4


async def test_openai_compatible_keeps_one_client_per_endpoint() -> None:
    """Two endpoints must not share a client, or one would send the other's key."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    first = endpoint(base_url="https://first.example/v1", api_key="key-one")
    second = endpoint(base_url="https://second.example/v1", api_key="key-two")

    async def call(target: ProviderEndpoint) -> None:
        await provider.complete(
            target,
            "model-a",
            "system",
            "prompt",
            params={},
            output=JSON_OBJECT_OUTPUT,
            max_output_tokens=10,
        )

    async with provider:
        await call(first)
        await call(first)
        assert len(provider._http_clients) == 1
        reused = next(iter(provider._http_clients.values()))

        await call(second)
        assert len(provider._http_clients) == 2
        clients = list(provider._http_clients.values())
        assert reused in clients
        assert {str(client.base_url) for client in clients} == {
            "https://first.example/v1/",
            "https://second.example/v1/",
        }
        assert {client.headers["authorization"] for client in clients} == {
            "Bearer key-one",
            "Bearer key-two",
        }

    assert all(client.is_closed for client in clients)
    assert provider._http_clients == {}
    await provider.aclose()


async def test_openai_compatible_rejects_duplicate_embedding_indices() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1, 0]},
                    {"index": 0, "embedding": [0, 1]},
                ]
            },
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    async with provider:
        with pytest.raises(LLMTransportError, match="invalid shape"):
            await provider.embed(endpoint(), "embed-model", ["a", "b"], dimensions=2, params={})
