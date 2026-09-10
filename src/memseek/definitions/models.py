"""Immutable Pydantic models for the startup definition catalog."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_serializer, model_validator

from .base import (
    SKILL_NAME_PATTERN,
    DefinitionModel,
    EmbeddingSpace,
    EnvVarName,
    NonBlank,
    ProcessorName,
    ProviderName,
    PublicName,
    SemVer,
    SkillName,
    StrictModel,
    VersionedDefinition,
    ensure_unique,
    split_exact_reference,
)

# An absolute ceiling on a declared embedding batch.  The declared value is the
# operative limit; this only stops a typo from turning into an unbounded request.
MAX_EMBEDDING_BATCH = 256

# Request body keys the runtime owns.  An author's pass-through params may not
# overwrite them, because doing so would silently detach a request from the
# model and texts the rest of the system believes it sent.
RESERVED_EMBEDDING_PARAMS = frozenset({"model", "input", "texts"})


def _validate_endpoint_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"base_url must be an absolute HTTP(S) URL: {value!r}")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"base_url must use HTTPS except on localhost: {value!r}")
    return value


class ProviderConnection(StrictModel):
    """One named endpoint a model can be called through.

    A provider is a *connection*, not a vendor: two entries may share an adapter
    and differ only in `base_url`, which is exactly what lets embeddings run on a
    different service than completions.  Everything here describes the endpoint
    itself, so an endpoint that cannot honor schema-constrained output says so
    next to its own URL rather than in a process-wide setting that would also
    (wrongly) describe every other endpoint.

    The API key never appears here.  `api_key_env` names the environment
    variable holding it, which keeps the definition safe to commit while still
    stating, explicitly, which credential this endpoint uses.
    """

    adapter: NonBlank
    base_url: NonBlank
    api_key_env: EnvVarName | None = None
    json_capability: Literal["json_schema", "json_object", "none"] = "json_schema"
    json_schema_strict: bool = False
    token_limit_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validate_endpoint_url(value)

    @model_validator(mode="after")
    def validate_connection(self) -> ProviderConnection:
        if self.json_schema_strict and self.json_capability != "json_schema":
            raise ValueError("json_schema_strict requires json_capability: json_schema")
        return self


class EmbeddingModel(StrictModel):
    """The one embedding model a catalog uses, declared in full.

    Embeddings are the only model output that is *stored* and later compared, so
    the properties that make two vectors comparable — endpoint, model, dimension,
    and the preprocessing bound `max_text_chars` — are declared together here
    rather than split between a definition and the process environment.  `space`
    names the resulting vector space: change any field above it and the space id
    must change too, because vectors from two models are not comparable.  See
    docs/changing-definitions.md for the staged re-embed that performs the swap.
    """

    provider: ProviderName
    model: NonBlank
    dimensions: int = Field(ge=8, le=4_096)
    space: EmbeddingSpace
    batch: int = Field(default=64, ge=1, le=MAX_EMBEDDING_BATCH)
    max_text_chars: int = Field(default=16_000, ge=64)
    # Extra request-body fields passed to the endpoint verbatim (for example
    # OpenAI's `dimensions`, or Voyage's `input_type`).  Deliberately not
    # interpreted: each vendor spells its options differently, and inventing a
    # neutral vocabulary here would quietly mistranslate them.
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_embedding(self) -> EmbeddingModel:
        reserved = sorted(RESERVED_EMBEDDING_PARAMS & set(self.params))
        if reserved:
            raise ValueError(f"embedding params must not set {reserved}")
        return self

    @property
    def target(self) -> str:
        """The `provider:model` identity persisted alongside every vector."""

        return f"{self.provider}:{self.model}"


class ModelAlias(StrictModel):
    targets: tuple[NonBlank, ...]
    params: dict[str, Any] = Field(default_factory=dict)
    context_tokens: int | None = Field(default=None, ge=4_096)

    @model_validator(mode="after")
    def validate_alias(self) -> ModelAlias:
        if not self.targets:
            raise ValueError("model alias requires at least one target")
        ensure_unique(self.targets, "model targets")
        temperature = self.params.get("temperature")
        if temperature is not None:
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                raise ValueError("temperature must be numeric")
            if not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
                raise ValueError("temperature must be between 0 and 2")
        max_output = self.params.get("max_output_tokens")
        if max_output is not None and (
            isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        return self


class ModelDefaults(StrictModel):
    derivation: ProcessorName
    fold: ProcessorName


class ModelCatalog(StrictModel):
    providers: dict[ProviderName, ProviderConnection]
    aliases: dict[ProcessorName, ModelAlias]
    embedding: EmbeddingModel
    defaults: ModelDefaults

    @model_validator(mode="after")
    def validate_defaults(self) -> ModelCatalog:
        if not self.providers:
            raise ValueError("providers must be non-empty")
        if not self.aliases:
            raise ValueError("aliases must be non-empty")
        if "embed" in self.aliases:
            raise ValueError(
                "'embed' is not an alias; declare the embedding model in the embedding: block"
            )
        for role, alias in (
            ("defaults.derivation", self.defaults.derivation),
            ("defaults.fold", self.defaults.fold),
        ):
            if alias not in self.aliases:
                raise ValueError(f"{role} references unknown alias {alias!r}")
        for name, alias in self.aliases.items():
            for target in alias.targets:
                provider, separator, model = target.partition(":")
                if not separator or not provider or not model:
                    raise ValueError(
                        f"aliases.{name}: target {target!r} must use provider:model syntax"
                    )
                if provider not in self.providers:
                    raise ValueError(
                        f"aliases.{name}: target {target!r} names undeclared provider {provider!r}"
                    )
        if self.embedding.provider not in self.providers:
            raise ValueError(
                f"embedding.provider names undeclared provider {self.embedding.provider!r}"
            )
        return self


class ProcessorInput(StrictModel):
    collections: tuple[PublicName, ...]
    types: tuple[PublicName, ...] = ()

    @model_validator(mode="after")
    def validate_input(self) -> ProcessorInput:
        if not self.collections:
            raise ValueError("processor input collections must be non-empty")
        ensure_unique(self.collections, "collections")
        ensure_unique(self.types, "types")
        return self


EMBEDDING_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["space"],
    "properties": {"space": {"type": "string"}},
}


class ProcessorDefinition(DefinitionModel):
    """One per-record enrichment capability.

    Every processor writes an annotation object under ``annotations.<name>``.
    Two kinds additionally project into dedicated storage: ``embedding``
    writes the record's vector, and ``score`` mirrors its number into the
    flat rankable ``scores.<name>`` map (as do ``score_fields`` promotions
    of ``json`` processors).
    """

    name: ProcessorName
    kind: Literal["embedding", "score", "json"]
    source: Literal["llm", "client", "constant"] | None = None
    input: ProcessorInput
    # The processor this one replaces.  Purely a *reading* preference: both
    # annotations stay on the record, separately auditable, and neither is ever
    # rewritten.  Declared fields, rendering, and projection prefer the newest
    # annotation present, so improving a processor does not force every reader to
    # learn both names.  See docs/processors.md.
    supersedes: ProcessorName | None = None
    # score kind only
    scale: tuple[float, float] | None = None
    default: float | None = None
    value: float | None = None
    render: bool = False
    # llm source only
    model: ProcessorName | None = None
    prompt: str | None = None
    # json kind only
    output_schema: dict[str, Any] | None = None
    default_output: Any | None = None
    score_fields: dict[ProcessorName, str] = Field(default_factory=dict)

    @field_validator("score_fields")
    @classmethod
    def validate_score_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value.values():
            if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$", path):
                raise ValueError(f"invalid score field path {path!r}")
        return value

    @model_validator(mode="after")
    def validate_kind(self) -> ProcessorDefinition:
        if self.kind == "embedding":
            forbidden = {
                "source": self.source,
                "scale": self.scale,
                "default": self.default,
                "value": self.value,
                "model": self.model,
                "prompt": self.prompt,
                "output_schema": self.output_schema,
                "default_output": self.default_output,
            }
            present = sorted(name for name, item in forbidden.items() if item is not None)
            if present or self.render or self.score_fields:
                extras = [*present, *(["render"] if self.render else [])]
                extras.extend(["score_fields"] if self.score_fields else [])
                raise ValueError(f"embedding processor forbids {extras}")
            return self

        if self.source is None:
            raise ValueError(f"{self.kind} processor requires source (llm, client, or constant)")

        if self.kind == "score":
            if self.scale is None:
                raise ValueError("score processor requires scale")
            low, high = self.scale
            if not math.isfinite(low) or not math.isfinite(high) or low >= high:
                raise ValueError("scale must contain two finite ascending numbers")
            for label, number in (("default", self.default), ("value", self.value)):
                if number is not None and (not math.isfinite(number) or not low <= number <= high):
                    raise ValueError(f"{label} must be finite and within scale")
            if self.output_schema is not None or self.default_output is not None:
                raise ValueError("score processor forbids output_schema and default_output")
            if self.score_fields:
                raise ValueError("score processor forbids score_fields; its own name is the score")
            if self.source == "llm":
                if self.default is None or self.model is None or not self.prompt:
                    raise ValueError("llm score processor requires default, model, and prompt")
                if self.value is not None:
                    raise ValueError("llm score processor forbids value")
            elif self.source == "client":
                if (
                    self.model is not None
                    or self.prompt is not None
                    or self.value is not None
                    or self.default is not None
                ):
                    raise ValueError(
                        "client score processor forbids model, prompt, value, and default"
                    )
            else:
                if self.value is None:
                    raise ValueError("constant score processor requires value")
                if self.model is not None or self.prompt is not None or self.default is not None:
                    raise ValueError("constant score processor forbids model, prompt, and default")
            return self

        # kind == "json"
        if self.scale is not None or self.default is not None or self.value is not None:
            raise ValueError("json processor forbids scale, default, and value")
        if self.render:
            raise ValueError("render is available only for score processors")
        if self.output_schema is None:
            raise ValueError("json processor requires output_schema")
        if self.source == "llm":
            if self.model is None or not self.prompt:
                raise ValueError("llm json processor requires model and prompt")
        else:
            if self.model is not None or self.prompt is not None:
                raise ValueError(f"{self.source} json processor forbids model and prompt")
            if self.source == "constant" and self.default_output is None:
                raise ValueError("constant json processor requires default_output")
        return self

    @property
    def effective_output_schema(self) -> dict[str, Any]:
        """The annotation contract, synthesized for embedding and score kinds."""

        if self.kind == "embedding":
            return EMBEDDING_OUTPUT_SCHEMA
        if self.kind == "score":
            assert self.scale is not None
            low, high = self.scale
            return {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "number", "minimum": low, "maximum": high}},
            }
        assert self.output_schema is not None
        return self.output_schema


ScalarFieldType = Literal["string", "number", "integer", "boolean", "datetime"]


class DeclaredField(StrictModel):
    path: str
    type: ScalarFieldType | tuple[ScalarFieldType]
    filter: bool = False
    sort: bool = False
    project: bool = False
    # Loader-injected supersession fallbacks for an ``annotations.<name>`` path,
    # newest first.  Excluded from serialization — and therefore from the record
    # contract hash — because preferring a newer annotation changes what a reader
    # sees, never what a stored row means.
    fallback_paths: tuple[str, ...] = Field(default=(), exclude=True, repr=False)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not re.fullmatch(
            r"^(?:content|annotations)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$",
            value,
        ):
            raise ValueError("field path must be dotted under content or annotations")
        return value

    @model_validator(mode="after")
    def validate_type(self) -> DeclaredField:
        if isinstance(self.type, tuple):
            if len(self.type) != 1:
                raise ValueError("array field type must contain exactly one scalar type")
            if self.sort:
                raise ValueError("array fields cannot be sortable")
        return self

    @property
    def is_array(self) -> bool:
        return isinstance(self.type, tuple)

    @property
    def scalar_type(self) -> str:
        return self.type[0] if isinstance(self.type, tuple) else self.type


class CollectionDefinition(VersionedDefinition):
    """One durable record contract plus the bindings attached to it.

    ``contract_hash`` is the identity persisted on every record: it covers the
    fields that determine how a stored row is read (``mode``, ``schema``,
    ``text_projection``, ``fields``, ``required_processors``).  The remaining
    fields are bindings — they change what else happens to a row, never what the
    row means — so editing them does not strand existing records.  See
    ``definitions/hashing.py`` for the exact split.
    """

    mode: Literal["event", "keyed", "mixed"]
    content_schema: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    text_projection: str | None = None
    fields: dict[PublicName, DeclaredField] = Field(default_factory=dict)
    required_processors: tuple[ProcessorName, ...] = ()
    optional_processors: tuple[ProcessorName, ...] = ()
    search_profile: PublicName
    allowed_search_profiles: tuple[PublicName, ...] = ()
    # Whether ``POST /answer`` may synthesize over this collection.  Answering is
    # the one read that composes several records into new prose, so which drawers
    # it may open is an author decision rather than a property of having an
    # embedding: raw transcripts, prompt snapshots, and operational records are
    # searchable without being sensible sources for synthesis.  It is a binding,
    # not part of the record contract — it changes what else may read a row, never
    # what the row means.
    answerable: bool = False
    contract_hash: str = Field(default="", exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_bindings(self) -> CollectionDefinition:
        ensure_unique(self.required_processors, "required_processors")
        ensure_unique(self.optional_processors, "optional_processors")
        ensure_unique(self.allowed_search_profiles, "allowed_search_profiles")
        overlap = set(self.required_processors) & set(self.optional_processors)
        if overlap:
            raise ValueError(f"processors cannot be both required and optional: {sorted(overlap)}")
        return self

    @property
    def all_search_profiles(self) -> frozenset[str]:
        return frozenset((self.search_profile, *self.allowed_search_profiles))


class SearchProfileDefinition(DefinitionModel):
    backend: Literal["pg", "turbopuffer"]
    layout: Literal["shared", "per_collection"] | None = None
    consistency: Literal["strong", "eventual"] | None = None
    enabled_if_credentials: bool = False

    @model_validator(mode="after")
    def validate_options(self) -> SearchProfileDefinition:
        if self.backend == "pg" and (
            self.layout is not None or self.consistency is not None or self.enabled_if_credentials
        ):
            raise ValueError("pg profile does not accept Turbopuffer options")
        return self


class RankDefaults(StrictModel):
    candidates: int = Field(ge=1, le=1_000)
    variants: dict[Literal["hybrid", "vector", "text", "recent"], Any]

    @model_validator(mode="after")
    def validate_variants(self) -> RankDefaults:
        required = {"hybrid", "vector", "text", "recent"}
        if set(self.variants) != required:
            raise ValueError(f"rank variants must be exactly {sorted(required)}")
        return self


ParameterType = Literal["string", "string_array", "number", "integer", "boolean", "datetime"]


class ParameterDefinition(StrictModel):
    """One public, typed parameter shared by views and artifacts.

    The definition is the single source for runtime validation and generated
    JSON Schema.  MCP declarations deliberately reference a view or artifact
    rather than repeating any of these fields.
    """

    type: ParameterType
    required: bool = False
    default: Any | None = None
    description: NonBlank | None = None
    enum: tuple[Any, ...] | None = None
    item_enum: tuple[NonBlank, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    min_items: int | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=0)

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def validate_numeric_bound(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("numeric bounds must be finite numbers")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("numeric bounds must be finite numbers")
        return result

    @field_validator("min_length", "max_length", "min_items", "max_items", mode="before")
    @classmethod
    def validate_size_bound(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("size bounds must be non-negative integers")
        return value

    @model_validator(mode="after")
    def validate_default(self) -> ParameterDefinition:
        if self.required and self.default is not None:
            raise ValueError("a required parameter cannot also declare a default")
        self._validate_constraint_applicability()
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if (
            self.min_items is not None
            and self.max_items is not None
            and self.min_items > self.max_items
        ):
            raise ValueError("min_items cannot exceed max_items")
        if self.enum is not None:
            if not self.enum:
                raise ValueError("enum must be non-empty")
            seen: set[Any] = set()
            for value in self.enum:
                if not _parameter_type_matches(self.type, value):
                    raise ValueError(f"enum value does not match parameter type {self.type!r}")
                key = _parameter_value_key(self.type, value)
                if key in seen:
                    raise ValueError("enum values must be unique")
                seen.add(key)
                if not _parameter_constraints_match(self, value, include_enum=False):
                    raise ValueError("enum value does not match parameter constraints")
        if self.item_enum is not None:
            if self.type != "string_array":
                raise ValueError("item_enum is available only for string_array")
            if not self.item_enum:
                raise ValueError("item_enum must be non-empty")
            ensure_unique(self.item_enum, "item_enum values")
        if self.default is not None and not parameter_value_matches(self, self.default):
            raise ValueError(f"default does not match parameter schema for type {self.type!r}")
        return self

    def _validate_constraint_applicability(self) -> None:
        numeric = {"number", "integer"}
        string = {"string"}
        string_array = {"string_array"}
        if self.type not in numeric and (self.minimum is not None or self.maximum is not None):
            raise ValueError("minimum and maximum are available only for number and integer")
        if self.type not in string and (self.min_length is not None or self.max_length is not None):
            raise ValueError("min_length and max_length are available only for string")
        if self.type not in string_array and (
            self.min_items is not None or self.max_items is not None
        ):
            raise ValueError("min_items and max_items are available only for string_array")
        if self.type == "integer" and any(
            bound is not None and not bound.is_integer() for bound in (self.minimum, self.maximum)
        ):
            raise ValueError("integer parameter bounds must be integers")


def _parameter_type_matches(parameter_type: ParameterType, value: Any) -> bool:
    """Return whether a value has the declared exact base type."""

    if parameter_type == "string":
        return isinstance(value, str)
    if parameter_type == "string_array":
        return isinstance(value, (list, tuple)) and all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )
    if parameter_type == "boolean":
        return isinstance(value, bool)
    if parameter_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if parameter_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _parameter_value_key(parameter_type: ParameterType, value: Any) -> Any:
    """Produce a stable comparable value after base-type validation."""

    if parameter_type == "string_array":
        return tuple(value)
    if parameter_type == "number":
        return float(value)
    if parameter_type == "datetime":
        if isinstance(value, datetime):
            return value.isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return value


def _parameter_constraints_match(
    parameter: ParameterDefinition,
    value: Any,
    *,
    include_enum: bool = True,
) -> bool:
    """Return whether a base-typed value satisfies declarative constraints."""

    if include_enum and parameter.enum is not None:
        value_key = _parameter_value_key(parameter.type, value)
        if all(
            _parameter_value_key(parameter.type, option) != value_key for option in parameter.enum
        ):
            return False
    if parameter.type in {"number", "integer"}:
        number = float(value)
        if parameter.minimum is not None and number < parameter.minimum:
            return False
        if parameter.maximum is not None and number > parameter.maximum:
            return False
    elif parameter.type == "string":
        if parameter.min_length is not None and len(value) < parameter.min_length:
            return False
        if parameter.max_length is not None and len(value) > parameter.max_length:
            return False
    elif parameter.type == "string_array":
        if parameter.min_items is not None and len(value) < parameter.min_items:
            return False
        if parameter.max_items is not None and len(value) > parameter.max_items:
            return False
        if parameter.item_enum is not None and any(
            item not in parameter.item_enum for item in value
        ):
            return False
    return True


def parameter_value_matches(
    parameter: ParameterDefinition | ParameterType,
    value: Any,
) -> bool:
    """Return whether a rendered value satisfies a parameter's full schema.

    Passing a bare ``ParameterType`` preserves the older type-only helper for
    callers outside the catalog.  New validation should pass the full
    ``ParameterDefinition`` so the same enum and bounds apply at runtime.
    """

    parameter_type = parameter.type if isinstance(parameter, ParameterDefinition) else parameter
    if not _parameter_type_matches(parameter_type, value):
        return False
    return not isinstance(parameter, ParameterDefinition) or _parameter_constraints_match(
        parameter, value
    )


def _json_schema_value(value: Any) -> Any:
    """Return a JSON-compatible value suitable for a generated schema."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_schema_value(item) for item in value]
    if isinstance(value, list):
        return [_json_schema_value(item) for item in value]
    return value


