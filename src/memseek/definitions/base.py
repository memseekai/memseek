"""Shared immutable types for declarative definitions."""

from __future__ import annotations

import math
import re
from collections.abc import Hashable
from typing import Annotated, Any, Never, SupportsIndex, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PUBLIC_NAME_PATTERN = r"^[a-z][a-z0-9._-]{0,63}$"
PROCESSOR_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
TRIGGER_NAME_PATTERN = r"^[a-z][a-z0-9._-]{0,63}$"
# Matches the ``record_embedding.space`` check constraint, so a declared space
# can never be rejected by the database that has to store it.
EMBEDDING_SPACE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
ENV_VAR_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

PublicName = Annotated[str, StringConstraints(pattern=PUBLIC_NAME_PATTERN)]
ProcessorName = Annotated[str, StringConstraints(pattern=PROCESSOR_NAME_PATTERN)]
ProviderName = Annotated[str, StringConstraints(pattern=PROVIDER_NAME_PATTERN)]
EmbeddingSpace = Annotated[str, StringConstraints(pattern=EMBEDDING_SPACE_PATTERN)]
EnvVarName = Annotated[str, StringConstraints(pattern=ENV_VAR_PATTERN)]
TriggerName = Annotated[str, StringConstraints(pattern=TRIGGER_NAME_PATTERN)]
SemVer = Annotated[str, StringConstraints(pattern=SEMVER_PATTERN)]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    """Base class for immutable, typo-intolerant configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _immutable() -> Never:
    raise TypeError("definition values are immutable")


class FrozenDict(dict[Any, Any]):
    """JSON-compatible dictionary whose contents cannot be changed."""

    def __setitem__(self, key: Any, value: Any) -> None:
        _immutable()

    def __delitem__(self, key: Any) -> None:
        _immutable()

    def clear(self) -> None:
        _immutable()

    def pop(self, key: Any, default: Any = None) -> Any:
        _immutable()

    def popitem(self) -> tuple[Any, Any]:
        _immutable()

    def setdefault(self, key: Any, default: Any = None) -> Any:
        _immutable()

    def update(self, *args: Any, **kwargs: Any) -> None:
        _immutable()

    def __ior__(self, other: Any) -> FrozenDict:
        _immutable()


class FrozenList(list[Any]):
    """JSON-compatible list whose contents cannot be changed."""

    def __setitem__(self, key: Any, value: Any) -> None:
        _immutable()

    def __delitem__(self, key: Any) -> None:
        _immutable()

    def append(self, value: Any) -> None:
        _immutable()

    def clear(self) -> None:
        _immutable()

    def extend(self, values: Any) -> None:
        _immutable()

    def insert(self, index: SupportsIndex, value: Any, /) -> None:
        _immutable()

    def pop(self, index: SupportsIndex = -1, /) -> Any:
        _immutable()

    def remove(self, value: Any) -> None:
        _immutable()

    def reverse(self) -> None:
        _immutable()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        _immutable()

    def __iadd__(self, values: Any) -> FrozenList:
        _immutable()

    def __imul__(self, count: SupportsIndex, /) -> FrozenList:
        _immutable()


def deep_freeze[T](value: T) -> T:
    """Recursively freeze JSON-like values and nested Pydantic models."""

    if isinstance(value, BaseModel):
        updates = {name: deep_freeze(getattr(value, name)) for name in type(value).model_fields}
        return cast(T, value.model_copy(update=updates))
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, dict):
        frozen = FrozenDict({deep_freeze(key): deep_freeze(item) for key, item in value.items()})
        return cast(T, frozen)
    if isinstance(value, list):
        return cast(T, FrozenList(deep_freeze(item) for item in value))
    if isinstance(value, tuple):
        return cast(T, tuple(deep_freeze(item) for item in value))
    if isinstance(value, set | frozenset):
        return cast(T, frozenset(deep_freeze(item) for item in value))
    return value


class DefinitionModel(StrictModel):
    """Common public identity fields.

    ``definition_hash`` is injected after normalization and is excluded from
    serialization so it cannot hash itself.
    """

    name: PublicName
    definition_hash: str = Field(default="", exclude=True, repr=False)


class VersionedDefinition(DefinitionModel):
    version: int = Field(ge=1)
    active: bool = False


class FenceDeclaration(StrictModel):
    """An author-declared wrapper around a rendered record sequence.

    Fencing is never applied implicitly.  A renderer escapes untrusted text
    unconditionally, but the element that surrounds it — and any sentence
    introducing it to a model — exists only because an author asked for it
    here.  ``tag`` names the element; the renderer always marks it
    ``untrusted="true"`` so the machine-readable boundary cannot drift from
    the escaping it pairs with.  ``preamble`` is the author's own prose and
    has no default: the engine writes no English into an author's prompt.
    """

    tag: PublicName = "records"
    preamble: NonBlank | None = None


def ensure_unique[T: Hashable](values: tuple[T, ...] | list[T], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must contain unique values")


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def split_exact_reference(reference: str, *, semver: bool = False) -> tuple[str, str | int]:
    """Parse ``name@version`` and validate both halves."""

    try:
        name, version = reference.rsplit("@", 1)
    except ValueError as exc:
        raise ValueError(f"exact reference required: {reference!r}") from exc
    if not re.fullmatch(PUBLIC_NAME_PATTERN, name):
        raise ValueError(f"invalid referenced name: {name!r}")
    if semver:
        if not re.fullmatch(SEMVER_PATTERN, version):
            raise ValueError(f"invalid semantic version: {version!r}")
        return name, version
    if not version.isdigit() or int(version) < 1:
        raise ValueError(f"invalid positive version: {version!r}")
    return name, int(version)
