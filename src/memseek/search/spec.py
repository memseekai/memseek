"""Immutable portable search request schema."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from memseek.definitions.base import FenceDeclaration, PublicName, StrictModel, ensure_unique

from .rank import RankValidationError, validate_rank_expression

SearchMode = Literal["vector", "text", "hybrid", "recent", "structured"]
StatusScope = Literal["active", "draft", "all"]
KeyedScope = bool | Literal["any"]
VersionScope = Literal["current", "all"]
IncludeField = Literal[
    "text",
    "scores",
    "collection",
    "collection_version",
    "entity",
    "type",
    "key",
    "status",
    "depth",
    "occurred_at",
    "created_at",
    "run_id",
]

PREDICATE_OPERATORS = frozenset(
    {"eq", "in", "gt", "gte", "lt", "lte", "exists", "contains_any", "contains_all"}
)
_EXACT_TEMPLATE_RE = re.compile(r"^{{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*}}$")


class SearchScope(StrictModel):
    collections: tuple[str, ...] = ()
    collection_versions: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    entities: tuple[str, ...] = ()
    types: tuple[PublicName, ...] = ()
    status: StatusScope = "active"
    keyed: KeyedScope = "any"
    versions: VersionScope = "current"
    occurred_after: datetime | str | None = None
    occurred_before: datetime | str | None = None
    depth_lte: int | None = Field(default=None, ge=0)

    @field_validator("collections")
    @classmethod
    def validate_collections(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 100:
            raise ValueError("collections is capped at 100")
        ensure_unique(value, "collections")
        for name in value:
            if name == "_system":
                continue
            # Reuse Pydantic's public-name adapter indirectly without making
            # `_system` a valid public definition identity.
            import re

            if not re.fullmatch(r"^[a-z][a-z0-9._-]{0,63}$", name):
                raise ValueError(f"invalid collection name {name!r}")
        return value

    @field_validator("entities", "types")
    @classmethod
    def validate_bounded_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 100:
            raise ValueError("scope lists are capped at 100")
        ensure_unique(value, "scope list")
        return value

    @field_validator("collection_versions")
    @classmethod
    def validate_collection_versions(
        cls, value: dict[str, tuple[int, ...]]
    ) -> dict[str, tuple[int, ...]]:
        if len(value) > 100:
            raise ValueError("collection_versions is capped at 100")
        for name, versions in value.items():
            if name == "_system" or not re.fullmatch(r"^[a-z][a-z0-9._-]{0,63}$", name):
                raise ValueError(f"invalid collection_versions name {name!r}")
            if not versions or any(version < 1 for version in versions):
                raise ValueError(f"collection_versions.{name} requires positive versions")
            ensure_unique(versions, f"collection_versions.{name}")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> SearchScope:
        stray = set(self.collection_versions) - set(self.collections)
        if stray:
            raise ValueError(f"collection_versions contains out-of-scope names: {sorted(stray)}")
        if isinstance(self.occurred_after, datetime) and isinstance(self.occurred_before, datetime):
            if self.occurred_after.tzinfo is None or self.occurred_before.tzinfo is None:
                raise ValueError("occurred time bounds must include timezones")
            if self.occurred_after >= self.occurred_before:
                raise ValueError("occurred_after must be earlier than occurred_before")
        return self


class SearchParams(StrictModel):
    candidates: int | None = Field(default=None, ge=1, le=1_000)


class OrderBy(StrictModel):
    field: PublicName
    direction: Literal["asc", "desc"] = "asc"


class FusionSpec(StrictModel):
    kind: Literal["rrf"] = "rrf"
    rank_constant: int = Field(default=60, ge=1, le=1_000)


class RerankSpec(StrictModel):
    """An opt-in bounded relevance judgment after canonical candidate reload."""

    backend: Literal["none", "llm_judge"] = "none"
    top_n: int = Field(default=20, ge=1, le=20)


class GraphBoostSpec(StrictModel):
    """An opt-in structural boost for records near one graph anchor."""

    anchor: str = Field(min_length=1, max_length=128)
    graph: PublicName | None = None
    depth: int = Field(default=2, ge=1, le=4)
    weight: float = Field(default=0.05, gt=0, le=1)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("anchor")
    @classmethod
    def normalize_anchor(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("graph_boost.anchor must not be blank")
        return normalized

    @field_validator("weight")
    @classmethod
    def finite_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("graph_boost.weight must be finite")
        return value


def _validate_predicates(value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for field, predicate in value.items():
        if not predicate:
            raise ValueError(f"where.{field} must contain at least one predicate")
        unknown = set(predicate) - PREDICATE_OPERATORS
        if unknown:
            raise ValueError(f"where.{field} contains unknown operator(s): {sorted(unknown)}")
        if "exists" in predicate:
            operand = predicate["exists"]
            if not isinstance(operand, bool) and not (
                isinstance(operand, str) and _EXACT_TEMPLATE_RE.fullmatch(operand)
            ):
                raise ValueError(f"where.{field}.exists must be boolean")
        for operator in ("in", "contains_any", "contains_all"):
            operand = predicate.get(operator)
            if operand is None:
                continue
            if isinstance(operand, str) and _EXACT_TEMPLATE_RE.fullmatch(operand):
                continue
            if not isinstance(operand, (list, tuple)) or not operand:
                raise ValueError(f"where.{field}.{operator} must be a non-empty list")
            if len(operand) > 100:
                raise ValueError(f"where.{field}.{operator} is capped at 100 values")
    return value


class SearchSource(StrictModel):
    name: PublicName
    mode: SearchMode
    scope: SearchScope = Field(default_factory=SearchScope)
    where: dict[PublicName, dict[str, Any]] = Field(default_factory=dict)
    order_by: tuple[OrderBy, ...] = ()
    params: SearchParams = Field(default_factory=SearchParams)
    rank: Any | None = None
    k: int = Field(default=20, ge=1, le=100)
    weight: float = Field(default=1.0, gt=0, le=100)

    @field_validator("where")
    @classmethod
    def validate_where(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return _validate_predicates(value)

    @field_validator("weight")
    @classmethod
    def finite_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("weight must be finite")
        return value

    @model_validator(mode="after")
    def validate_mode_shape(self) -> SearchSource:
        ensure_unique([order.field for order in self.order_by], "order_by fields")
        if self.mode == "structured":
            if self.rank is not None:
                raise ValueError("structured sources must omit rank")
            if not self.order_by:
                raise ValueError("structured sources require order_by")
        elif self.rank is not None:
            try:
                validate_rank_expression(self.rank, mode=self.mode)
            except RankValidationError as exc:
                raise ValueError(str(exc)) from exc
        return self

    @property
    def candidates(self) -> int:
        return self.params.candidates or min(1_000, max(100, 10 * self.k))


class SearchSpec(StrictModel):
    q: str | None = None
    mode: SearchMode | None = None
    scope: SearchScope | None = None
    sources: tuple[SearchSource, ...] | None = None
    fuse: FusionSpec | None = None
    boost: Any | None = None
    rerank: RerankSpec | None = None
    graph_boost: GraphBoostSpec | None = None
    where: dict[PublicName, dict[str, Any]] = Field(default_factory=dict)
    order_by: tuple[OrderBy, ...] = ()
    params: SearchParams = Field(default_factory=SearchParams)
    rank: Any | None = None
    k: int = Field(default=20, ge=1, le=100)
    include: tuple[IncludeField, ...] = ()
    fields: tuple[PublicName, ...] = ()
    annotations: tuple[PublicName, ...] = ()
    render: bool = False
    # `rendered` rows are always escaped.  They are wrapped in an element only
    # when the requester or the view author declares one here, so no caller
    # receives framing it did not ask for.
    fence: FenceDeclaration | None = None

    @field_validator("where")
    @classmethod
    def validate_where(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return _validate_predicates(value)

    @field_validator("include")
    @classmethod
    def validate_include(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique(value, "include")
        return value

    @field_validator("fields", "annotations")
    @classmethod
    def validate_projection_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 16:
            raise ValueError("fields and annotations are capped at 16")
        ensure_unique(value, "projection list")
        return value

    @model_validator(mode="after")
    def validate_request_shape(self) -> SearchSpec:
        ensure_unique([order.field for order in self.order_by], "order_by fields")
        multi = self.sources is not None
        if multi:
            if (
                self.mode is not None
                or self.scope is not None
                or self.where
                or self.order_by
                or self.rank
            ):
                raise ValueError("sources is mutually exclusive with top-level source fields")
            if not self.sources or len(self.sources) > 8:
                raise ValueError("multi-source search requires 1 through 8 sources")
            ensure_unique([source.name for source in self.sources], "source names")
            if self.fuse is None:
                raise ValueError("multi-source search requires fuse")
            if self.boost is not None:
                try:
                    validate_rank_expression(self.boost, boost=True)
                except RankValidationError as exc:
                    raise ValueError(str(exc)) from exc
            if any(source.mode in {"vector", "text", "hybrid"} for source in self.sources):
                self._require_query()
        else:
            if self.mode is None:
                raise ValueError("single-source search requires mode")
            if self.fuse is not None or self.boost is not None:
                raise ValueError("single-source search cannot declare fuse or boost")
            if self.mode in {"vector", "text", "hybrid"}:
                self._require_query()
            if self.mode == "structured":
                if self.rank is not None:
                    raise ValueError("structured mode must omit rank")
                if not self.order_by:
                    raise ValueError("structured mode requires order_by")
            elif self.rank is not None:
                try:
                    validate_rank_expression(self.rank, mode=self.mode)
                except RankValidationError as exc:
                    raise ValueError(str(exc)) from exc
        if self.rerank is not None and self.rerank.backend == "llm_judge":
            modes = (
                tuple(source.mode for source in self.sources)
                if self.sources is not None
                else (self.mode,)
            )
            unsupported = sorted(
                {mode for mode in modes if mode not in {"text", "vector", "hybrid"}}
            )
            if unsupported:
                raise ValueError(
                    "llm_judge reranking requires text, vector, or hybrid search; "
                    f"got {unsupported}"
                )
        if self.fence is not None and not self.render:
            raise ValueError("fence requires render: true")
        return self

    def _require_query(self) -> None:
        if self.q is None or not self.q.strip():
            raise ValueError("q is required and must be non-blank for relevance search")

    @property
    def candidates(self) -> int:
        return self.params.candidates or min(1_000, max(100, 10 * self.k))


SEARCH_SPEC_JSON_SCHEMA: dict[str, Any] = SearchSpec.model_json_schema()