def parameter_json_schema(parameter: ParameterDefinition) -> dict[str, Any]:
    """Generate the Draft 2020-12 schema fragment for one parameter."""

    schema: dict[str, Any]
    if parameter.type == "string_array":
        items: dict[str, Any] = {"type": "string"}
        if parameter.item_enum is not None:
            items["enum"] = list(parameter.item_enum)
        schema = {"type": "array", "items": items}
    elif parameter.type == "datetime":
        schema = {"type": "string", "format": "date-time"}
    else:
        schema = {"type": parameter.type}
    if parameter.description is not None:
        schema["description"] = parameter.description
    if parameter.enum is not None:
        schema["enum"] = [_json_schema_value(value) for value in parameter.enum]
    if parameter.minimum is not None:
        schema["minimum"] = parameter.minimum
    if parameter.maximum is not None:
        schema["maximum"] = parameter.maximum
    if parameter.min_length is not None:
        schema["minLength"] = parameter.min_length
    if parameter.max_length is not None:
        schema["maxLength"] = parameter.max_length
    if parameter.min_items is not None:
        schema["minItems"] = parameter.min_items
    if parameter.max_items is not None:
        schema["maxItems"] = parameter.max_items
    if parameter.default is not None:
        schema["default"] = _json_schema_value(parameter.default)
    return schema


