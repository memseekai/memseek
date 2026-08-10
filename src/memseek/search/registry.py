"""Typed search-backend extension contracts and capability descriptors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol
from uuid import UUID

from memseek.config import Settings

from .scope import FieldVersions
from .spec import SearchSource

SearchCapability = Literal["vector", "text", "recent", "structured"]


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    source: SearchSource
    query: str | None = None
    # Portable field name -> per stored (collection, version) declaration.
    # Structured pushdown and ordering dispatch on the row's exact version.
    field_versions: Mapping[str, FieldVersions] = field(default_factory=dict)
    layout: str | None = None
    # The active embedding space, resolved from the catalog by the engine.  A
    # backend must never guess it: vectors from another space are not comparable,
    # and a backend that defaulted would silently return meaningless neighbours.
    embedding_space: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateHit:
    id: UUID
    channel: str
    backend_score: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class SearchBackend(Protocol):
    NAME: ClassVar[str]
    CAPS: ClassVar[frozenset[SearchCapability]]

    async def candidates(
        self,
        cfg: Settings,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        qvec: list[float] | None,
    ) -> list[CandidateHit]: ...

    async def upsert(self, cfg: Settings, rows: list[dict[str, Any]]) -> None: ...

    async def delete(self, cfg: Settings, workspace: str, rows: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True, slots=True)
class SearchBackendDescriptor:
    name: str
    capabilities: frozenset[SearchCapability]
    allowed_options: frozenset[str]
    credential_setting: str | None = None

    def usable(self, settings: Settings) -> bool:
        if self.credential_setting is None:
            return True
        return bool(getattr(settings, self.credential_setting, ""))


SEARCH_BACKENDS: dict[str, SearchBackendDescriptor] = {
    "pg": SearchBackendDescriptor(
        name="pg",
        capabilities=frozenset({"vector", "text", "recent", "structured"}),
        allowed_options=frozenset(),
    ),
    "turbopuffer": SearchBackendDescriptor(
        name="turbopuffer",
        capabilities=frozenset({"vector", "text", "recent", "structured"}),
        allowed_options=frozenset({"layout", "consistency", "enabled_if_credentials"}),
        credential_setting="turbopuffer_api_key",
    ),
}


def backend_descriptor(name: str) -> SearchBackendDescriptor:
    try:
        return SEARCH_BACKENDS[name]
    except KeyError as exc:
        raise ValueError(f"unknown search backend {name!r}") from exc


def required_capabilities(mode: str) -> frozenset[SearchCapability]:
    if mode == "hybrid":
        return frozenset({"vector", "text"})
    if mode == "vector":
        return frozenset({"vector"})
    if mode == "text":
        return frozenset({"text"})
    if mode == "recent":
        return frozenset({"recent"})
    if mode == "structured":
        return frozenset({"structured"})
    raise ValueError(f"unknown search mode {mode!r}")
