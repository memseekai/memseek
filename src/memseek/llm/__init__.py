"""LLM extension contracts."""

from .registry import (
    JSON_OBJECT_OUTPUT,
    PROVIDER_DESCRIPTORS,
    PROVIDERS,
    TEXT_OUTPUT,
    Completion,
    CompletionOutput,
    EmbeddingResult,
    LLMProvider,
    LLMTransportError,
    ProviderDescriptor,
)

__all__ = [
    "JSON_OBJECT_OUTPUT",
    "PROVIDERS",
    "PROVIDER_DESCRIPTORS",
    "TEXT_OUTPUT",
    "Completion",
    "CompletionOutput",
    "EmbeddingResult",
    "LLMProvider",
    "LLMTransportError",
    "ProviderDescriptor",
]