def parameters_json_schema(
    parameters: Mapping[str, ParameterDefinition],
) -> dict[str, Any]:
    """Generate a closed Draft 2020-12 input object schema."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            name: parameter_json_schema(parameter) for name, parameter in parameters.items()
        },
        "additionalProperties": False,
    }
    required = [name for name, parameter in parameters.items() if parameter.required]
    if required:
        schema["required"] = required
    return schema


class GraphProjection(StrictModel):
    """The canonical collections projected by a graph-derived view.

    ``subject``/``object``/``predicate`` are the default declared-field names,
    but authors may map all three roles onto another collection vocabulary.
    Keeping that mapping in the view lets one workspace expose several graphs
    without giving callers arbitrary storage access or coupling the runtime to
    an example package's collection names.
    """

    edges: PublicName
    subject: PublicName = "subject"
    object: PublicName = "object"
    predicate: PublicName = "predicate"
    nodes: PublicName | None = None


class ViewDefinition(VersionedDefinition):
    kind: Literal["search", "graph", "graph_orphans"] = "search"
    parameters: dict[PublicName, ParameterDefinition] = Field(default_factory=dict)
    query: dict[str, Any] | None = None
    graph: GraphProjection | None = None
    required_capabilities: tuple[Literal["vector", "text", "recent", "structured"], ...] = ()

    @model_validator(mode="after")
    def validate_capabilities(self) -> ViewDefinition:
        ensure_unique(self.required_capabilities, "required_capabilities")
        if self.kind == "search":
            if self.query is None:
                raise ValueError("search view requires query")
            if self.graph is not None:
                raise ValueError("search view forbids graph")
        if self.kind in {"graph", "graph_orphans"}:
            if self.query is not None:
                raise ValueError("graph-derived view forbids query")
            if self.graph is None:
                raise ValueError("graph-derived view requires graph")
            if self.required_capabilities:
                raise ValueError("graph-derived view forbids required_capabilities")
        if self.kind == "graph" and self.graph is not None and self.graph.nodes is not None:
            raise ValueError("graph view forbids graph.nodes")
        if self.kind == "graph_orphans" and self.graph is not None and self.graph.nodes is None:
            raise ValueError("graph_orphans view requires graph.nodes")
        return self


class DocumentBlock(StrictModel):
    entity: str
    collections: tuple[PublicName, ...]
    status: Literal["active", "draft", "all"] = "active"

    @model_validator(mode="after")
    def validate_collections(self) -> DocumentBlock:
        if not self.collections:
            raise ValueError("document block requires collections")
        ensure_unique(self.collections, "document collections")
        return self


class ArtifactBlock(StrictModel):
    document: DocumentBlock | None = None
    view: str | None = None
    args: dict[PublicName, Any] = Field(default_factory=dict)
    max_tokens: int = Field(ge=1)
    required: bool = True

    @model_validator(mode="after")
    def validate_source(self) -> ArtifactBlock:
        if (self.document is None) == (self.view is None):
            raise ValueError("artifact block requires exactly one of document or view")
        if self.document is not None and self.args:
            raise ValueError("document block cannot declare view args")
        return self


class ArtifactSnapshot(StrictModel):
    entity: str | None = None
    collection: PublicName
    type: PublicName
    key: str = Field(min_length=1, max_length=128)


class ArtifactLearning(StrictModel):
    """Which maintained component feedback about this render should improve.

    A composed prompt draws on several maintained values, so the client that
    reports an outcome cannot reasonably choose one.  The author names the
    block whose reviewed value is the improvement target, plus the exact
    reviewed artifact that owns that value's promotion lifecycle.  Rendering
    resolves the declaration to the exact keyed heads that were in force, so a
    candidate is always based on the version that influenced the execution.
    """

    target_block: PublicName
    artifact: str

    @model_validator(mode="after")
    def validate_reference(self) -> ArtifactLearning:
        _require_exact_definition_reference(self.artifact, "learning artifact")
        return self


class ArtifactDefinition(VersionedDefinition):
    kind: Literal["prompt", "skill", "profile", "policy"]
    # One line describing when this artifact applies.  A skill is disclosed to a
    # model by name and description *before* its body is loaded, so a skill that
    # is offered as a tool must carry one; everywhere else it is documentation.
    description: NonBlank | None = None
    lifecycle: Literal["live", "reviewed"]
    parameters: dict[PublicName, ParameterDefinition] = Field(default_factory=dict)
    blocks: dict[PublicName, ArtifactBlock]
    template: str
    snapshot: ArtifactSnapshot | None = None
    candidate_processor: ProcessorName | None = None
    complete_keys: tuple[str, ...] = ()
    learning: ArtifactLearning | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        dumped = dict(handler(self))
        if self.description is None:
            dumped.pop("description", None)
        return dumped

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ArtifactDefinition:
        if not self.blocks:
            raise ValueError("artifact requires at least one block")
        ensure_unique(self.complete_keys, "complete_keys")
        if self.lifecycle == "reviewed":
            if self.candidate_processor is None or not self.complete_keys:
                raise ValueError("reviewed artifact requires candidate_processor and complete_keys")
        elif self.candidate_processor is not None or self.complete_keys:
            raise ValueError("live artifact forbids candidate_processor and complete_keys")
        if self.learning is not None and self.learning.target_block not in self.blocks:
            raise ValueError(
                f"learning.target_block names no block: {self.learning.target_block!r}"
            )
        return self


ComputerRuntimeName = Literal["worker-javascript", "container"]
ComputerCapabilityName = Literal["filesystem", "exec", "network"]


def _absolute_computer_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or value == "/":
        raise ValueError(f"{label} must be a safe absolute path below root")
    return str(path)


def _relative_program_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ValueError(f"{label} must be a safe relative path")
    return str(path)


class ComputerContextMount(StrictModel):
    path: str
    artifact: str
    mode: Literal["read_only"] = "read_only"

    @model_validator(mode="after")
    def validate_mount(self) -> ComputerContextMount:
        _absolute_computer_path(self.path, "context path")
        if not self.path.startswith("/.memseek/"):
            raise ValueError("context paths must live below /.memseek")
        _require_exact_definition_reference(self.artifact, "context artifact")
        return self


class ComputerRuntime(StrictModel):
    default: ComputerRuntimeName = "worker-javascript"
    fallback: ComputerRuntimeName | None = None
    fallback_requires: Literal["explicit_policy"] | None = None

    @model_validator(mode="after")
    def validate_fallback(self) -> ComputerRuntime:
        if self.fallback == self.default:
            raise ValueError("runtime fallback must differ from default")
        if (self.fallback is None) != (self.fallback_requires is None):
            raise ValueError("runtime fallback and fallback_requires must be declared together")
        return self


class ComputerCapabilities(StrictModel):
    filesystem: bool = True
    exec: bool = False
    network: bool = False

    @property
    def enabled(self) -> frozenset[str]:
        return frozenset(
            name for name in ("filesystem", "exec", "network") if bool(getattr(self, name))
        )


class ComputerWriteback(StrictModel):
    path: str
    type: Literal["observations", "maintained_state", "final_result"]
    review: bool
    collection: str | None = None
    record_type: PublicName | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        dumped = dict(handler(self))
        if self.collection is None:
            dumped.pop("collection", None)
        if self.record_type is None:
            dumped.pop("record_type", None)
        return dumped

    @model_validator(mode="after")
    def validate_writeback(self) -> ComputerWriteback:
        _absolute_computer_path(self.path, "writeback path")
        if not self.path.startswith("/outbox/"):
            raise ValueError("writeback paths must live below /outbox")
        if self.type == "maintained_state" and not self.review:
            raise ValueError("maintained_state writeback requires review")
        if self.type == "observations" and self.review:
            raise ValueError("observations writeback cannot require review")
        if self.type == "final_result":
            if self.collection is not None or self.record_type is not None:
                raise ValueError("final_result writeback forbids collection and record_type")
        else:
            if self.collection is None or self.record_type is None:
                raise ValueError(
                    "observations and maintained_state writeback require collection and record_type"
                )
            _require_exact_definition_reference(self.collection, "writeback collection")
        return self


class ComputerRetention(StrictModel):
    workspace_days: int = Field(default=30, ge=1, le=365)
    preserve: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_preserve(self) -> ComputerRetention:
        ensure_unique(self.preserve, "preserve paths")
        for value in self.preserve:
            _absolute_computer_path(value, "preserve path")
        return self


class ComputerDefinition(VersionedDefinition):
    """Reusable execution policy; physical instances are session scoped."""

    provider: ProviderName
    context: tuple[ComputerContextMount, ...] = ()
    writable: tuple[str, ...] = ("/workspace", "/outbox")
    runtime: ComputerRuntime = Field(default_factory=ComputerRuntime)
    capabilities: ComputerCapabilities = Field(default_factory=ComputerCapabilities)
    writeback: tuple[ComputerWriteback, ...] = ()
    retention: ComputerRetention = Field(default_factory=ComputerRetention)

    @model_validator(mode="after")
    def validate_computer(self) -> ComputerDefinition:
        ensure_unique([item.path for item in self.context], "computer context paths")
        ensure_unique(self.writable, "computer writable paths")
        ensure_unique([item.path for item in self.writeback], "computer writeback paths")
        for value in self.writable:
            _absolute_computer_path(value, "writable path")
            if value.startswith(("/.memseek", "/inputs")):
                raise ValueError("/.memseek and /inputs are always read-only")
        for item in self.writeback:
            if not any(
                item.path == root or item.path.startswith(f"{root}/") for root in self.writable
            ):
                raise ValueError(f"writeback path {item.path!r} is outside writable roots")
        return self


class ExternalProgramBundle(StrictModel):
    uri: NonBlank
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProgramDefinition(VersionedDefinition):
    """Immutable executable bundle accepted by a Computer provider."""

    runtime: ComputerRuntimeName
    entrypoint: str
    command: tuple[NonBlank, ...] | None = None
    files: dict[str, str] = Field(default_factory=dict)
    bundle: ExternalProgramBundle | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: tuple[ComputerCapabilityName, ...] = ("filesystem",)

    @model_validator(mode="after")
    def validate_program(self) -> ProgramDefinition:
        entrypoint = _relative_program_path(self.entrypoint, "program entrypoint")
        if bool(self.files) == (self.bundle is not None):
            raise ValueError("program requires exactly one of files or bundle")
        if self.files:
            normalized: set[str] = set()
            total = 0
            for raw_path, content in self.files.items():
                path = _relative_program_path(raw_path, "program file path")
                if path in normalized:
                    raise ValueError(f"duplicate normalized program path {path!r}")
                normalized.add(path)
                total += len(content.encode("utf-8"))
            if entrypoint not in normalized:
                raise ValueError("program entrypoint must name an embedded file")
            if total > 1_048_576:
                raise ValueError("embedded program bundle exceeds 1 MiB")
        if self.runtime == "container" and not self.command:
            raise ValueError("container programs require an immutable command argv")
        if self.runtime != "container" and self.command is not None:
            raise ValueError("command is only valid for container programs")
        ensure_unique(self.capabilities, "program capabilities")
        if "exec" not in self.capabilities and self.runtime == "container":
            raise ValueError("container programs require the exec capability")
        return self


class AgentLimits(StrictModel):
    max_steps: int = Field(default=32, ge=1, le=128)
    max_wall_s: int = Field(default=300, ge=1, le=3_600)
    max_output_bytes: int = Field(default=10_485_760, ge=1, le=67_108_864)


class AgentDefinition(VersionedDefinition):
    model: ProcessorName
    instructions: str
    skills: tuple[str, ...] = ()
    tools: tuple[Literal["computer", "recall"], ...] = ("computer", "recall")
    # The declared tool surface.  `tools` and `skills` are the pre-toolset
    # spelling of the same fact, kept working so published catalogs stay valid.
    toolset: str | None = None
    computers: tuple[str, ...]
    context_policy: str
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        dumped = dict(handler(self))
        if self.toolset is None:
            dumped.pop("toolset", None)
        return dumped

    @model_validator(mode="after")
    def validate_agent(self) -> AgentDefinition:
        _require_exact_definition_reference(self.instructions, "agent instructions artifact")
        for skill in self.skills:
            _require_exact_definition_reference(skill, "agent skill artifact")
        for computer in self.computers:
            _require_exact_definition_reference(computer, "agent computer")
        _require_exact_definition_reference(self.context_policy, "agent context policy")
        ensure_unique(self.skills, "agent skills")
        ensure_unique(self.tools, "agent tools")
        ensure_unique(self.computers, "agent computers")
        if not self.computers:
            raise ValueError("agent requires at least one allowed computer")
        if self.toolset is not None:
            _require_exact_definition_reference(self.toolset, "agent toolset")
            # Declaring both would give an Agent two tool surfaces and no rule
            # for which wins.  `model_fields_set` is what distinguishes "the
            # author wrote the default" from "the author wrote nothing".
            declared = self.model_fields_set & {"tools", "skills"}
            if declared:
                raise ValueError(f"agent toolset is exclusive with {sorted(declared)}")
        return self


class ContextThresholds(StrictModel):
    pointerize: float = Field(default=0.70, gt=0, lt=1)
    compact: float = Field(default=0.82, gt=0, lt=1)
    pause: float = Field(default=0.92, gt=0, lt=1)

    @model_validator(mode="after")
    def validate_order(self) -> ContextThresholds:
        if not self.pointerize < self.compact < self.pause:
            raise ValueError("context thresholds must be strictly increasing")
        return self


class ContextPolicyDefinition(VersionedDefinition):
    max_input_tokens: int = Field(default=50_000, ge=4_096)
    reserve_output_tokens: int = Field(default=4_000, ge=256)
    thresholds: ContextThresholds = Field(default_factory=ContextThresholds)
    max_recall_pages: int = Field(default=5, ge=1, le=20)
    max_recall_hits: int = Field(default=50, ge=1, le=200)
    max_exposed_bytes: int = Field(default=262_144, ge=1, le=4_194_304)
    receipt_fanout: int = Field(default=8, ge=2, le=32)

    @model_validator(mode="after")
    def validate_budget(self) -> ContextPolicyDefinition:
        if self.reserve_output_tokens >= self.max_input_tokens:
            raise ValueError("reserve_output_tokens must be below max_input_tokens")
        return self


class TombstoneRetention(StrictModel):
    """A bounded scheduled physical-erasure policy for keyed tombstones.

    The collection reference is deliberately exact so a retention rule cannot
    silently start applying to a newly active collection version.
    """

    name: PublicName
    collection: str
    after_days: int = Field(ge=1, le=3_650)
    cron: str = Field(min_length=1)
    max_pages: int = Field(default=25, ge=1, le=100)


class McpToolDefinition(StrictModel):
    """One explicitly exposed operation in a package MCP interface.

    A tool can bind only an existing named view or artifact.  The fixed
    ``answer`` and ``record`` kinds deliberately have no arbitrary target or
    transport configuration: their server adapters remain the sole authority
    over those operations.
    """

    name: PublicName
    kind: Literal["view", "artifact", "answer", "record", "ingest", "invocation"]
    description: NonBlank
    title: NonBlank | None = None
    view: str | None = None
    artifact: str | None = None
    # `ingest` is the one writing kind, so the collection it may append to is
    # named here rather than chosen by the caller: an agent can add evidence to
    # exactly the drawer the package opened for it, and to no other.
    collection: str | None = None
    computer: str | None = None
    agent: str | None = None
    program: str | None = None
    context_policy: str | None = None
    invocation_task: Literal["answer", "task", "compute"] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        """Omit ``collection`` unless this tool actually binds one.

        ``definition_hash`` covers the whole dumped definition, and a catalog is
        read back by recompiling its stored YAML and checking the hash still
        matches. Emitting a null for a field an existing interface never
        declared would restate the identity of every previously published
        catalog, and every one of them would fail that check with a 503. A new
        optional field must therefore be invisible until it is used.
        """

        dumped = dict(handler(self))
        if self.collection is None:
            dumped.pop("collection", None)
        for field_name in ("computer", "agent", "program", "context_policy", "invocation_task"):
            if getattr(self, field_name) is None:
                dumped.pop(field_name, None)
        return dumped

    @model_validator(mode="after")
    def validate_target(self) -> McpToolDefinition:
        invocation_fields = (
            self.computer,
            self.agent,
            self.program,
            self.context_policy,
            self.invocation_task,
        )
        if self.kind == "view":
            if self.view is None:
                raise ValueError("view MCP tool requires view")
            if (
                self.artifact is not None
                or self.collection is not None
                or any(value is not None for value in invocation_fields)
            ):
                raise ValueError("view MCP tool forbids other resource bindings")
            _require_exact_definition_reference(self.view, "view")
        elif self.kind == "artifact":
            if self.artifact is None:
                raise ValueError("artifact MCP tool requires artifact")
            if (
                self.view is not None
                or self.collection is not None
                or any(value is not None for value in invocation_fields)
            ):
                raise ValueError("artifact MCP tool forbids other resource bindings")
            _require_exact_definition_reference(self.artifact, "artifact")
        elif self.kind == "ingest":
            if self.collection is None:
                raise ValueError("ingest MCP tool requires collection")
            if (
                self.view is not None
                or self.artifact is not None
                or any(value is not None for value in invocation_fields)
            ):
                raise ValueError("ingest MCP tool forbids other resource bindings")
            _require_exact_definition_reference(self.collection, "collection")
        elif self.kind == "invocation":
            if self.computer is None or self.invocation_task is None:
                raise ValueError("invocation MCP tool requires computer and invocation_task")
            if (self.agent is None) == (self.program is None):
                raise ValueError("invocation MCP tool requires exactly one of agent or program")
            if (self.agent is None) != (self.context_policy is None):
                raise ValueError("agent invocation requires context_policy; Program forbids it")
            if self.agent is not None and self.invocation_task == "compute":
                raise ValueError("agent invocation task must be answer or task")
            if self.program is not None and self.invocation_task != "compute":
                raise ValueError("Program invocation task must be compute")
            _require_exact_definition_reference(self.computer, "computer")
            if self.agent is not None:
                _require_exact_definition_reference(self.agent, "agent")
                assert self.context_policy is not None
                _require_exact_definition_reference(self.context_policy, "context_policy")
            else:
                assert self.program is not None
                _require_exact_definition_reference(self.program, "program")
            if self.view is not None or self.artifact is not None or self.collection is not None:
                raise ValueError("invocation MCP tool forbids view, artifact and collection")
        elif any(
            value is not None
            for value in (
                self.view,
                self.artifact,
                self.collection,
                self.computer,
                self.agent,
                self.program,
                self.context_policy,
                self.invocation_task,
            )
        ):
            raise ValueError(f"{self.kind} MCP tool forbids resource bindings")
        return self


class McpDefinition(DefinitionModel):
    """A versioned, curated MCP surface owned by one package.

    Unlike views and artifacts, MCP definitions do not have an active alias:
    a package always selects one exact interface version.
    """

    version: int = Field(ge=1)
    title: NonBlank | None = None
    instructions: NonBlank | None = None
    tools: tuple[McpToolDefinition, ...]

    @model_validator(mode="after")
    def validate_tools(self) -> McpDefinition:
        ensure_unique([tool.name for tool in self.tools], "MCP tool names")
        return self


def _require_exact_definition_reference(reference: str, kind: str) -> None:
    try:
        split_exact_reference(reference)
    except ValueError as exc:
        raise ValueError(f"{kind} must be an exact name@version reference") from exc


ToolSourceKind = Literal["filesystem", "exec", "recall", "writeback", "skill", "view", "mcp_server"]
FilesystemMode = Literal["read", "ls", "find", "grep", "write", "edit", "delete"]

# What each source kind requires and what it may additionally carry.  Stating
# the exclusivity rule once as data keeps seven kinds from becoming seven
# near-identical branches; the checks that are genuinely per-kind — exact
# references, path prefixes, URL scheme — stay explicit in the validator.
_TOOL_SOURCE_FIELDS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "filesystem": (frozenset({"modes"}), frozenset({"root"})),
    "exec": (frozenset(), frozenset()),
    "recall": (frozenset(), frozenset()),
    "writeback": (frozenset({"path"}), frozenset({"commit"})),
    "skill": (frozenset({"artifact"}), frozenset({"skill_name"})),
    "view": (frozenset({"view"}), frozenset({"arguments", "mode"})),
    "mcp_server": (frozenset({"url"}), frozenset({"allowed_tools"})),
}
_TOOL_SOURCE_BINDINGS = frozenset(
    {
        "root",
        "modes",
        "path",
        "commit",
        "artifact",
        "skill_name",
        "view",
        "arguments",
        "mode",
        "url",
        "allowed_tools",
    }
)
# Two sources of one of these kinds would be two identically-behaving tools the
# model has to choose between.  Skills are deliberately absent: many skill
# sources produce one tool with many choices, not many tools.
_SINGLETON_TOOL_KINDS = frozenset({"filesystem", "exec", "recall"})


def _derived_skill_name(reference: str) -> str:
    """The skill name implied by an artifact reference.

    Artifact names may carry dots and underscores; a skill name may not, because
    it becomes a path segment and a value the model types.  Where the mapping is
    not obvious the author declares ``skill_name`` instead.
    """

    name, _ = split_exact_reference(reference)
    return name.replace("_", "-").replace(".", "-")


class ToolSourceDefinition(StrictModel):
    """One capability an Agent may reach through its toolset.

    A source is a *binding*, not an implementation: it names the exact catalog
    definition a tool operates on, so the surface an Agent sees is auditable
    from the catalog alone.  Nothing here is implicit — a filesystem source that
    does not list ``write`` grants no write tool — because the alternative is a
    surface that silently widens when a dependency adds a tool.
    """

    name: PublicName
    kind: ToolSourceKind
    # Required for every kind except `skill`.  A skill's description belongs to
    # the skill: two toolsets that each described the same skill would be two
    # different promises about one body of text.
    description: NonBlank | None = None
    root: str | None = None
    modes: tuple[FilesystemMode, ...] | None = None
    path: str | None = None
    commit: Literal["outbox", "staged"] | None = None
    artifact: str | None = None
    skill_name: SkillName | None = None
    view: str | None = None
    arguments: dict[PublicName, str | int | float | bool] | None = None
    mode: Literal["snapshot", "live"] | None = None
    url: str | None = None
    allowed_tools: tuple[PublicName, ...] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        """Omit every binding this source does not use.

        Same reason as ``McpToolDefinition._serialize``: a definition's identity
        covers its whole dumped form, so a null for a field this source never
        declared would restate it.
        """

        dumped = dict(handler(self))
        for field_name in (*_TOOL_SOURCE_BINDINGS, "description"):
            if getattr(self, field_name) is None:
                dumped.pop(field_name, None)
        return dumped

    @model_validator(mode="after")
    def validate_source(self) -> ToolSourceDefinition:
        required, optional = _TOOL_SOURCE_FIELDS[self.kind]
        for field_name in _TOOL_SOURCE_BINDINGS:
            declared = getattr(self, field_name) is not None
            if field_name in required and not declared:
                raise ValueError(f"{self.kind} tool source requires {field_name}")
            if declared and field_name not in required and field_name not in optional:
                raise ValueError(f"{self.kind} tool source forbids {field_name}")
        if self.kind == "skill":
            if self.description is not None:
                raise ValueError(
                    "skill tool source forbids description; it belongs on the artifact"
                )
            assert self.artifact is not None
            _require_exact_definition_reference(self.artifact, "tool source artifact")
            if self.skill_name is None:
                _validate_derived_skill_name(self.artifact)
        elif self.description is None:
            raise ValueError(f"{self.kind} tool source requires description")
        if self.kind == "filesystem":
            assert self.modes is not None
            if not self.modes:
                raise ValueError("filesystem tool source requires at least one mode")
            ensure_unique(self.modes, "filesystem modes")
            if self.root is not None:
                _absolute_computer_path(self.root, "tool source root")
        if self.kind == "writeback":
            assert self.path is not None
            _absolute_computer_path(self.path, "tool source path")
            if not self.path.startswith("/outbox/"):
                raise ValueError("writeback tool sources must name a path below /outbox")
            if self.commit == "staged":
                # Staged writes need the host tool channel, which does not exist
                # yet.  Declaring the value early would promise a durability the
                # runtime cannot deliver, so it is refused until the channel and
                # its promotion step land together.
                raise ValueError("writeback commit 'staged' requires the host tool channel")
        if self.kind == "view":
            assert self.view is not None
            _require_exact_definition_reference(self.view, "tool source view")
            if self.mode == "live":
                raise ValueError("view mode 'live' requires the host tool channel")
        if self.kind == "mcp_server":
            assert self.url is not None
            _validate_endpoint_url(self.url)
            if self.allowed_tools is not None:
                ensure_unique(self.allowed_tools, "allowed_tools")
        return self

    @property
    def resolved_skill_name(self) -> str:
        assert self.kind == "skill"
        assert self.artifact is not None
        return self.skill_name or _derived_skill_name(self.artifact)


def _validate_derived_skill_name(reference: str) -> None:
    derived = _derived_skill_name(reference)
    if not re.fullmatch(SKILL_NAME_PATTERN, derived):
        raise ValueError(
            f"artifact {reference!r} implies invalid skill name {derived!r}; declare skill_name"
        )


class ToolsetDefinition(DefinitionModel):
    """The inbound tool surface one Agent may consume.

    The counterpart to :class:`McpDefinition`.  That one publishes memseek's own
    operations *outward* to external clients; this one enumerates, exactly, what
    an Agent running inside a Computer may reach.  Like an MCP interface it has
    no active alias: an Agent binds one exact surface, because a tool set that
    could change under a pinned Agent is not a surface at all.
    """

    version: int = Field(ge=1)
    title: NonBlank | None = None
    instructions: NonBlank | None = None
    sources: tuple[ToolSourceDefinition, ...]

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        dumped = dict(handler(self))
        for field_name in ("title", "instructions"):
            if getattr(self, field_name) is None:
                dumped.pop(field_name, None)
        return dumped

    @model_validator(mode="after")
    def validate_sources(self) -> ToolsetDefinition:
        if not self.sources:
            raise ValueError("toolset requires at least one source")
        ensure_unique([source.name for source in self.sources], "toolset source names")
        for kind in sorted(_SINGLETON_TOOL_KINDS):
            if sum(source.kind == kind for source in self.sources) > 1:
                raise ValueError(f"toolset declares more than one {kind} source")
        ensure_unique(
            [source.resolved_skill_name for source in self.sources if source.kind == "skill"],
            "toolset skill names",
        )
        ensure_unique(
            [source.path for source in self.sources if source.kind == "writeback"],
            "toolset writeback paths",
        )
        return self


class PackageDefinition(DefinitionModel):
    version: SemVer
    collections: tuple[str, ...] = ()
    processors: tuple[ProcessorName, ...] = ()
    triggers: tuple[str, ...] = ()
    views: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    computers: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    context_policies: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    search_profiles: tuple[PublicName, ...] = ()
    optional_search_profiles: tuple[PublicName, ...] = ()
    retentions: tuple[TombstoneRetention, ...] = ()
    # Packages without this opt-in deliberately expose no MCP tools.  This
    # permits existing catalogs to remain valid while preserving an explicit,
    # curatable MCP surface for every package that does define one.
    mcp: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        """Omit ``toolsets`` until a package declares one.

        NOTE: ``mcp`` is deliberately *not* popped here.  It has dumped as an
        explicit null since it was added, and every package hash published since
        then covers that null.  Popping it now would restate all of them, which
        is the exact failure this method exists to prevent.
        """

        dumped = dict(handler(self))
        if not self.toolsets:
            dumped.pop("toolsets", None)
        return dumped

    @model_validator(mode="after")
    def validate_manifest(self) -> PackageDefinition:
        for field_name in (
            "collections",
            "processors",
            "triggers",
            "views",
            "artifacts",
            "computers",
            "programs",
            "agents",
            "context_policies",
            "toolsets",
            "search_profiles",
            "optional_search_profiles",
        ):
            ensure_unique(getattr(self, field_name), field_name)
        overlap = set(self.search_profiles) & set(self.optional_search_profiles)
        if overlap:
            raise ValueError(f"search profiles cannot be required and optional: {sorted(overlap)}")
        ensure_unique([retention.name for retention in self.retentions], "retention names")
        return self


class DeploymentOverrides(StrictModel):
    collection_profiles: dict[PublicName, PublicName] = Field(default_factory=dict)
