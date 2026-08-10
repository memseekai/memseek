"""Bounded model-alias resolution and provider execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog
from memseek.logging import log_llm_debug

from .fake import estimate_tokens, fake
from .openai_compat import openai_compat
from .registry import (
    PROVIDERS,
    TEXT_OUTPUT,
    Completion,
    CompletionOutput,
    EmbeddingResult,
    LLMProvider,
    LLMTransportError,
    ProviderEndpoint,
    materialize_json_schema,
    validate_generation_params,
)

type Sleep = Callable[[float], Awaitable[object]]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    estimated: bool


@dataclass(frozen=True, slots=True)
class ModelAttempt:
    alias: str
    attempt: int
    resolved: str
    effective_params: Mapping[str, object]
    outcome: str
    usage: Usage | None
    prompt_sha256: str | None = None
    response_sha256: str | None = None
    requested_output_mode: str | None = None
    output_mode: str | None = None
    output_schema_sha256: str | None = None
    error_kind: str | None = None

    def audit_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "alias": self.alias,
            "attempt": self.attempt,
            "resolved": self.resolved,
            "effective_params": dict(self.effective_params),
            "outcome": self.outcome,
        }
        if self.usage is not None:
            value["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "estimated": self.usage.estimated,
            }
        if self.prompt_sha256 is not None:
            value["prompt_sha256"] = self.prompt_sha256
        if self.response_sha256 is not None:
            value["response_sha256"] = self.response_sha256
        if self.requested_output_mode is not None:
            value["requested_output_mode"] = self.requested_output_mode
        if self.output_mode is not None:
            value["output_mode"] = self.output_mode
        if self.output_schema_sha256 is not None:
            value["output_schema_sha256"] = self.output_schema_sha256
        if self.error_kind is not None:
            value["error_kind"] = self.error_kind
        return value


@dataclass(frozen=True, slots=True)
class ResolvedCompletion:
    completion: Completion
    resolved: str
    effective_params: Mapping[str, object]
    attempts: tuple[ModelAttempt, ...]


@dataclass(frozen=True, slots=True)
class ResolvedEmbedding:
    embedding: EmbeddingResult
    resolved: str
    attempts: tuple[ModelAttempt, ...]


class ModelAttemptsExhausted(LLMTransportError):
    """A bounded provider call failed, retaining every attempt for run audit."""

    def __init__(self, message: str, attempts: tuple[ModelAttempt, ...]) -> None:
        self.attempts = attempts
        super().__init__(message)


class _EmbeddingResponseError(LLMTransportError):
    """A retryable malformed response from an embedding provider."""

    kind = "response"


_SEMAPHORES: dict[tuple[int, int], asyncio.Semaphore] = {}


def _semaphore(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = (id(loop), limit)
    semaphore = _SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _SEMAPHORES[key] = semaphore
    return semaphore


def _endpoint(
    settings: Settings,
    catalog: DefinitionCatalog,
    provider_name: str,
    model: str,
) -> tuple[LLMProvider, ProviderEndpoint, str]:
    """Resolve one declared connection into an executable adapter and endpoint.

    ``LLM_FAKE`` substitutes the adapter but keeps the declared connection, so
    offline runs exercise the same resolution path a deployment uses.  The
    returned identity string is what gets persisted, and it names the *provider*
    rather than the adapter: two endpoints serving the same model name are not
    interchangeable, and the stored identity has to be able to say so.
    """

    connection = catalog.models.providers.get(provider_name)
    if connection is None:
        raise LLMTransportError(f"undeclared provider {provider_name!r}")
    adapter_name = "fake" if settings.llm_fake else connection.adapter
    provider = PROVIDERS.get(adapter_name)
    if provider is None:
        raise LLMTransportError(f"adapter {adapter_name!r} has no runtime adapter")
    endpoint = ProviderEndpoint(
        provider=adapter_name if settings.llm_fake else provider_name,
        adapter=adapter_name,
        base_url=connection.base_url,
        api_key=settings.secret(connection.api_key_env),
        json_capability=connection.json_capability,
        json_schema_strict=connection.json_schema_strict,
        token_limit_field=connection.token_limit_field,
        max_concurrency=settings.llm_max_concurrency,
    )
    return provider, endpoint, endpoint.identity(model)


def _split_target(configured_target: str) -> tuple[str, str]:
    provider_name, separator, model = configured_target.partition(":")
    if not separator or not provider_name or not model:
        raise LLMTransportError(f"invalid provider target {configured_target!r}")
    return provider_name, model


def _usage(completion: Completion, *, system: str, prompt: str) -> Usage:
    estimated = completion.prompt_tokens is None or completion.completion_tokens is None
    return Usage(
        prompt_tokens=(
            completion.prompt_tokens
            if completion.prompt_tokens is not None
            else estimate_tokens(system) + estimate_tokens(prompt)
        ),
        completion_tokens=(
            completion.completion_tokens
            if completion.completion_tokens is not None
            else estimate_tokens(completion.text)
        ),
        estimated=estimated,
    )


async def complete(
    settings: Settings,
    catalog: DefinitionCatalog,
    alias: str,
    system: str,
    prompt: str,
    *,
    params: Mapping[str, object] | None = None,
    output: CompletionOutput = TEXT_OUTPUT,
    max_output_tokens: int | None = None,
    context: str | None = None,
    _sleep: Sleep = asyncio.sleep,
    _retry_delay_s: float = 2.0,
) -> ResolvedCompletion:
    """Resolve an alias and execute one retry per ordered provider candidate.

    ``context`` labels the caller (e.g. ``"derivation:<task>"`` or
    ``"processor:<name>"``) purely so LLM debug logging can attribute the
    request; it does not affect resolution or execution.
    """

    if output.schema is not None:
        assert output.name is not None
        output = CompletionOutput.json_schema(output.name, materialize_json_schema(output.schema))
    try:
        definition = catalog.models.aliases[alias]
    except KeyError as exc:
        raise ValueError(f"unknown model alias {alias!r}") from exc
    effective = dict(definition.params)
    effective.update(params or {})
    configured_max = effective.pop("max_output_tokens", settings.max_output_tokens)
    output_limit = max_output_tokens if max_output_tokens is not None else configured_max
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    # Sampling controls are model-specific. In particular, current reasoning
    # models reject an explicit temperature value, so aliases must opt into
    # temperature when their provider/model supports it.
    audit_params = MappingProxyType({**effective, "max_output_tokens": output_limit})
    attempts: list[ModelAttempt] = []
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema_hash = (
        hashlib.sha256(
            json.dumps(
                materialize_json_schema(output.schema),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if output.schema is not None
        else None
    )

    for configured_target in definition.targets:
        provider_name, model = _split_target(str(configured_target))
        provider, endpoint, resolved = _endpoint(settings, catalog, provider_name, model)
        validate_generation_params(provider.NAME, dict(audit_params))
        for candidate_attempt in (1, 2):
            if settings.llm_debug:
                log_llm_debug(
                    LOGGER,
                    "llm.request",
                    context=context,
                    alias=alias,
                    provider=endpoint.provider,
                    adapter=endpoint.adapter,
                    model=model,
                    resolved=resolved,
                    attempt=candidate_attempt,
                    system=system,
                    prompt=prompt,
                    params=dict(audit_params),
                    max_output_tokens=output_limit,
                    output_mode=output.mode,
                    output_schema_name=output.name,
                )
            try:
                async with _semaphore(settings.llm_max_concurrency):
                    result = await provider.complete(
                        endpoint,
                        model,
                        system,
                        prompt,
                        params=dict(effective),
                        output=output,
                        max_output_tokens=output_limit,
                    )
            except LLMTransportError as exc:
                attempts.append(
                    ModelAttempt(
                        alias=alias,
                        attempt=candidate_attempt,
                        resolved=resolved,
                        effective_params=audit_params,
                        outcome="error",
                        usage=None,
                        prompt_sha256=prompt_hash,
                        requested_output_mode=output.mode,
                        output_schema_sha256=schema_hash,
                        error_kind=getattr(exc, "kind", "transport"),
                    )
                )
                if candidate_attempt == 1:
                    await _sleep(_retry_delay_s)
                continue
            usage = _usage(result, system=system, prompt=prompt)
            if settings.llm_debug:
                log_llm_debug(
                    LOGGER,
                    "llm.response",
                    context=context,
                    alias=alias,
                    model=model,
                    resolved=resolved,
                    attempt=candidate_attempt,
                    output_mode=result.output_mode,
                    response=result.text,
                )
            attempts.append(
                ModelAttempt(
                    alias=alias,
                    attempt=candidate_attempt,
                    resolved=resolved,
                    effective_params=audit_params,
                    outcome="ok",
                    usage=usage,
                    prompt_sha256=prompt_hash,
                    response_sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                    requested_output_mode=output.mode,
                    output_mode=result.output_mode,
                    output_schema_sha256=schema_hash,
                )
            )
            return ResolvedCompletion(
                completion=result,
                resolved=resolved,
                effective_params=audit_params,
                attempts=tuple(attempts),
            )
    detail = attempts[-1].error_kind if attempts else "configuration"
    raise ModelAttemptsExhausted(
        f"all candidates for alias {alias!r} failed ({detail})", tuple(attempts)
    )


async def embed(
    settings: Settings,
    catalog: DefinitionCatalog,
    texts: list[str],
    *,
    context: str | None = None,
    _sleep: Sleep = asyncio.sleep,
    _retry_delay_s: float = 2.0,
) -> ResolvedEmbedding:
    """Embed one declared-size batch through the catalog's embedding model.

    There is deliberately no fallback candidate.  A completion may be retried on
    a second model because the answer is read once; a vector is stored and later
    compared, so silently producing one from a different model would corrupt the
    space rather than degrade a single result.
    """

    definition = catalog.models.embedding
    if len(texts) > definition.batch:
        raise ValueError(f"embedding batches are limited to {definition.batch} texts")
    provider, endpoint, resolved = _endpoint(
        settings, catalog, definition.provider, definition.model
    )
    model = definition.model
    if settings.llm_debug:
        log_llm_debug(
            LOGGER,
            "llm.embed_request",
            context=context,
            model=model,
            resolved=resolved,
            dimensions=definition.dimensions,
            text_count=len(texts),
            texts=list(texts),
        )
    attempts: list[ModelAttempt] = []
    for candidate_attempt in (1, 2):
        usage: Usage | None = None
        try:
            async with _semaphore(settings.llm_max_concurrency):
                result = await provider.embed(
                    endpoint,
                    model,
                    texts,
                    dimensions=definition.dimensions,
                    params=definition.params,
                )
            usage = _embedding_usage(result, texts)
            _validate_embedding_result(
                result, expected_count=len(texts), dimension=definition.dimensions
            )
        except LLMTransportError as exc:
            attempts.append(
                ModelAttempt(
                    alias="embedding",
                    attempt=candidate_attempt,
                    resolved=resolved,
                    effective_params=MappingProxyType({}),
                    outcome="error",
                    usage=usage,
                    error_kind=getattr(exc, "kind", "transport"),
                )
            )
            if candidate_attempt == 1:
                await _sleep(_retry_delay_s)
            continue
        assert usage is not None
        attempts.append(
            ModelAttempt(
                alias="embedding",
                attempt=candidate_attempt,
                resolved=resolved,
                effective_params=MappingProxyType({}),
                outcome="ok",
                usage=usage,
            )
        )
        return ResolvedEmbedding(result, resolved, tuple(attempts))
    raise ModelAttemptsExhausted(
        "embedding candidate failed after its bounded retry", tuple(attempts)
    )


def _embedding_usage(result: EmbeddingResult, texts: list[str]) -> Usage:
    prompt_tokens = result.prompt_tokens
    reported = (
        prompt_tokens
        if isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and prompt_tokens >= 0
        else None
    )
    return Usage(
        prompt_tokens=(
            reported if reported is not None else sum(estimate_tokens(text) for text in texts)
        ),
        completion_tokens=0,
        estimated=reported is None,
    )


def _validate_embedding_result(
    result: EmbeddingResult,
    *,
    expected_count: int,
    dimension: int,
) -> None:
    if len(result.vectors) != expected_count:
        raise _EmbeddingResponseError("embedding provider returned the wrong vector count")
    for vector in result.vectors:
        if len(vector) != dimension:
            raise _EmbeddingResponseError("embedding provider returned an invalid vector dimension")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise _EmbeddingResponseError("embedding provider returned a non-finite vector")


PROVIDERS.setdefault("fake", fake)
PROVIDERS.setdefault("openai_compat", openai_compat)
