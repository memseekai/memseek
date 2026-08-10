"""Public immutable definition-catalog interface."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .errors import (
    CollectionDefinitionMismatch,
    DefinitionError,
    DefinitionValidationError,
)
from .models import (
    ArtifactDefinition,
    ArtifactLearning,
    CollectionDefinition,
    GraphProjection,
    McpDefinition,
    McpToolDefinition,
    PackageDefinition,
    ParameterDefinition,
    ProcessorDefinition,
    SearchProfileDefinition,
    TombstoneRetention,
    ViewDefinition,
    parameter_json_schema,
    parameters_json_schema,
)

if TYPE_CHECKING:
    from .loader import DefinitionCatalog, DefinitionSources

_LOADER_EXPORTS = frozenset(
    {
        "DefinitionCatalog",
        "DefinitionSources",
        "canonical_json",
        "compile_definition_catalog",
        "load_definition_catalog",
        "sha256_canonical",
    }
)


def __getattr__(name: str) -> Any:
    """Load the catalog compiler only when a caller asks for it.

    Definition models are also used by the public Pipeline and Task Interfaces.
    Keeping the compiler lazy prevents those lightweight imports from depending
    on loader startup order.
    """

    if name not in _LOADER_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.loader"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LOADER_EXPORTS})


__all__ = [
    "ArtifactDefinition",
    "ArtifactLearning",
    "CollectionDefinition",
    "CollectionDefinitionMismatch",
    "DefinitionCatalog",
    "DefinitionError",
    "DefinitionSources",
    "DefinitionValidationError",
    "GraphProjection",
    "McpDefinition",
    "McpToolDefinition",
    "PackageDefinition",
    "ParameterDefinition",
    "ProcessorDefinition",
    "SearchProfileDefinition",
    "TombstoneRetention",
    "ViewDefinition",
    "canonical_json",
    "compile_definition_catalog",
    "load_definition_catalog",
    "parameter_json_schema",
    "parameters_json_schema",
    "sha256_canonical",
]
