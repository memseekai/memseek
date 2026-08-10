"""Immutable Pydantic models for the startup definition catalog."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_serializer, model_validator

from .base import (
    DefinitionModel,
    EmbeddingSpace,
    EnvVarName,
    NonBlank,
    ProcessorName,
    ProviderName,
    PublicName,
    SemVer,
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
    lifecycle: Literal["live", "reviewed"]
    parameters: dict[PublicName, ParameterDefinition] = Field(default_factory=dict)
    blocks: dict[PublicName, ArtifactBlock]
    template: str
    snapshot: ArtifactSnapshot | None = None
    candidate_processor: ProcessorName | None = None
    complete_keys: tuple[str, ...] = ()
    learning: ArtifactLearning | None = None

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
    kind: Literal["view", "artifact", "answer", "record", "ingest"]
    description: NonBlank
    title: NonBlank | None = None
    view: str | None = None
    artifact: str | None = None
    # `ingest` is the one writing kind, so the collection it may append to is
    # named here rather than chosen by the caller: an agent can add evidence to
    # exactly the drawer the package opened for it, and to no other.
    collection: str | None = None

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
        return dumped

    @model_validator(mode="after")
    def validate_target(self) -> McpToolDefinition:
        if self.kind == "view":
            if self.view is None:
                raise ValueError("view MCP tool requires view")
            if self.artifact is not None or self.collection is not None:
                raise ValueError("view MCP tool forbids artifact and collection")
            _require_exact_definition_reference(self.view, "view")
        elif self.kind == "artifact":
            if self.artifact is None:
                raise ValueError("artifact MCP tool requires artifact")
            if self.view is not None or self.collection is not None:
                raise ValueError("artifact MCP tool forbids view and collection")
            _require_exact_definition_reference(self.artifact, "artifact")
        elif self.kind == "ingest":
            if self.collection is None:
                raise ValueError("ingest MCP tool requires collection")
            if self.view is not None or self.artifact is not None:
                raise ValueError("ingest MCP tool forbids view and artifact")
            _require_exact_definition_reference(self.collection, "collection")
        elif self.view is not None or self.artifact is not None or self.collection is not None:
            raise ValueError(f"{self.kind} MCP tool forbids view, artifact and collection")
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


class PackageDefinition(DefinitionModel):
    version: SemVer
    collections: tuple[str, ...] = ()
    processors: tuple[ProcessorName, ...] = ()
    triggers: tuple[str, ...] = ()
    views: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    search_profiles: tuple[PublicName, ...] = ()
    optional_search_profiles: tuple[PublicName, ...] = ()
    retentions: tuple[TombstoneRetention, ...] = ()
    # Packages without this opt-in deliberately expose no MCP tools.  This
    # permits existing catalogs to remain valid while preserving an explicit,
    # curatable MCP surface for every package that does define one.
    mcp: str | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> PackageDefinition:
        for field_name in (
            "collections",
            "processors",
            "triggers",
            "views",
            "artifacts",
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
