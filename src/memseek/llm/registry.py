"""Typed LLM provider seam and static provider descriptors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

type CompletionOutputMode = Literal["text", "json_object", "json_schema"]
type JSONCapability = Literal["json_object", "json_schema"]


@dataclass(frozen=True, slots=True)
class CompletionOutput:
    """Provider-neutral requested completion shape."""

    mode: CompletionOutputMode
    name: str | None = None
    schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.mode == "json_schema":
            if self.schema is None:
                raise ValueError("json_schema output requires schema")
            if self.name is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
                raise ValueError("json_schema output name must use 1-64 letters, digits, _ or -")
            return
        if self.name is not None or self.schema is not None:
            raise ValueError(f"{self.mode} output forbids schema and name")

    @classmethod
    def json_schema(cls, name: str, schema: Mapping[str, Any]) -> CompletionOutput:
        return cls("json_schema", name=name, schema=schema)


TEXT_OUTPUT = CompletionOutput("text")
JSON_OBJECT_OUTPUT = CompletionOutput("json_object")


def materialize_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an immutable catalog schema into ordinary JSON containers."""

    def materialize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: materialize(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [materialize(item) for item in value]
        return value

    return materialize(schema)


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    output_mode: CompletionOutputMode = "text"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    prompt_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """One catalog-declared connection, resolved and ready to execute.

    Adapters receive this instead of process settings so that "which endpoint,
    with which credential and which output capabilities" is decided once, by the
    definition that named it, rather than re-derived from globals inside each
    adapter — which is what previously forced every model in a deployment onto a
    single base URL and key.
    """

    provider: str
    adapter: str
    base_url: str
    api_key: str
    json_capability: Literal["json_schema", "json_object", "none"]
    json_schema_strict: bool
    token_limit_field: str
    max_concurrency: int

    def identity(self, model: str) -> str:
        """The `provider:model` string persisted for audit and compatibility."""

        return f"{self.provider}:{model}"


class LLMProvider(Protocol):
    NAME: ClassVar[str]

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
    ) -> Completion: ...

    async def embed(
        self,
        endpoint: ProviderEndpoint,
        model: str,
        texts: list[str],
        *,
        dimensions: int,
        params: Mapping[str, Any],
    ) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Static maximum capabilities implemented by one provider Adapter."""

    name: str
    completion_params: frozenset[str]
    supports_completion: bool = True
    supports_embedding: bool = True
    json_capabilities: frozenset[JSONCapability] = frozenset()


_GENERATION_PARAMS = frozenset(
    {
        "temperature",
        "max_output_tokens",
        "top_p",
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
    }
)

_JSON_CAPABILITIES: frozenset[JSONCapability] = frozenset({"json_object", "json_schema"})

PROVIDER_DESCRIPTORS: dict[str, ProviderDescriptor] = {
    "openai_compat": ProviderDescriptor(
        "openai_compat", _GENERATION_PARAMS, json_capabilities=_JSON_CAPABILITIES
    ),
    "fake": ProviderDescriptor("fake", _GENERATION_PARAMS, json_capabilities=_JSON_CAPABILITIES),
}

# Runtime adapters register themselves here. Keeping descriptors separate lets
# definition loading validate aliases without importing network clients.
PROVIDERS: dict[str, LLMProvider] = {}


def provider_descriptor(name: str) -> ProviderDescriptor:
    try:
        return PROVIDER_DESCRIPTORS[name]
    except KeyError as exc:
        raise ValueError(f"unknown LLM provider {name!r}") from exc


def validate_generation_params(provider: str, params: dict[str, object]) -> None:
    descriptor = provider_descriptor(provider)
    unknown = set(params) - descriptor.completion_params
    if unknown:
        raise ValueError(f"unsupported {provider} generation parameter(s): {sorted(unknown)}")


class LLMTransportError(RuntimeError):
    """All bounded attempts for an LLM call were exhausted."""
