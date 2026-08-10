"""Deterministic startup loader for the immutable definition graph."""

from __future__ import annotations

import json
import tempfile
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from types import MappingProxyType
from typing import Any

import yaml
from croniter import croniter
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from memseek.config import Settings
from memseek.derive.emission import emission_status
from memseek.derive.schema import (
    RUN_TEMPLATE_KEYS,
    CurrentSource,
    PipelineDefinition,
    RecordSource,
    StandaloneTrigger,
    StreamSource,
    TriggerConditions,
    ViewSource,
)
from memseek.derive.tasks import (
    LLMTaskConfig,
    SearchTaskConfig,
    TemplateTaskConfig,
    import_task_modules,
    task_adapter,
    task_implementation_hashes,
)
from memseek.llm.registry import provider_descriptor, validate_generation_params
from memseek.search.rank import RankValidationError, validate_rank_expression
from memseek.search.registry import backend_descriptor, required_capabilities
from memseek.search.scope import field_annotation_names
from memseek.search.spec import SearchMode, SearchSource, SearchSpec
from memseek.templates import TemplateError, require_known_references, template_references

from .base import DefinitionModel, deep_freeze, split_exact_reference
from .errors import CollectionDefinitionMismatch, DefinitionError
from .hashing import (
    canonical_json,
    collection_contract_hash,
    dump_definition,
    sha256_canonical,
)
from .models import (
    ArtifactDefinition,
    CollectionDefinition,
    DeclaredField,
    DeploymentOverrides,
    McpDefinition,
    ModelAlias,
    ModelCatalog,
    PackageDefinition,
    ParameterDefinition,
    ProcessorDefinition,
    RankDefaults,
    SearchProfileDefinition,
    ViewDefinition,
    parameter_value_matches,
)
from .yaml import load_yaml_file, yaml_files


def _catalog_files(path: Path | None) -> tuple[Path, ...]:
    """Return one legacy catalog file or a deterministic directory of fragments."""

    if path is None:
        return ()
    if path.is_dir():
        return yaml_files(path)
    return (path,)


def _optional_yaml_files(directory: Path | None) -> tuple[Path, ...]:
    """Definition files for a catalog section, or none when it is unconfigured.

    ``None`` means the deployment ships no definitions of that kind, which is
    the default: a workspace's catalog arrives by being published, not by being
    found on disk. A configured directory that is missing still raises, so a
    typo in a path is never mistaken for "there is nothing here".
    """

    if directory is None:
        return ()
    return yaml_files(directory)


def _programmatic_value(value: Any) -> Any:
    """Convert a Python-authored definition into strict JSON-compatible data."""

    if isinstance(value, BaseModel):
        return _programmatic_value(
            value.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude_defaults=True,
            )
        )
    if isinstance(value, Mapping):
        return {str(key): _programmatic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_programmatic_value(item) for item in value]
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TypeError("programmatic definitions must contain finite JSON values") from exc


@dataclass(frozen=True, slots=True)
class DefinitionSources:
    """Python-authored definition inputs for the normal catalog compiler.

    The source mirrors the repository layout, but contains Pydantic models (or
    JSON-compatible mappings) instead of YAML files.  It is intentionally a
    source object rather than a second validation implementation: definitions
    are materialized into an isolated temporary layout and pass through the
    exact same duplicate-key, schema, reference, budget, graph, and hashing
    checks as the shipped YAML catalog.
    """

    models: BaseModel | Mapping[str, Any]
    processors: tuple[BaseModel | Mapping[str, Any], ...]
    rank_defaults: BaseModel | Mapping[str, Any]
    search_profiles: Mapping[str, BaseModel | Mapping[str, Any]]
    collections: tuple[BaseModel | Mapping[str, Any], ...]
    derivations: tuple[BaseModel | Mapping[str, Any], ...]
    views: tuple[BaseModel | Mapping[str, Any], ...]
    artifacts: tuple[BaseModel | Mapping[str, Any], ...]
    packages: tuple[BaseModel | Mapping[str, Any], ...]
    mcps: tuple[BaseModel | Mapping[str, Any], ...] = ()
    triggers: tuple[BaseModel | Mapping[str, Any], ...] = ()
    deployment_overrides: BaseModel | Mapping[str, Any] | None = None

    @classmethod
    def from_catalog(cls, catalog: DefinitionCatalog) -> DefinitionSources:
        """Create editable Python inputs from a loaded immutable catalog.

        Inline triggers are already owned by their derivation and are omitted;
        standalone trigger definitions remain explicit.
        """

        inline = {f"{name}.default" for name in catalog.derivations}
        return cls(
            models=catalog.models,
            processors=tuple(catalog.processors.values()),
            rank_defaults=catalog.rank_defaults,
            search_profiles=dict(catalog.search_profiles),
            collections=tuple(catalog.collections.values()),
            derivations=tuple(catalog.derivations.values()),
            views=tuple(catalog.views.values()),
            artifacts=tuple(catalog.artifacts.values()),
            packages=tuple(catalog.packages.values()),
            mcps=tuple(catalog.mcps.values()),
            triggers=tuple(
                trigger for name, trigger in catalog.triggers.items() if name not in inline
            ),
            deployment_overrides={"collection_profiles": dict(catalog.deployment_bindings)},
        )

    def compile(self, settings: Settings) -> DefinitionCatalog:
        """Compile these Python definitions with the canonical catalog validator."""

        root = Path(tempfile.mkdtemp(prefix="memseek-python-catalog-"))
        try:
            conf = root / "conf"
            for directory in (
                conf,
                root / "collections",
                root / "derivations",
                root / "triggers",
                root / "views",
                root / "artifacts",
                root / "mcp",
                root / "packages",
            ):
                directory.mkdir(parents=True, exist_ok=True)

            def write(path: Path, value: Any) -> None:
                path.write_text(
                    yaml.safe_dump(_programmatic_value(value), sort_keys=False),
                    encoding="utf-8",
                )

            write(conf / "models.yaml", self.models)
            write(conf / "processors.yaml", {"processors": list(self.processors)})
            write(conf / "rank_default.yaml", self.rank_defaults)
            profiles: dict[str, Any] = {}
            for name, profile in sorted(self.search_profiles.items()):
                raw = _programmatic_value(profile)
                if isinstance(raw, dict):
                    raw.pop("name", None)
                profiles[name] = raw
            write(conf / "search_profiles.yaml", {"profiles": profiles})
            write(root / "collections" / "python.yaml", {"collections": list(self.collections)})
            for definition in self.derivations:
                name = _programmatic_value(definition).get("name")
                write(root / "derivations" / f"{name}.yaml", definition)
            for trigger in self.triggers:
                name = _programmatic_value(trigger).get("name")
                write(root / "triggers" / f"{name}.yaml", trigger)
            write(root / "views" / "python.yaml", {"views": list(self.views)})
            write(root / "artifacts" / "python.yaml", {"artifacts": list(self.artifacts)})
            for definition in self.mcps:
                name = _programmatic_value(definition).get("name")
                write(root / "mcp" / f"{name}.yaml", definition)
            write(root / "packages" / "python.yaml", {"packages": list(self.packages)})

            overrides_path: Path | None = None
            if self.deployment_overrides is not None:
                overrides_path = conf / "deployment_overrides.yaml"
                write(overrides_path, self.deployment_overrides)
            compiled_settings = settings.model_copy(
                update={
                    "models_file": conf / "models.yaml",
                    "processors_file": conf / "processors.yaml",
                    "rank_default_file": conf / "rank_default.yaml",
                    "search_profiles_file": conf / "search_profiles.yaml",
                    "collections_dir": root / "collections",
                    "derivations_dir": root / "derivations",
                    "triggers_dir": root / "triggers",
                    "views_dir": root / "views",
                    "artifacts_dir": root / "artifacts",
                    "mcp_dir": root / "mcp",
                    "packages_dir": root / "packages",
                    "search_profile_overrides_file": overrides_path,
                }
            )
            return _CatalogBuilder(compiled_settings).build()
        finally:
            rmtree(root, ignore_errors=True)


_dump = dump_definition


def _hashed[TDefinition: DefinitionModel](definition: TDefinition) -> TDefinition:
    """Inject a definition's identity hashes.

    Every definition gets ``definition_hash`` over its whole normalized form.  A
    collection additionally gets ``contract_hash`` over its record contract only;
    that narrower hash is what records persist, so binding edits never strand
    stored rows.
    """

    update: dict[str, Any] = {"definition_hash": sha256_canonical(_dump(definition, semantic=True))}
    if isinstance(definition, CollectionDefinition):
        update["contract_hash"] = collection_contract_hash(definition)
    return definition.model_copy(update=update)


def _path_from_pydantic(error: Mapping[str, Any]) -> str:
    path = ""
    for part in error.get("loc", ()):
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += ("." if path else "") + str(part)
    return path


def _parse[T: BaseModel](model: type[T], raw: Any, path: Path, *, context: str = "") -> T:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        field = _path_from_pydantic(error)
        dotted = ".".join(part for part in (context, field) if part)
        raise DefinitionError(
            "schema",
            error["msg"],
            file=path,
            path=dotted,
        ) from exc
    except ValueError as exc:
        raise DefinitionError("schema", str(exc), file=path, path=context) from exc


def _mapping(raw: Any, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DefinitionError("shape", f"{label} must be a mapping", file=path)
    return raw


def _sequence(root: dict[str, Any], key: str, path: Path) -> list[Any]:
    if set(root) != {key}:
        raise DefinitionError("shape", f"root must contain exactly {key!r}", file=path, path=key)
    value = root[key]
    if not isinstance(value, list):
        raise DefinitionError("shape", f"{key} must be a list", file=path, path=key)
    return value


def _check_json_schema(schema: dict[str, Any], path: Path, field: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        dotted = ".".join(str(part) for part in exc.absolute_schema_path)
        raise DefinitionError(
            "json_schema",
            exc.message,
            file=path,
            path=".".join(part for part in (field, dotted) if part),
        ) from exc


@dataclass(frozen=True, slots=True)
class DefinitionCatalog:
    """One immutable, fully resolved startup snapshot."""

    models: ModelCatalog
    processors: Mapping[str, ProcessorDefinition]
    score_names: frozenset[str]
    score_owners: Mapping[str, str]
    rank_defaults: RankDefaults
    rank_hash: str
    search_profiles: Mapping[str, SearchProfileDefinition]
    collections: Mapping[tuple[str, int], CollectionDefinition]
    derivations: Mapping[str, PipelineDefinition]
    triggers: Mapping[str, StandaloneTrigger]
    views: Mapping[tuple[str, int], ViewDefinition]
    artifacts: Mapping[tuple[str, int], ArtifactDefinition]
    mcps: Mapping[tuple[str, int], McpDefinition]
    packages: Mapping[tuple[str, str], PackageDefinition]
    deployment_bindings: Mapping[str, str]
    active_collections: Mapping[str, int]
    active_views: Mapping[str, int]
    active_artifacts: Mapping[str, int]
    processor_config_hashes: Mapping[str, str]
    catalog_hash: str

    @property
    def answerable_collections(self) -> frozenset[str]:
        """The active collections whose author declared them answerable.

        Only active versions count: answering is a read of current memory, so a
        retired collection version stays searchable by explicit reference without
        becoming a synthesis source.
        """

        return frozenset(
            name
            for name, version in self.active_collections.items()
            if self.collections[(name, version)].answerable
        )

    def resolve_collection(
        self, reference: str, version: int | None = None
    ) -> CollectionDefinition:
        name, resolved_version = self._resolve_version_ref(
            reference, version, self.active_collections, "collection"
        )
        try:
            return self.collections[(name, resolved_version)]
        except KeyError as exc:
            raise KeyError(f"unknown collection {name}@{resolved_version}") from exc

    def resolve_stored_collection(
        self,
        name: str,
        version: int,
        contract_hash: str,
    ) -> CollectionDefinition:
        """Resolve an immutable record contract persisted on a record.

        Name and version alone are insufficient: reusing a version with a changed
        schema, projection, or readiness requirement must never reinterpret
        stored rows through the replacement definition.  Binding edits — optional
        processors and search routing — are outside the contract by design, so
        they resolve without a new version.
        """

        definition = self.collections.get((name, version))
        if definition is None:
            raise CollectionDefinitionMismatch(name, version, contract_hash, None)
        if definition.contract_hash != contract_hash:
            raise CollectionDefinitionMismatch(
                name,
                version,
                contract_hash,
                definition.contract_hash,
            )
        return definition

    def resolve_view(self, reference: str, version: int | None = None) -> ViewDefinition:
        name, resolved_version = self._resolve_version_ref(
            reference, version, self.active_views, "view"
        )
        try:
            return self.views[(name, resolved_version)]
        except KeyError as exc:
            raise KeyError(f"unknown view {name}@{resolved_version}") from exc

    def resolve_artifact(self, reference: str, version: int | None = None) -> ArtifactDefinition:
        name, resolved_version = self._resolve_version_ref(
            reference, version, self.active_artifacts, "artifact"
        )
        try:
            return self.artifacts[(name, resolved_version)]
        except KeyError as exc:
            raise KeyError(f"unknown artifact {name}@{resolved_version}") from exc

    def resolve_mcp(self, reference: str, version: int | None = None) -> McpDefinition:
        """Resolve an exact MCP interface version.

        MCP definitions intentionally have no active alias because package
        manifests bind an exact interface contract.
        """

        if "@" in reference:
            if version is not None:
                raise ValueError("MCP version supplied twice")
            name, resolved = split_exact_reference(reference)
            resolved_version = int(resolved)
        else:
            if version is None:
                raise ValueError("MCP reference must be exact name@version")
            if version < 1:
                raise ValueError("MCP version must be positive")
            name, resolved_version = reference, version
        try:
            return self.mcps[(name, resolved_version)]
        except KeyError as exc:
            raise KeyError(f"unknown MCP {name}@{resolved_version}") from exc

    @property
    def mcp_definitions(self) -> Mapping[tuple[str, int], McpDefinition]:
        """Descriptive alias for callers that need the entire MCP family."""

        return self.mcps

    def resolve_package(self, name: str, version: str) -> PackageDefinition:
        try:
            return self.packages[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown package {name}@{version}") from exc

    def resolve_processor(self, name: str) -> ProcessorDefinition | PipelineDefinition:
        if name in self.processors:
            return self.processors[name]
        if name in self.derivations:
            return self.derivations[name]
        raise KeyError(f"unknown processor {name!r}")

    def resolve_trigger(self, name: str) -> StandaloneTrigger:
        try:
            return self.triggers[name]
        except KeyError as exc:
            raise KeyError(f"unknown trigger {name!r}") from exc

    def resolve_search_profile(self, name: str) -> SearchProfileDefinition:
        try:
            return self.search_profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown search profile {name!r}") from exc

    @staticmethod
    def _resolve_version_ref(
        reference: str,
        version: int | None,
        active: Mapping[str, int],
        kind: str,
    ) -> tuple[str, int]:
        if "@" in reference:
            if version is not None:
                raise ValueError(f"{kind} version supplied twice")
            name, exact = split_exact_reference(reference)
            return name, int(exact)
        if version is not None:
            if version < 1:
                raise ValueError(f"{kind} version must be positive")
            return reference, version
        try:
            return reference, active[reference]
        except KeyError as exc:
            raise KeyError(f"{kind} {reference!r} has no active version") from exc


@dataclass(slots=True)
class _Loaded:
    value: BaseModel
    path: Path


class _CatalogBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.models: ModelCatalog | None = None
        self.processors: dict[str, ProcessorDefinition] = {}
        self.score_owners: dict[str, str] = {}
        self.rank_defaults: RankDefaults | None = None
        self.search_profiles: dict[str, SearchProfileDefinition] = {}
        self.collections: dict[tuple[str, int], CollectionDefinition] = {}
        self.derivations: dict[str, PipelineDefinition] = {}
        self.triggers: dict[str, StandaloneTrigger] = {}
        self.views: dict[tuple[str, int], ViewDefinition] = {}
        self.artifacts: dict[tuple[str, int], ArtifactDefinition] = {}
        self.mcps: dict[tuple[str, int], McpDefinition] = {}
        self.packages: dict[tuple[str, str], PackageDefinition] = {}
        self.paths: dict[tuple[str, Any], Path] = {}
        self.active_collections: dict[str, int] = {}
        self.active_views: dict[str, int] = {}
        self.active_artifacts: dict[str, int] = {}
        self.bindings: dict[str, str] = {}
        self.processor_config_hashes: dict[str, str] = {}

    def build(self) -> DefinitionCatalog:
        self._load_models()
        self._load_processors()
        self._load_rank()
        self._load_search_profiles()
        self._load_collections()
        self._load_derivations_and_inline_triggers()
        self._load_standalone_triggers()
        self._load_views()
        self._load_artifacts()
        self._load_mcps()
        self._load_packages()
        self._load_overrides()
        self._validate_global_graph()
        return self._freeze()

    def _duplicate(self, kind: str, key: Any, path: Path) -> None:
        previous = self.paths.get((kind, key))
        if previous is not None:
            raise DefinitionError(
                "duplicate",
                f"duplicate {kind} {key!r}; first declared in {previous}",
                file=path,
            )
        self.paths[(kind, key)] = path

    def _load_models(self) -> None:
        path = self.settings.models_file
        self.models = _parse(ModelCatalog, load_yaml_file(path), path)
        # Provider *references* are checked by ModelCatalog itself; what needs
        # the runtime registry is whether each named connection's adapter exists
        # and can do what the definitions ask of it.
        for provider_name, connection in self.models.providers.items():
            try:
                descriptor = provider_descriptor(connection.adapter)
            except ValueError as exc:
                raise DefinitionError(
                    "provider", str(exc), file=path, path=f"providers.{provider_name}.adapter"
                ) from exc
            if connection.json_capability != "none" and not (
                descriptor.json_capabilities & {connection.json_capability}
            ):
                raise DefinitionError(
                    "provider",
                    f"adapter {connection.adapter!r} cannot request "
                    f"{connection.json_capability} output",
                    file=path,
                    path=f"providers.{provider_name}.json_capability",
                )
        for alias_name, alias in self.models.aliases.items():
            for target in alias.targets:
                provider_name = target.split(":", 1)[0]
                adapter = self.models.providers[provider_name].adapter
                try:
                    validate_generation_params(adapter, alias.params)
                except ValueError as exc:
                    raise DefinitionError(
                        "provider", str(exc), file=path, path=f"aliases.{alias_name}"
                    ) from exc
            if (alias.context_tokens or self.settings.model_context_tokens) < 4_096:
                raise DefinitionError(
                    "model_context",
                    "completion alias has no usable context limit",
                    file=path,
                    path=f"aliases.{alias_name}.context_tokens",
                )
        embedding_adapter = self.models.providers[self.models.embedding.provider].adapter
        if not provider_descriptor(embedding_adapter).supports_embedding:
            raise DefinitionError(
                "provider",
                f"adapter {embedding_adapter!r} does not support embedding",
                file=path,
                path="embedding.provider",
            )

    def _load_processors(self) -> None:
        paths = _catalog_files(self.settings.processors_file)
        for path in paths:
            root = _mapping(load_yaml_file(path), path, "processor catalog")
            for index, raw in enumerate(_sequence(root, "processors", path)):
                definition = _parse(
                    ProcessorDefinition,
                    raw,
                    path,
                    context=f"processors[{index}]",
                )
                self._duplicate("processor", definition.name, path)
                if definition.kind == "score":
                    self._claim_score_name(definition.name, definition.name, path, index)
                if definition.kind == "json":
                    assert definition.output_schema is not None
                    _check_json_schema(
                        definition.output_schema, path, f"processors[{index}].output_schema"
                    )
                    if definition.output_schema.get("type") != "object":
                        raise DefinitionError(
                            "output_schema",
                            "processor output_schema must have type object",
                            file=path,
                            path=f"processors[{index}].output_schema.type",
                        )
                    for score_name, score_path in definition.score_fields.items():
                        leaf = self._schema_at_path(definition.output_schema, score_path)
                        if leaf is None or leaf.get("type") not in {"number", "integer"}:
                            raise DefinitionError(
                                "score_field",
                                f"score path {score_path!r} must resolve to a numeric schema leaf",
                                file=path,
                                path=f"processors[{index}].score_fields.{score_name}",
                            )
                        self._claim_score_name(score_name, definition.name, path, index)
                    if definition.default_output is not None:
                        try:
                            Draft202012Validator(
                                definition.output_schema, format_checker=FormatChecker()
                            ).validate(definition.default_output)
                        except JsonSchemaValidationError as exc:
                            raise DefinitionError(
                                "default_output",
                                exc.message,
                                file=path,
                                path=f"processors[{index}].default_output",
                            ) from exc
                        if (
                            len(canonical_json(definition.default_output))
                            > self.settings.max_annotation_bytes
                        ):
                            raise DefinitionError(
                                "limit",
                                "default_output exceeds MAX_ANNOTATION_BYTES",
                                file=path,
                                path=f"processors[{index}].default_output",
                            )
                self.processors[definition.name] = _hashed(definition)
        if len(self.score_owners) > 8:
            raise DefinitionError(
                "limit",
                "at most eight score names (score processors plus score_fields) may be declared",
                file=path,
            )
        llm_scores = sum(
            processor.kind == "score" and processor.source == "llm"
            for processor in self.processors.values()
        )
        if llm_scores > 4:
            raise DefinitionError(
                "limit", "at most four LLM score processors may be declared", file=path
            )
        self._validate_supersession()

    def _validate_supersession(self) -> None:
        """Check that ``supersedes`` forms linear, same-kind, acyclic chains.

        Linear because two processors superseding one name would leave no defined
        preference order; same-kind because a reader that follows the chain must
        find comparable values in the same shape.
        """

        claimed: dict[str, str] = {}
        for name in sorted(self.processors):
            definition = self.processors[name]
            target = definition.supersedes
            if target is None:
                continue
            path = self.paths[("processor", name)]
            if target == name:
                raise DefinitionError(
                    "supersedes", f"processor {name!r} cannot supersede itself", file=path
                )
            superseded = self.processors.get(target)
            if superseded is None:
                raise DefinitionError(
                    "reference",
                    f"processor {name!r} supersedes unknown processor {target!r}",
                    file=path,
                    path="supersedes",
                )
            if superseded.kind != definition.kind:
                raise DefinitionError(
                    "supersedes",
                    f"processor {name!r} supersedes {target!r} of a different kind",
                    file=path,
                    path="supersedes",
                )
            previous = claimed.get(target)
            if previous is not None:
                raise DefinitionError(
                    "supersedes",
                    f"processors {previous!r} and {name!r} both supersede {target!r}",
                    file=path,
                    path="supersedes",
                )
            claimed[target] = name
        for name in sorted(self.processors):
            seen: set[str] = set()
            cursor: str | None = name
            while cursor is not None:
                if cursor in seen:
                    raise DefinitionError(
                        "supersedes",
                        f"supersession cycle through processor {cursor!r}",
                        file=self.paths[("processor", name)],
                        path="supersedes",
                    )
                seen.add(cursor)
                cursor = self.processors[cursor].supersedes

    def supersession_chain(self, name: str) -> tuple[str, ...]:
        """Return the names a reader falls back to for ``name``, newest first."""

        chain: list[str] = []
        cursor = self.processors[name].supersedes if name in self.processors else None
        while cursor is not None:
            chain.append(cursor)
            cursor = self.processors[cursor].supersedes
        return tuple(chain)

    def _claim_score_name(self, score_name: str, owner: str, path: Path, index: int) -> None:
        if score_name in self.score_owners:
            raise DefinitionError(
                "score_collision",
                f"flat score name {score_name!r} is declared more than once",
                file=path,
                path=f"processors[{index}]",
            )
        self.score_owners[score_name] = owner

    @staticmethod
    def _schema_at_path(schema: dict[str, Any], dotted_path: str) -> dict[str, Any] | None:
        current: Any = schema
        for part in dotted_path.split("."):
            properties = current.get("properties") if isinstance(current, dict) else None
            if not isinstance(properties, dict) or part not in properties:
                return None
            current = properties[part]
        return current if isinstance(current, dict) else None

    def _load_rank(self) -> None:
        path = self.settings.rank_default_file
        self.rank_defaults = _parse(RankDefaults, load_yaml_file(path), path)
        scorer_names = frozenset(self.score_owners)
        normalized: dict[str, Any] = {}
        for mode, expression in self.rank_defaults.variants.items():
            try:
                normalized[mode] = validate_rank_expression(
                    expression, mode=mode, scorer_names=scorer_names
                )
            except RankValidationError as exc:
                raise DefinitionError(
                    exc.code, str(exc), file=path, path=f"variants.{mode}"
                ) from exc
        self.rank_defaults = self.rank_defaults.model_copy(update={"variants": normalized})

    def _load_search_profiles(self) -> None:
        for path in _catalog_files(self.settings.search_profiles_file):
            root = _mapping(load_yaml_file(path), path, "search profile catalog")
            profiles = root.get("profiles")
            if set(root) != {"profiles"} or not isinstance(profiles, dict):
                raise DefinitionError(
                    "shape", "root must contain exactly a profiles mapping", file=path
                )
            for name in sorted(profiles):
                raw = profiles[name]
                if not isinstance(raw, dict):
                    raise DefinitionError(
                        "shape", "profile must be a mapping", file=path, path=name
                    )
                if "name" in raw:
                    raise DefinitionError(
                        "profile_identity",
                        "profile name is supplied only by its mapping key",
                        file=path,
                        path=f"profiles.{name}.name",
                    )
                definition = _parse(
                    SearchProfileDefinition,
                    {**raw, "name": name},
                    path,
                    context=f"profiles.{name}",
                )
                self._duplicate("search_profile", name, path)
                try:
                    descriptor = backend_descriptor(definition.backend)
                except ValueError as exc:
                    raise DefinitionError("backend", str(exc), file=path, path=name) from exc
                option_names = {
                    key for key in ("layout", "consistency", "enabled_if_credentials") if key in raw
                }
                if not option_names <= descriptor.allowed_options:
                    raise DefinitionError(
                        "backend_option",
                        f"unsupported options: {sorted(option_names - descriptor.allowed_options)}",
                        file=path,
                        path=name,
                    )
                self.search_profiles[name] = _hashed(definition)

    def _load_collections(self) -> None:
        for path in _optional_yaml_files(self.settings.collections_dir):
            root = _mapping(load_yaml_file(path), path, "collection file")
            for index, raw in enumerate(_sequence(root, "collections", path)):
                definition = _parse(
                    CollectionDefinition, raw, path, context=f"collections[{index}]"
                )
                key = (definition.name, definition.version)
                self._duplicate("collection", key, path)
                _check_json_schema(definition.content_schema, path, f"collections[{index}].schema")
                self._validate_content_schema(definition, path, index)
                self._validate_collection_references(definition, path)
                hashed = _hashed(self._with_supersession(definition))
                self.collections[key] = hashed
                if definition.active:
                    self._set_active(
                        self.active_collections,
                        definition.name,
                        definition.version,
                        "collection",
                        path,
                    )
        # A configured directory that yielded nothing is a misconfiguration and
        # still fails. A service with no catalog configured at all is the
        # shipped default: it compiles to an empty catalog and waits for a
        # workspace to publish one.
        if not self.collections and self.settings.collections_dir is not None:
            raise DefinitionError(
                "empty_catalog",
                "no collection definitions found",
                file=self.settings.collections_dir,
            )

    def _with_supersession(self, collection: CollectionDefinition) -> CollectionDefinition:
        """Attach read-time supersession fallbacks to annotation-backed fields.

        A field declared over ``annotations.<processor>`` gains the equivalent
        paths through that processor's superseded ancestors, so a row annotated by
        an older name still answers the field.  This never affects the record
        contract: the fallbacks are excluded from serialization and therefore from
        the contract hash.
        """

        if not any(definition.supersedes for definition in self.processors.values()):
            return collection
        updated: dict[str, DeclaredField] = {}
        changed = False
        for name, declaration in collection.fields.items():
            root, *parts = declaration.path.split(".")
            if root != "annotations" or not parts:
                updated[name] = declaration
                continue
            chain = self.supersession_chain(parts[0])
            if not chain:
                updated[name] = declaration
                continue
            updated[name] = declaration.model_copy(
                update={
                    "fallback_paths": tuple(
                        ".".join(("annotations", ancestor, *parts[1:])) for ancestor in chain
                    )
                }
            )
            changed = True
        if not changed:
            return collection
        return collection.model_copy(update={"fields": updated})

    def _validate_collection_references(self, collection: CollectionDefinition, path: Path) -> None:
        for profile in collection.all_search_profiles:
            if profile not in self.search_profiles:
                raise DefinitionError(
                    "reference",
                    f"collection references unknown search profile {profile!r}",
                    file=path,
                    path="search_profile",
                )
        for processor in (
            *collection.required_processors,
            *collection.optional_processors,
        ):
            if processor not in self.processors:
                raise DefinitionError(
                    "reference",
                    f"collection references unknown processor {processor!r}",
                    file=path,
                    path=(
                        "required_processors"
                        if processor in collection.required_processors
                        else "optional_processors"
                    ),
                )

    def _set_active(
        self, target: dict[str, int], name: str, version: int, kind: str, path: Path
    ) -> None:
        if name in target:
            raise DefinitionError(
                "active_collision",
                f"multiple active {kind} versions for {name!r}",
                file=path,
            )
        target[name] = version

    def _validate_content_schema(
        self, definition: CollectionDefinition, path: Path, index: int
    ) -> None:
        schema = definition.content_schema
        if schema.get("type") != "object":
            raise DefinitionError(
                "collection_schema",
                "collection content schema must have type object",
                file=path,
                path=f"collections[{index}].schema.type",
            )
        properties = schema.get("properties")
        if not isinstance(properties, dict) or properties.get("text", {}).get("type") != "string":
            raise DefinitionError(
                "collection_schema",
                "collection schema must declare string property text",
                file=path,
                path=f"collections[{index}].schema.properties.text",
            )
        if "text" not in schema.get("required", []):
            raise DefinitionError(
                "collection_schema",
                "collection schema must require text after projection",
                file=path,
                path=f"collections[{index}].schema.required",
            )
        if definition.text_projection is not None:
            try:
                refs = template_references(definition.text_projection)
            except TemplateError as exc:
                raise DefinitionError(
                    exc.code, str(exc), file=path, path="text_projection"
                ) from exc
            unknown = sorted(ref for ref in refs if ref.split(".", 1)[0] not in properties)
            if unknown:
                raise DefinitionError(
                    "template_reference",
                    f"text projection references undeclared content property {unknown[0]!r}",
                    file=path,
                    path="text_projection",
                )
        for field_name, field in definition.fields.items():
            self._validate_declared_field(definition, field_name, field, path)

    def _validate_declared_field(
        self,
        collection: CollectionDefinition,
        field_name: str,
        field: DeclaredField,
        path: Path,
    ) -> None:
        parts = field.path.split(".")
        if parts[0] == "content":
            schema: Any = collection.content_schema
            for part in parts[1:]:
                properties = schema.get("properties") if isinstance(schema, dict) else None
                if not isinstance(properties, dict) or part not in properties:
                    raise DefinitionError(
                        "field_path",
                        f"path {field.path!r} is not declared by the content schema",
                        file=path,
                        path=f"fields.{field_name}.path",
                    )
                schema = properties[part]
        else:
            processor_name = parts[1] if len(parts) > 1 else ""
            processor = self.processors.get(processor_name)
            if processor is None:
                raise DefinitionError(
                    "reference",
                    f"unknown annotation processor in path {field.path!r}",
                    file=path,
                    path=f"fields.{field_name}.path",
                )
            schema = processor.effective_output_schema
            for part in parts[2:]:
                properties = schema.get("properties") if isinstance(schema, dict) else None
                if not isinstance(properties, dict) or part not in properties:
                    raise DefinitionError(
                        "field_path",
                        f"path {field.path!r} is not declared by annotation schema",
                        file=path,
                        path=f"fields.{field_name}.path",
                    )
                schema = properties[part]
        if not self._schema_matches_field(schema, field):
            raise DefinitionError(
                "field_type",
                f"declared type for {field_name!r} does not match its JSON Schema path",
                file=path,
                path=f"fields.{field_name}.type",
            )

    @staticmethod
    def _schema_matches_field(schema: Any, field: DeclaredField) -> bool:
        if not isinstance(schema, dict):
            return False
        if field.is_array:
            if schema.get("type") != "array" or not isinstance(schema.get("items"), dict):
                return False
            schema = schema["items"]
        expected = field.scalar_type
        if expected == "datetime":
            return schema.get("type") == "string" and schema.get("format") == "date-time"
        return schema.get("type") == expected

    def _load_derivations_and_inline_triggers(self) -> None:
        for path in _optional_yaml_files(self.settings.derivations_dir):
            raw = _mapping(load_yaml_file(path), path, "derivation")
            definition = _parse(PipelineDefinition, raw, path)
            self._duplicate("processor", definition.name, path)
            definition = self._resolve_derivation(definition, path)
            self.derivations[definition.name] = _hashed(definition)
            if definition.trigger is not None:
                trigger_raw = {
                    "name": f"{definition.name}.default",
                    "processor": definition.name,
                    **definition.trigger.model_dump(mode="python"),
                }
                trigger = _parse(StandaloneTrigger, trigger_raw, path, context="trigger")
                self._add_trigger(trigger, path)
        # Same rule as collections: configured-but-empty is a mistake, wholly
        # unconfigured is the default a service starts in.
        if not self.derivations and self.settings.derivations_dir is not None:
            raise DefinitionError(
                "empty_catalog",
                "no derivation definitions found",
                file=self.settings.derivations_dir,
            )

    def _resolve_derivation(self, definition: PipelineDefinition, path: Path) -> PipelineDefinition:
        assert self.models is not None
        resolved_sources = dict(definition.sources)
        for name, source in definition.sources.items():
            if isinstance(source, StreamSource | CurrentSource):
                for collection in source.collections:
                    self._require_collection_scope_version(
                        collection, source.collection_versions, path
                    )
                resolved_versions = {
                    collection: (
                        source.collection_versions[collection]
                        if collection in source.collection_versions
                        else (self.active_collections[collection],)
                    )
                    for collection in source.collections
                }
                resolved_sources[name] = source.model_copy(
                    update={"collection_versions": resolved_versions}
                )
            elif isinstance(source, RecordSource):
                try:
                    target_version = (
                        source.collection_version
                        if source.collection_version is not None
                        else self.active_collections[source.collection]
                    )
                    target = self.collections[(source.collection, target_version)]
                except KeyError as exc:
                    raise DefinitionError(
                        "reference",
                        f"source {name!r} references unknown collection/version "
                        f"{source.collection!r}",
                        file=path,
                        path=f"sources.{name}.collection",
                    ) from exc
                resolved_sources[name] = source.model_copy(
                    update={"collection_version": target.version}
                )
        definition = definition.model_copy(update={"sources": resolved_sources})

        emit = definition.emit
        try:
            target = (
                self.collections[(emit.collection, emit.collection_version)]
                if emit.collection_version is not None
                else self.collections[(emit.collection, self.active_collections[emit.collection])]
            )
        except KeyError as exc:
            raise DefinitionError(
                "reference",
                f"unknown emission collection/version {emit.collection!r}",
                file=path,
                path="emit.collection",
            ) from exc
        if (emit.keys or emit.driver_key or emit.dynamic_keys) and target.mode not in {
            "keyed",
            "mixed",
        }:
            raise DefinitionError(
                "emit_mode", "emission keys require a keyed or mixed collection", file=path
            )
        if (
            not emit.keys
            and not emit.driver_key
            and not emit.dynamic_keys
            and target.mode not in {"event", "mixed"}
        ):
            raise DefinitionError(
                "emit_mode", "an unkeyed emission requires an event or mixed collection", file=path
            )
        if emit.review == "required" and not emit.keys:
            raise DefinitionError(
                "emit_review", "reviewed emission requires a bounded keyed target", file=path
            )
        if emit.driver_key and (
            definition.driver.keyed is not True
            or definition.driver.max_records != 1
            or definition.driver.allow_empty
        ):
            raise DefinitionError(
                "emit_driver_key",
                "driver_key emission requires one non-empty keyed driver record",
                file=path,
                path="emit.driver_key",
            )
        if emit.collection_version is None:
            emit = emit.model_copy(update={"collection_version": target.version})
            definition = definition.model_copy(update={"emit": emit})

        if definition.trigger is not None:
            definition = definition.model_copy(
                update={"trigger": self._resolve_trigger_scopes(definition.trigger)}
            )

        required_client_scores = sorted(
            processor_name
            for processor_name in target.required_processors
            if (processor := self.processors.get(processor_name)) is not None
            and processor.kind == "score"
            and processor.source == "client"
        )
        if required_client_scores:
            raise DefinitionError(
                "required_client_output",
                f"pipeline emission collection {target.name}@{target.version} requires "
                "client score values that a derivation cannot supply: "
                f"{required_client_scores}",
                file=path,
                path="emit.collection",
            )

        self._validate_derivation_limits(definition, path)
        self._validate_derivation_context(definition, path)
        self._validate_derivation_templates(definition, path)
        self._validate_trigger_conditions(definition, path)
        if len(canonical_json(_dump(definition))) > self.settings.max_derivation_config_bytes:
            raise DefinitionError(
                "limit", "normalized derivation exceeds MAX_DERIVATION_CONFIG_BYTES", file=path
            )
        return definition

    def _require_collection_scope_version(
        self,
        collection: str,
        version_map: Mapping[str, tuple[int, ...]],
        path: Path,
    ) -> None:
        versions = version_map.get(collection)
        if versions:
            for version in versions:
                if (collection, version) not in self.collections:
                    raise DefinitionError(
                        "reference",
                        f"unknown collection {collection}@{version}",
                        file=path,
                    )
        elif collection not in self.active_collections:
            raise DefinitionError(
                "reference", f"collection {collection!r} has no active version", file=path
            )

    def _validate_derivation_limits(self, definition: PipelineDefinition, path: Path) -> None:
        models = self.models
        assert models is not None
        limits = definition.limits
        if limits.max_visible_records > self.settings.max_derived_from - 1:
            raise DefinitionError(
                "budget",
                "max_visible_records must reserve one provenance parent",
                file=path,
                path="limits.max_visible_records",
            )
        if limits.max_total_tokens > self.settings.max_run_total_tokens:
            raise DefinitionError("budget", "max_total_tokens exceeds process maximum", file=path)
        if limits.max_wall_s > self.settings.max_run_wall_s:
            raise DefinitionError("budget", "max_wall_s exceeds process maximum", file=path)

        retrieval_bound = 0
        llm_calls = 0
        for index, task in enumerate(definition.tasks):
            try:
                adapter = task_adapter(task.use)
                config = adapter.validate_config(task.config)
            except (KeyError, TypeError, ValidationError, ValueError) as exc:
                raise DefinitionError(
                    "task_config", str(exc), file=path, path=f"tasks[{index}].with"
                ) from exc
            if isinstance(config, LLMTaskConfig | SearchTaskConfig | TemplateTaskConfig) and (
                task.input is not None
            ):
                raise DefinitionError(
                    "task_input",
                    f"built-in Task {task.use!r} requires input to be omitted; "
                    "reference dynamic values in its configured prompt, query, or template",
                    file=path,
                    path=f"tasks[{index}].input",
                )
            if isinstance(config, LLMTaskConfig):
                _check_json_schema(
                    config.output_schema,
                    path,
                    f"tasks[{index}].with.output_schema",
                )
                # A JSON Task may make one bounded correction call.
                llm_calls += 2
                alias_name = config.model or definition.model or models.defaults.derivation
                alias = self._alias(alias_name, path, f"tasks[{index}].with.model")
                self._validate_step_params(alias, config.params, path, index)
                output_tokens = (
                    config.max_output_tokens
                    or config.params.get("max_output_tokens")
                    or alias.params.get("max_output_tokens")
                    or self.settings.max_output_tokens
                )
                assert isinstance(output_tokens, int)
                if output_tokens > self.settings.max_output_tokens:
                    raise DefinitionError(
                        "budget",
                        "Task output exceeds MAX_OUTPUT_TOKENS",
                        file=path,
                        path=f"tasks[{index}].with.max_output_tokens",
                    )
                context = alias.context_tokens or self.settings.model_context_tokens
                if self.settings.max_prompt_tokens + output_tokens > context:
                    raise DefinitionError(
                        "model_context",
                        f"statically bounded call does not fit alias {alias_name!r}",
                        file=path,
                        path=f"tasks[{index}]",
                    )
                if output_tokens > limits.max_total_tokens:
                    raise DefinitionError(
                        "budget", "Task output exceeds run token budget", file=path
                    )
            elif isinstance(config, SearchTaskConfig):
                if config.max_tokens > self.settings.max_prompt_tokens:
                    raise DefinitionError(
                        "budget",
                        "search max_tokens exceeds MAX_PROMPT_TOKENS",
                        file=path,
                        path=f"tasks[{index}].with.max_tokens",
                    )
                query_raw = dict(config.spec)
                if config.q is not None:
                    if "q" in query_raw:
                        raise DefinitionError(
                            "search_spec",
                            "search q must not also appear inside spec",
                            file=path,
                            path=f"tasks[{index}].with",
                        )
                    query_raw["q"] = config.q
                    multiplier = 1
                else:
                    if "q" not in query_raw:
                        raise DefinitionError(
                            "search_spec",
                            "foreach search spec must provide q, normally {{item}}",
                            file=path,
                            path=f"tasks[{index}].with.spec.q",
                        )
                    multiplier = 5
                try:
                    spec = SearchSpec.model_validate(query_raw)
                except ValidationError as exc:
                    error = exc.errors(include_url=False)[0]
                    raise DefinitionError(
                        "search_spec",
                        error["msg"],
                        file=path,
                        path=f"tasks[{index}].with.spec.{_path_from_pydantic(error)}",
                    ) from exc
                self._validate_search_spec(
                    spec,
                    path,
                    f"tasks[{index}].with.spec",
                    allow_templates=True,
                )
                retrieval_bound += spec.k * multiplier
        if retrieval_bound > limits.max_retrieved_records:
            raise DefinitionError(
                "budget",
                "search Tasks can expose more than max_retrieved_records",
                file=path,
                path="limits.max_retrieved_records",
            )
        if limits.max_retrieved_records > limits.max_visible_records:
            raise DefinitionError(
                "budget",
                "max_retrieved_records cannot exceed max_visible_records",
                file=path,
                path="limits.max_retrieved_records",
            )
        if llm_calls > limits.max_llm_calls:
            raise DefinitionError("budget", "pipeline exceeds max_llm_calls", file=path)

    def _alias(self, name: str, path: Path, field: str) -> ModelAlias:
        assert self.models is not None
        try:
            alias = self.models.aliases[name]
        except KeyError as exc:
            raise DefinitionError(
                "reference", f"unknown model alias {name!r}", file=path, path=field
            ) from exc
        return alias

    def _validate_step_params(
        self, alias: ModelAlias, params: dict[str, Any], path: Path, index: int
    ) -> None:
        assert self.models is not None
        merged = {**alias.params, **params}
        for target in alias.targets:
            adapter = self.models.providers[target.split(":", 1)[0]].adapter
            try:
                validate_generation_params(adapter, merged)
            except ValueError as exc:
                raise DefinitionError(
                    "provider_param",
                    str(exc),
                    file=path,
                    path=f"tasks[{index}].with.params",
                ) from exc

    def _validate_derivation_context(self, definition: PipelineDefinition, path: Path) -> None:
        """Validate named canonical sources; views resolve in the graph pass."""

        for name, source in definition.sources.items():
            if source.max_tokens > self.settings.max_prompt_tokens:
                raise DefinitionError(
                    "budget",
                    f"sources.{name}.max_tokens exceeds MAX_PROMPT_TOKENS",
                    file=path,
                    path=f"sources.{name}.max_tokens",
                )
            if isinstance(source, CurrentSource):
                collections = self._scope_collection_definitions(
                    source.collections,
                    source.collection_versions,
                    path,
                    f"sources.{name}",
                )
                if any(collection.mode not in {"keyed", "mixed"} for collection in collections):
                    raise DefinitionError(
                        "source_kind",
                        f"current source {name!r} requires keyed or mixed collections",
                        file=path,
                        path=f"sources.{name}",
                    )
            elif isinstance(source, RecordSource):
                assert source.collection_version is not None
                collection = self.collections[(source.collection, source.collection_version)]
                if collection.mode not in {"keyed", "mixed"}:
                    raise DefinitionError(
                        "source_kind",
                        f"record source {name!r} requires a keyed or mixed collection",
                        file=path,
                        path=f"sources.{name}.collection",
                    )
            elif isinstance(source, ViewSource):
                try:
                    refs = require_known_references(
                        source.params,
                        {"entity", "run"},
                        context=f"sources.{name}.params",
                    )
                except TemplateError as exc:
                    raise DefinitionError(
                        exc.code, str(exc), file=path, path=f"sources.{name}.params"
                    ) from exc
                self._require_valid_core_refs(refs, path, f"sources.{name}.params")

    def _require_valid_core_refs(self, refs: Iterable[str], path: Path, where: str) -> None:
        allowed = {
            "entity",
            *(f"run.{key}" for key in RUN_TEMPLATE_KEYS),
        }
        invalid = sorted(
            ref for ref in refs if ref not in allowed and ref.split(".", 1)[0] in {"entity", "run"}
        )
        if invalid:
            raise DefinitionError(
                "template_reference",
                f"unknown core reference(s) {invalid}; entity is a scalar and run offers "
                f"{sorted(RUN_TEMPLATE_KEYS)}; use {{{{entity}}}} directly, while bare "
                "{{run}} is not allowed",
                file=path,
                path=where,
            )

    def _validate_derivation_templates(self, definition: PipelineDefinition, path: Path) -> None:
        available = {"entity", "run", *definition.sources}
        all_refs: set[str] = set()
        task_refs: list[frozenset[str]] = []
        for index, task in enumerate(definition.tasks):
            try:
                config = task_adapter(task.use).validate_config(task.config)
                input_refs = require_known_references(
                    task.input,
                    available,
                    context=f"tasks[{index}].input",
                )
                if isinstance(config, SearchTaskConfig) and config.foreach is not None:
                    refs = (
                        input_refs
                        | require_known_references(
                            config.foreach,
                            available,
                            context=f"tasks[{index}].with.foreach",
                        )
                        | require_known_references(
                            config.spec,
                            available | {"item", "index"},
                            context=f"tasks[{index}].with.spec",
                        )
                    )
                    require_known_references(
                        {"max_tokens": config.max_tokens},
                        set(),
                        context=f"tasks[{index}].with",
                    )
                elif isinstance(config, SearchTaskConfig):
                    refs = input_refs | require_known_references(
                        {"q": config.q, "spec": config.spec},
                        available,
                        context=f"tasks[{index}].with",
                    )
                elif isinstance(config, LLMTaskConfig):
                    refs = input_refs | require_known_references(
                        config.prompt,
                        available,
                        context=f"tasks[{index}].with.prompt",
                    )
                    require_known_references(
                        {
                            "model": config.model,
                            "params": config.params,
                            "output_schema": config.output_schema,
                            "max_output_tokens": config.max_output_tokens,
                        },
                        set(),
                        context=f"tasks[{index}].with",
                    )
                elif isinstance(config, TemplateTaskConfig):
                    refs = input_refs | require_known_references(
                        config.template,
                        available,
                        context=f"tasks[{index}].with.template",
                    )
                else:
                    require_known_references(
                        task.config,
                        set(),
                        context=f"tasks[{index}].with",
                    )
                    refs = input_refs
                if isinstance(config, SearchTaskConfig) and config.foreach is not None:
                    exact = config.foreach.strip()
                    if not exact.startswith("{{") or not exact.endswith("}}"):
                        raise TemplateError(
                            "foreach must be an exact typed reference", code="template_foreach"
                        )
                    if len(template_references(exact)) != 1:
                        raise TemplateError(
                            "foreach must be an exact typed reference", code="template_foreach"
                        )
                task_refs.append(refs)
                all_refs.update(refs)
            except TemplateError as exc:
                raise DefinitionError(
                    exc.code, str(exc), file=path, path=exc.path or f"tasks[{index}]"
                ) from exc
            except (KeyError, TypeError, ValidationError, ValueError) as exc:
                raise DefinitionError(
                    "task_config", str(exc), file=path, path=f"tasks[{index}].with"
                ) from exc
            available.add(task.id)
        try:
            emit_refs = require_known_references(
                definition.emit.from_, available, context="emit.from"
            )
        except TemplateError as exc:
            raise DefinitionError(exc.code, str(exc), file=path, path="emit.from") from exc
        self._require_valid_core_refs(all_refs | set(emit_refs), path, "tasks")
        all_roots = {ref.split(".", 1)[0] for ref in (*all_refs, *emit_refs)}
        for name in definition.sources:
            if name not in all_roots:
                raise DefinitionError(
                    "unused_source",
                    f"source {name!r} is never referenced by a Task",
                    file=path,
                    path=f"sources.{name}",
                )
        for index, task in enumerate(definition.tasks):
            later = set().union(*task_refs[index + 1 :], emit_refs)
            if not any(ref.split(".", 1)[0] == task.id for ref in later):
                raise DefinitionError(
                    "unused_task",
                    f"Task result {task.id!r} is never referenced later or emitted",
                    file=path,
                    path=f"tasks[{index}]",
                )

    def _validate_trigger_conditions(self, definition: PipelineDefinition, path: Path) -> None:
        trigger = definition.trigger
        if trigger is None:
            return
        self._validate_trigger(trigger, definition, path, "trigger")

    _SCOPED_TRIGGER_CONDITIONS = ("write", "quiet", "changed", "retraction", "census", "at")

    def _resolve_trigger_scopes(self, trigger: Any) -> Any:
        """Resolve omitted trigger-scope versions to the active catalog versions."""

        updates: dict[str, Any] = {}
        for condition in self._SCOPED_TRIGGER_CONDITIONS:
            scope = getattr(trigger, condition)
            if scope is None:
                continue
            versions = dict(scope.collection_versions)
            for collection in scope.collections:
                if collection != "_system" and collection not in versions:
                    active = self.active_collections.get(collection)
                    if active is not None:
                        versions[collection] = (active,)
            updates[condition] = scope.model_copy(update={"collection_versions": versions})
        return trigger.model_copy(update=updates) if updates else trigger

    def _validate_trigger(
        self,
        trigger: TriggerConditions,
        target: PipelineDefinition,
        path: Path,
        prefix: str,
    ) -> None:
        if not trigger.automatic:
            raise DefinitionError("trigger", "trigger has no enabled conditions", file=path)
        if trigger.cron is not None:
            try:
                croniter(trigger.cron.expr, datetime.now(UTC))
            except (KeyError, ValueError) as exc:
                raise DefinitionError(
                    "cron", str(exc), file=path, path=f"{prefix}.cron.expr"
                ) from exc
        if trigger.accumulator is not None:
            self._validate_accumulator(trigger, target, path, prefix)
        for condition in ("write", "quiet", "changed", "retraction"):
            scope = getattr(trigger, condition)
            if scope is None:
                continue
            if scope.ignore_own_outputs and (condition == "changed" or not target.emit.driver_key):
                raise DefinitionError(
                    "trigger_self_output",
                    "ignore_own_outputs is only valid for a driver_key write, quiet, or retraction trigger",
                    file=path,
                    path=f"{prefix}.{condition}.ignore_own_outputs",
                )
            for label, actual, allowed in (
                ("collections", scope.collections, target.driver.collections),
                ("types", scope.types, target.driver.types),
                ("statuses", scope.statuses, target.driver.statuses),
            ):
                if not set(actual) <= set(allowed):
                    raise DefinitionError(
                        "trigger_scope",
                        f"{condition} trigger {label} must be a subset of input",
                        file=path,
                        path=f"{prefix}.{condition}.{label}",
                    )
            self._validate_scope_versions(scope, condition, target, path, prefix)
            self._validate_trigger_where(scope, path, f"{prefix}.{condition}")
        if trigger.census is not None:
            self._scope_collection_definitions(
                trigger.census.collections,
                trigger.census.collection_versions,
                path,
                f"{prefix}.census.collections",
            )
            self._validate_trigger_where(trigger.census, path, f"{prefix}.census")
        if trigger.at is not None:
            self._validate_at_condition(trigger.at, path, prefix)

    def _validate_scope_versions(
        self,
        scope: Any,
        condition: str,
        target: PipelineDefinition,
        path: Path,
        prefix: str,
    ) -> None:
        for collection in scope.collections:
            if collection == "_system":
                continue
            all_versions = {version for name, version in self.collections if name == collection}
            scope_versions = set(scope.collection_versions.get(collection, all_versions))
            input_versions = set(target.driver.collection_versions.get(collection, all_versions))
            if not scope_versions <= input_versions:
                raise DefinitionError(
                    "trigger_scope",
                    f"{condition} trigger versions for {collection!r} are not consumable by input",
                    file=path,
                    path=f"{prefix}.{condition}.collection_versions.{collection}",
                )

    def _validate_at_condition(self, at: Any, path: Path, prefix: str) -> None:
        collections = self._scope_collection_definitions(
            at.collections,
            at.collection_versions,
            path,
            f"{prefix}.at.collections",
        )
        declarations = [collection.fields.get(at.field) for collection in collections]
        if not declarations or any(item is None for item in declarations):
            raise DefinitionError(
                "field_reference",
                f"at trigger field {at.field!r} is not declared by every collection version",
                file=path,
                path=f"{prefix}.at.field",
            )
        typed = [item for item in declarations if item is not None]
        if any(
            item.scalar_type != "datetime" or item.is_array or not item.filter for item in typed
        ):
            raise DefinitionError(
                "field_compatibility",
                f"at trigger field {at.field!r} must be a filterable datetime scalar",
                file=path,
                path=f"{prefix}.at.field",
            )
        for collection, declaration in zip(collections, typed, strict=True):
            chain = field_annotation_names(declaration)
            if chain:
                annotation = chain[0]
                if not set(chain) & set(collection.required_processors):
                    raise DefinitionError(
                        "required_annotation",
                        f"at trigger field {at.field!r} uses optional annotation {annotation!r}",
                        file=path,
                        path=f"{prefix}.at.field",
                    )
        self._validate_trigger_where(at, path, f"{prefix}.at")

    def _scope_collection_definitions(
        self,
        collections: Iterable[str],
        collection_versions: Mapping[str, tuple[int, ...]],
        path: Path,
        field: str,
    ) -> tuple[CollectionDefinition, ...]:
        resolved: list[CollectionDefinition] = []
        for collection in collections:
            if collection == "_system":
                continue
            versions = collection_versions.get(collection)
            if versions is None:
                versions = tuple(
                    version for name, version in self.collections if name == collection
                )
            if not versions:
                raise DefinitionError(
                    "reference",
                    f"unknown collection {collection!r}",
                    file=path,
                    path=field,
                )
            for version in versions:
                try:
                    resolved.append(self.collections[(collection, version)])
                except KeyError as exc:
                    raise DefinitionError(
                        "reference",
                        f"unknown collection {collection}@{version}",
                        file=path,
                        path=field,
                    ) from exc
        return tuple(resolved)

    def _validate_accumulator(
        self,
        trigger: TriggerConditions,
        target: PipelineDefinition,
        path: Path,
        prefix: str,
    ) -> None:
        assert trigger.accumulator is not None
        metric = trigger.accumulator.metric
        if metric == "count":
            return
        collections = self._scope_collection_definitions(
            target.driver.collections,
            target.driver.collection_versions,
            path,
            f"{prefix}.accumulator.metric",
        )
        if isinstance(metric, str):
            if metric not in self.score_owners:
                raise DefinitionError(
                    "reference",
                    f"accumulator shorthand must name a score, not {metric!r}",
                    file=path,
                    path=f"{prefix}.accumulator.metric",
                )
            processor_name = self.score_owners[metric]
        elif metric.scorer is not None:
            if metric.scorer not in self.score_owners:
                raise DefinitionError(
                    "reference",
                    f"accumulator scorer must name a score, not {metric.scorer!r}",
                    file=path,
                    path=f"{prefix}.accumulator.metric.scorer",
                )
            processor_name = self.score_owners[metric.scorer]
        else:
            assert metric.annotation is not None
            assert metric.path is not None
            processor = self.processors.get(metric.annotation)
            if processor is None:
                raise DefinitionError(
                    "reference",
                    f"unknown accumulator annotation {metric.annotation!r}",
                    file=path,
                    path=f"{prefix}.accumulator.metric.annotation",
                )
            leaf = self._schema_at_path(processor.effective_output_schema, metric.path)
            if leaf is None:
                raise DefinitionError(
                    "accumulator_type",
                    "annotation accumulator path must resolve to a schema leaf",
                    file=path,
                    path=f"{prefix}.accumulator.metric.path",
                )
            numeric_aggregates = {"sum", "avg", "max", "min"}
            if metric.aggregate in numeric_aggregates and leaf.get("type") not in {
                "number",
                "integer",
            }:
                raise DefinitionError(
                    "accumulator_type",
                    "annotation accumulator path must resolve to a numeric leaf",
                    file=path,
                    path=f"{prefix}.accumulator.metric.path",
                )
            processor_name = metric.annotation
        missing = [
            f"{collection.name}@{collection.version}"
            for collection in collections
            if processor_name not in collection.required_processors
        ]
        if missing:
            raise DefinitionError(
                "required_annotation",
                f"accumulator {processor_name!r} is not required by {missing}",
                file=path,
                path=f"{prefix}.accumulator.metric",
            )

    def _validate_trigger_where(self, scope: Any, path: Path, prefix: str) -> None:
        if not scope.where:
            return
        has_system = "_system" in scope.collections
        public_collections = self._scope_collection_definitions(
            scope.collections,
            scope.collection_versions,
            path,
            prefix,
        )
        for name, predicate in scope.where.items():
            if has_system:
                if set(scope.collections) != {"_system"} or name != "predicate":
                    raise DefinitionError(
                        "field_reference",
                        "system relation triggers may filter only the predicate field",
                        file=path,
                        path=f"{prefix}.where.{name}",
                    )
                declaration = DeclaredField(path="content.predicate", type="string", filter=True)
                self._validate_predicate(
                    name, predicate, declaration, path, f"{prefix}.where.{name}"
                )
                continue
            declarations = [collection.fields.get(name) for collection in public_collections]
            if not declarations or any(item is None for item in declarations):
                raise DefinitionError(
                    "field_reference",
                    f"trigger field {name!r} is not declared by every collection version",
                    file=path,
                    path=f"{prefix}.where.{name}",
                )
            typed = [item for item in declarations if item is not None]
            if len({item.type for item in typed}) != 1 or not all(item.filter for item in typed):
                raise DefinitionError(
                    "field_compatibility",
                    f"trigger field {name!r} is not compatibly filterable",
                    file=path,
                    path=f"{prefix}.where.{name}",
                )
            for collection, declaration in zip(public_collections, typed, strict=True):
                chain = field_annotation_names(declaration)
                if chain and not set(chain) & set(collection.required_processors):
                    raise DefinitionError(
                        "required_annotation",
                        f"trigger field {name!r} uses optional annotation {chain[0]!r}",
                        file=path,
                        path=f"{prefix}.where.{name}",
                    )
            self._validate_predicate(name, predicate, typed[0], path, f"{prefix}.where.{name}")

    def _add_trigger(self, trigger: StandaloneTrigger, path: Path) -> None:
        self._duplicate("trigger", trigger.name, path)
        self.triggers[trigger.name] = trigger.model_copy(
            update={
                "definition_hash": sha256_canonical(trigger.model_dump(exclude={"definition_hash"}))
            }
        )

    def _load_standalone_triggers(self) -> None:
        directory = self.settings.triggers_dir
        if directory is None or not directory.exists():
            return
        for path in yaml_files(directory):
            raw = _mapping(load_yaml_file(path), path, "standalone trigger")
            trigger = _parse(StandaloneTrigger, raw, path)
            if not trigger.automatic:
                raise DefinitionError("trigger", "standalone trigger has no conditions", file=path)
            target = self.derivations.get(trigger.processor)
            if target is None:
                raise DefinitionError(
                    "reference",
                    f"trigger references unknown processor {trigger.processor!r}",
                    file=path,
                    path="processor",
                )
            trigger = self._resolve_trigger_scopes(trigger)
            self._validate_trigger(trigger, target, path, "trigger")
            self._add_trigger(trigger, path)

    def _load_views(self) -> None:
        for path in _optional_yaml_files(self.settings.views_dir):
            root = _mapping(load_yaml_file(path), path, "view file")
            for index, raw in enumerate(_sequence(root, "views", path)):
                definition = _parse(ViewDefinition, raw, path, context=f"views[{index}]")
                key = (definition.name, definition.version)
                self._duplicate("view", key, path)
                self._validate_view(definition, path)
                self.views[key] = _hashed(definition)
                if definition.active:
                    self._set_active(
                        self.active_views, definition.name, definition.version, "view", path
                    )
        # Configured-but-empty is a mistake; wholly unconfigured is the state a
        # service starts in before any workspace has published a catalog.
        if not self.views and self.settings.views_dir is not None:
            raise DefinitionError("empty_catalog", "no views found", file=self.settings.views_dir)

    def _validate_view(self, definition: ViewDefinition, path: Path) -> None:
        if definition.kind == "graph":
            self._validate_graph_view(definition, path)
            return
        if definition.kind == "graph_orphans":
            self._validate_graph_orphans_view(definition, path)
            return

        assert definition.query is not None
        allowed = set(definition.parameters)
        try:
            require_known_references(definition.query, allowed, context="view query")
        except TemplateError as exc:
            raise DefinitionError(exc.code, str(exc), file=path, path=exc.path or "query") from exc
        rendered = self._render_static(definition.query, definition.parameters)
        try:
            spec = SearchSpec.model_validate(rendered)
        except ValidationError as exc:
            error = exc.errors(include_url=False)[0]
            raise DefinitionError(
                "search_spec",
                error["msg"],
                file=path,
                path=f"query.{_path_from_pydantic(error)}",
            ) from exc
        self._validate_search_spec(spec, path, "query", definition.required_capabilities)

    def _validate_graph_view(self, definition: ViewDefinition, path: Path) -> None:
        """Keep graph views explicit rather than accepting ad-hoc traversal YAML."""

        expected = {
            "seed": ("string", True, None),
            "predicates": ("string_array", False, []),
            "direction": ("string", False, "out"),
            "depth": ("integer", False, 1),
            "limit": ("integer", False, 20),
        }
        actual = set(definition.parameters)
        if actual != set(expected):
            raise DefinitionError(
                "graph_view",
                "graph view parameters must be exactly seed, predicates, direction, depth, limit",
                file=path,
                path="parameters",
            )
        for name, (parameter_type, required, default) in expected.items():
            parameter = definition.parameters[name]
            if (
                parameter.type != parameter_type
                or parameter.required != required
                or parameter.default != default
            ):
                raise DefinitionError(
                    "graph_view",
                    f"graph view parameter {name!r} must be "
                    f"type={parameter_type!r}, required={required!r}, default={default!r}",
                    file=path,
                    path=f"parameters.{name}",
                )
        self._validate_graph_projection(definition, path)

    def _validate_graph_orphans_view(self, definition: ViewDefinition, path: Path) -> None:
        """Keep orphan reports bounded and independent of ad-hoc SQL."""

        expected = {"limit": ("integer", False, 50)}
        actual = set(definition.parameters)
        if actual != set(expected):
            raise DefinitionError(
                "graph_orphans_view",
                "graph_orphans view parameters must be exactly limit",
                file=path,
                path="parameters",
            )
        parameter = definition.parameters["limit"]
        parameter_type, required, default = expected["limit"]
        if (
            parameter.type != parameter_type
            or parameter.required != required
            or parameter.default != default
        ):
            raise DefinitionError(
                "graph_orphans_view",
                "graph_orphans view parameter 'limit' must be type='integer', "
                "required=False, default=50",
                file=path,
                path="parameters.limit",
            )
        self._validate_graph_projection(definition, path)

    def _validate_graph_projection(self, definition: ViewDefinition, path: Path) -> None:
        """Validate the small canonical contract used by graph-derived reads."""

        projection = definition.graph
        assert projection is not None  # Guaranteed by ViewDefinition.
        try:
            edges = self.collections[(projection.edges, self.active_collections[projection.edges])]
        except KeyError as exc:
            raise DefinitionError(
                "reference",
                f"graph references unknown active edge collection {projection.edges!r}",
                file=path,
                path="graph.edges",
            ) from exc
        if edges.mode != "event":
            raise DefinitionError(
                "graph_edge_collection",
                f"graph edge collection {projection.edges!r} must use mode='event'",
                file=path,
                path="graph.edges",
            )
        for role in ("subject", "object", "predicate"):
            name = getattr(projection, role)
            field = edges.fields.get(name)
            if field is None:
                raise DefinitionError(
                    "graph_field",
                    f"graph edge collection {projection.edges!r} must declare "
                    f"its {role} field {name!r}",
                    file=path,
                    path=f"graph.{role}",
                )
            if field.type != "string" or not field.filter:
                raise DefinitionError(
                    "graph_field",
                    f"graph {role} field {name!r} must use type='string' and filter=true",
                    file=path,
                    path=f"graph.{role}",
                )
            annotations = field_annotation_names(field)
            if annotations and not set(annotations) & set(edges.required_processors):
                raise DefinitionError(
                    "required_annotation",
                    f"graph {role} field {name!r} uses optional annotation {annotations[0]!r}",
                    file=path,
                    path=f"graph.{role}",
                )
        if projection.nodes is None:
            return
        try:
            nodes = self.collections[(projection.nodes, self.active_collections[projection.nodes])]
        except KeyError as exc:
            raise DefinitionError(
                "reference",
                f"graph references unknown active node collection {projection.nodes!r}",
                file=path,
                path="graph.nodes",
            ) from exc
        if nodes.mode not in {"keyed", "mixed"}:
            raise DefinitionError(
                "graph_node_collection",
                f"graph node collection {projection.nodes!r} must support keyed records",
                file=path,
                path="graph.nodes",
            )

    def _render_static(self, value: Any, parameters: Mapping[str, ParameterDefinition]) -> Any:
        from memseek.templates import render_object

        examples: dict[str, Any] = {}
        for name, parameter in parameters.items():
            examples[name] = self._parameter_example(parameter)
        return render_object(value, examples)

    @staticmethod
    def _parameter_example(parameter: ParameterDefinition) -> Any:
        """Choose one declared-valid value for static template validation."""

        if parameter.default is not None:
            return parameter.default
        if parameter.enum is not None:
            return parameter.enum[0]
        if parameter.type == "string":
            minimum = parameter.min_length if parameter.min_length is not None else 1
            maximum = parameter.max_length
            return "x" * (min(minimum, maximum) if maximum is not None else minimum)
        if parameter.type == "string_array":
            minimum = parameter.min_items if parameter.min_items is not None else 1
            maximum = parameter.max_items
            return ["example"] * (min(minimum, maximum) if maximum is not None else minimum)
        if parameter.type == "number":
            if parameter.minimum is not None:
                return parameter.minimum
            if parameter.maximum is not None:
                return parameter.maximum
            return 1.5
        if parameter.type == "integer":
            if parameter.minimum is not None:
                return int(parameter.minimum)
            if parameter.maximum is not None:
                return int(parameter.maximum)
            return 1
        if parameter.type == "boolean":
            return True
        return "2026-01-01T00:00:00Z"

    def _validate_search_spec(
        self,
        spec: SearchSpec,
        path: Path,
        field: str,
        explicitly_required: Iterable[str] = (),
        *,
        allow_templates: bool = False,
    ) -> None:
        if spec.q is not None and len(spec.q) > self.settings.max_query_chars:
            raise DefinitionError(
                "limit", "q exceeds MAX_QUERY_CHARS", file=path, path=f"{field}.q"
            )
        sources = (
            spec.sources
            if spec.sources is not None
            else (
                SearchSource(
                    name="source",
                    mode=self._single_mode(spec),
                    scope=spec.scope or {},
                    where=spec.where,
                    order_by=spec.order_by,
                    params=spec.params,
                    rank=spec.rank,
                    k=spec.k,
                ),
            )
        )
        all_collections: list[CollectionDefinition] = []
        for source in sources:
            collections = source.scope.collections
            if not collections:
                raise DefinitionError(
                    "search_scope",
                    "definition search sources must explicitly declare collections",
                    file=path,
                    path=field,
                )
            resolved = self._search_scope_collections(source, path, field)
            all_collections.extend(resolved)
            profiles = {self.bindings.get(item.name, item.search_profile) for item in resolved}
            if len(profiles) != 1:
                raise DefinitionError(
                    "search_profile",
                    "one source must resolve to exactly one search profile",
                    file=path,
                    path=field,
                )
            profile_name = next(iter(profiles))
            profile = self.search_profiles.get(profile_name)
            if profile is None:
                raise DefinitionError(
                    "reference", f"unknown search profile {profile_name!r}", file=path, path=field
                )
            descriptor = backend_descriptor(profile.backend)
            needed = required_capabilities(source.mode) | frozenset(explicitly_required)
            missing = needed - descriptor.capabilities
            if missing:
                raise DefinitionError(
                    "capability",
                    f"backend {profile.backend!r} lacks {sorted(missing)}",
                    file=path,
                    path=field,
                )
            if source.rank is not None:
                try:
                    validate_rank_expression(
                        source.rank, mode=source.mode, scorer_names=self.score_owners
                    )
                except RankValidationError as exc:
                    raise DefinitionError(exc.code, str(exc), file=path, path=field) from exc
            self._validate_structured_fields(
                source, resolved, path, field, allow_templates=allow_templates
            )
        if spec.boost is not None:
            try:
                validate_rank_expression(spec.boost, scorer_names=self.score_owners, boost=True)
            except RankValidationError as exc:
                raise DefinitionError(exc.code, str(exc), file=path, path=field) from exc
        self._validate_search_projections(spec, tuple(all_collections), path, field)

    @staticmethod
    def _single_mode(spec: SearchSpec) -> SearchMode:
        assert spec.mode is not None
        return spec.mode

    def _search_scope_collections(
        self, source: SearchSource, path: Path, field: str
    ) -> tuple[CollectionDefinition, ...]:
        resolved: list[CollectionDefinition] = []
        for name in source.scope.collections:
            if name == "_system":
                raise DefinitionError(
                    "reserved", "named definitions cannot query _system", file=path, path=field
                )
            pinned = source.scope.collection_versions.get(name)
            if pinned:
                versions = pinned
            else:
                versions = tuple(
                    version for candidate, version in self.collections if candidate == name
                )
            if not versions:
                raise DefinitionError(
                    "reference", f"unknown collection {name!r}", file=path, path=field
                )
            for version in versions:
                try:
                    resolved.append(self.collections[(name, version)])
                except KeyError as exc:
                    raise DefinitionError(
                        "reference", f"unknown collection {name}@{version}", file=path
                    ) from exc
        return tuple(resolved)

    def _validate_structured_fields(
        self,
        source: SearchSource,
        collections: tuple[CollectionDefinition, ...],
        path: Path,
        field: str,
        *,
        allow_templates: bool,
    ) -> None:
        requested = set(source.where) | {order.field for order in source.order_by}
        for name in requested:
            declarations = [collection.fields.get(name) for collection in collections]
            if any(declaration is None for declaration in declarations):
                raise DefinitionError(
                    "field_reference",
                    f"field {name!r} is not declared by every source collection version",
                    file=path,
                    path=field,
                )
            typed = [declaration for declaration in declarations if declaration is not None]
            signatures = {
                (declaration.type, declaration.filter, declaration.sort) for declaration in typed
            }
            if len(signatures) != 1:
                raise DefinitionError(
                    "field_compatibility",
                    f"field {name!r} has incompatible source declarations",
                    file=path,
                    path=field,
                )
            if name in source.where and not typed[0].filter:
                raise DefinitionError(
                    "field_permission", f"field {name!r} is not filterable", file=path, path=field
                )
            if any(order.field == name for order in source.order_by) and not typed[0].sort:
                raise DefinitionError(
                    "field_permission", f"field {name!r} is not sortable", file=path, path=field
                )
            predicate = source.where.get(name, {})
            for collection, declaration in zip(collections, typed, strict=True):
                chain = field_annotation_names(declaration)
                if (
                    chain
                    and set(predicate) != {"exists"}
                    and not set(chain) & set(collection.required_processors)
                ):
                    raise DefinitionError(
                        "required_annotation",
                        f"field {name!r} depends on optional annotation {chain[0]!r}",
                        file=path,
                        path=field,
                    )
            self._validate_predicate(
                name,
                predicate,
                typed[0],
                path,
                field,
                allow_templates=allow_templates,
            )

    @staticmethod
    def _is_template_operand(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        stripped = value.strip()
        if not (stripped.startswith("{{") and stripped.endswith("}}")):
            return False
        try:
            refs = template_references(stripped)
        except TemplateError:
            return False
        return len(refs) == 1 and stripped.count("{{") == 1

    @staticmethod
    def _scalar_matches(field_type: str, value: Any) -> bool:
        if field_type == "string":
            return isinstance(value, str)
        if field_type == "boolean":
            return isinstance(value, bool)
        if field_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if field_type == "number":
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float("-inf") < float(value) < float("inf")
            )
        if isinstance(value, datetime):
            return value.tzinfo is not None
        if not isinstance(value, str):
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            return False

    @classmethod
    def _validate_predicate(
        cls,
        name: str,
        predicate: dict[str, Any],
        declaration: DeclaredField,
        path: Path,
        field: str,
        *,
        allow_templates: bool = False,
    ) -> None:
        operators = set(predicate)
        if operators & {"gt", "gte", "lt", "lte"} and (
            declaration.is_array or declaration.scalar_type not in {"number", "integer", "datetime"}
        ):
            raise DefinitionError(
                "field_operator",
                f"range predicate is invalid for field {name!r}",
                file=path,
                path=field,
            )
        if "in" in operators and declaration.is_array:
            raise DefinitionError(
                "field_operator",
                f"in predicate requires a scalar field, not {name!r}",
                file=path,
                path=field,
            )
        if operators & {"contains_any", "contains_all"} and not declaration.is_array:
            raise DefinitionError(
                "field_operator",
                f"contains predicate requires array field {name!r}",
                file=path,
                path=field,
            )
        for operator, operand in predicate.items():
            if allow_templates and cls._is_template_operand(operand):
                continue
            if operator == "exists":
                valid = isinstance(operand, bool)
            elif operator in {"in", "contains_any", "contains_all"}:
                valid = (
                    isinstance(operand, (list, tuple))
                    and bool(operand)
                    and all(cls._scalar_matches(declaration.scalar_type, item) for item in operand)
                )
            elif declaration.is_array:
                valid = (
                    operator == "eq"
                    and isinstance(operand, (list, tuple))
                    and all(cls._scalar_matches(declaration.scalar_type, item) for item in operand)
                )
            else:
                valid = cls._scalar_matches(declaration.scalar_type, operand)
            if not valid:
                raise DefinitionError(
                    "field_operand",
                    f"operand for {name}.{operator} does not match {declaration.type!r}",
                    file=path,
                    path=field,
                )

    def _validate_search_projections(
        self,
        spec: SearchSpec,
        collections: tuple[CollectionDefinition, ...],
        path: Path,
        field: str,
    ) -> None:
        for name in spec.fields:
            declarations = [collection.fields.get(name) for collection in collections]
            if not declarations or any(item is None or not item.project for item in declarations):
                raise DefinitionError(
                    "field_permission",
                    f"projected field {name!r} is not declared/projectable everywhere",
                    file=path,
                    path=f"{field}.fields",
                )
            for collection, declaration in zip(collections, declarations, strict=True):
                assert declaration is not None
                if declaration.path.startswith("annotations."):
                    processor = declaration.path.split(".", 2)[1]
                    if processor not in collection.required_processors:
                        raise DefinitionError(
                            "required_annotation",
                            f"projected field {name!r} uses optional annotation {processor!r}",
                            file=path,
                            path=f"{field}.fields",
                        )
        for processor in spec.annotations:
            if processor not in self.processors:
                raise DefinitionError(
                    "reference",
                    f"unknown requested annotation {processor!r}",
                    file=path,
                    path=f"{field}.annotations",
                )
            missing = [
                f"{collection.name}@{collection.version}"
                for collection in collections
                if processor not in collection.required_processors
            ]
            if missing:
                raise DefinitionError(
                    "required_annotation",
                    f"requested annotation {processor!r} is optional/missing in {missing}",
                    file=path,
                    path=f"{field}.annotations",
                )

    def _load_artifacts(self) -> None:
        for path in _optional_yaml_files(self.settings.artifacts_dir):
            root = _mapping(load_yaml_file(path), path, "artifact file")
            for index, raw in enumerate(_sequence(root, "artifacts", path)):
                definition = _parse(ArtifactDefinition, raw, path, context=f"artifacts[{index}]")
                key = (definition.name, definition.version)
                self._duplicate("artifact", key, path)
                self._validate_artifact(definition, path)
                self.artifacts[key] = _hashed(definition)
                if definition.active:
                    self._set_active(
                        self.active_artifacts,
                        definition.name,
                        definition.version,
                        "artifact",
                        path,
                    )
        if not self.artifacts and self.settings.artifacts_dir is not None:
            raise DefinitionError(
                "empty_catalog", "no artifacts found", file=self.settings.artifacts_dir
            )
        # A learning target names another artifact, so it resolves only after
        # every version in the family is loaded.
        for key, artifact in self.artifacts.items():
            if artifact.learning is not None:
                self._validate_learning_reference(artifact, self.paths[("artifact", key)])

    def _validate_artifact(self, definition: ArtifactDefinition, path: Path) -> None:
        allowed = set(definition.parameters) | set(definition.blocks)
        try:
            refs = require_known_references(
                definition.template, allowed, context="artifact template"
            )
        except TemplateError as exc:
            raise DefinitionError(
                exc.code, str(exc), file=path, path=exc.path or "template"
            ) from exc
        referenced_blocks = {ref.split(".", 1)[0] for ref in refs} & set(definition.blocks)
        missing_blocks = set(definition.blocks) - referenced_blocks
        if missing_blocks:
            raise DefinitionError(
                "unused_block",
                f"artifact template omits block(s): {sorted(missing_blocks)}",
                file=path,
            )
        if (
            sum(block.max_tokens for block in definition.blocks.values())
            > self.settings.max_artifact_render_tokens
        ):
            raise DefinitionError(
                "budget", "artifact block budgets exceed MAX_ARTIFACT_RENDER_TOKENS", file=path
            )
        for name, block in definition.blocks.items():
            if block.document is not None:
                try:
                    require_known_references(
                        block.document.model_dump(mode="python"),
                        set(definition.parameters),
                        context=f"blocks.{name}.document",
                    )
                except TemplateError as exc:
                    raise DefinitionError(
                        exc.code, str(exc), file=path, path=f"blocks.{name}"
                    ) from exc
                rendered_document = self._render_static(
                    block.document.model_dump(mode="python"), definition.parameters
                )
                if (
                    not isinstance(rendered_document.get("entity"), str)
                    or not rendered_document["entity"]
                ):
                    raise DefinitionError(
                        "parameter_type",
                        "document entity must render to a non-empty string",
                        file=path,
                        path=f"blocks.{name}.document.entity",
                    )
                for collection in block.document.collections:
                    if collection not in self.active_collections:
                        raise DefinitionError(
                            "reference",
                            f"unknown document collection {collection!r}",
                            file=path,
                            path=f"blocks.{name}",
                        )
            else:
                assert block.view is not None
                view_name, view_version = split_exact_reference(block.view)
                view = self.views.get((view_name, int(view_version)))
                if view is None:
                    raise DefinitionError(
                        "reference",
                        f"unknown view {block.view!r}",
                        file=path,
                        path=f"blocks.{name}",
                    )
                unknown_args = set(block.args) - set(view.parameters)
                missing_args = {
                    parameter
                    for parameter, config in view.parameters.items()
                    if config.required and parameter not in block.args
                }
                if unknown_args or missing_args:
                    raise DefinitionError(
                        "view_args",
                        f"invalid view args; unknown={sorted(unknown_args)}, missing={sorted(missing_args)}",
                        file=path,
                        path=f"blocks.{name}.args",
                    )
                try:
                    require_known_references(
                        block.args,
                        set(definition.parameters),
                        context=f"blocks.{name}.args",
                    )
                except TemplateError as exc:
                    raise DefinitionError(
                        exc.code, str(exc), file=path, path=f"blocks.{name}"
                    ) from exc
                rendered_args = self._render_static(block.args, definition.parameters)
                for argument, value in rendered_args.items():
                    if not parameter_value_matches(view.parameters[argument], value):
                        raise DefinitionError(
                            "parameter_type",
                            f"argument {argument!r} does not match view parameter type",
                            file=path,
                            path=f"blocks.{name}.args.{argument}",
                        )
        if definition.snapshot is not None:
            target = self.collections.get(
                (
                    definition.snapshot.collection,
                    self.active_collections.get(definition.snapshot.collection, -1),
                )
            )
            if target is None or target.mode not in {"keyed", "mixed"}:
                raise DefinitionError(
                    "snapshot_target",
                    "artifact snapshot target must be an active keyed collection",
                    file=path,
                )
            if "entity" not in definition.parameters and not definition.snapshot.entity:
                raise DefinitionError(
                    "snapshot_target",
                    "artifact without entity parameter must declare snapshot.entity",
                    file=path,
                )
            if definition.snapshot.entity is not None:
                try:
                    require_known_references(
                        definition.snapshot.entity,
                        set(definition.parameters),
                        context="snapshot.entity",
                    )
                except TemplateError as exc:
                    raise DefinitionError(
                        exc.code, str(exc), file=path, path="snapshot.entity"
                    ) from exc
                rendered_entity = self._render_static(
                    definition.snapshot.entity, definition.parameters
                )
                if not isinstance(rendered_entity, str) or not rendered_entity:
                    raise DefinitionError(
                        "parameter_type",
                        "snapshot.entity must render to a non-empty string",
                        file=path,
                        path="snapshot.entity",
                    )
        if definition.learning is not None:
            target = definition.blocks[definition.learning.target_block]
            # A learning target must name an exact promoted value, so it can
            # only be a keyed document read of the active heads.  A view block
            # is a ranked selection whose membership is not a promotable unit.
            if target.document is None:
                raise DefinitionError(
                    "learning_target",
                    "learning.target_block must be a document block",
                    file=path,
                    path="learning.target_block",
                )
            if target.document.status != "active":
                raise DefinitionError(
                    "learning_target",
                    "learning.target_block must read active records",
                    file=path,
                    path=f"blocks.{definition.learning.target_block}.document.status",
                )
            if not target.required:
                raise DefinitionError(
                    "learning_target",
                    "learning.target_block must be a required block",
                    file=path,
                    path=f"blocks.{definition.learning.target_block}.required",
                )
        if definition.lifecycle == "reviewed":
            processor = self.derivations.get(definition.candidate_processor or "")
            document_collections = {
                collection
                for block in definition.blocks.values()
                if block.document is not None
                for collection in block.document.collections
            }
            if (
                processor is None
                or not processor.emit.complete
                or processor.emit.review != "required"
                or set(processor.emit.keys) != set(definition.complete_keys)
                or processor.emit.collection not in document_collections
                or processor.emit.type != definition.kind
            ):
                raise DefinitionError(
                    "artifact_lifecycle",
                    "reviewed artifact processor must emit the linked complete reviewed value",
                    file=path,
                )

    def _load_mcps(self) -> None:
        """Load package-curated MCP interfaces from their own definition family."""

        directory = self.settings.mcp_dir
        # MCP is opt-in at the package level.  A catalog without an ``mcp/``
        # directory remains a valid catalog whose packages expose no tools.
        if directory is None or not directory.exists():
            return
        for path in yaml_files(directory):
            raw = _mapping(load_yaml_file(path), path, "MCP definition")
            definition = _parse(McpDefinition, raw, path)
            key = (definition.name, definition.version)
            self._duplicate("mcp", key, path)
            self._validate_mcp_targets(definition, path)
            self.mcps[key] = _hashed(definition)

    def _validate_mcp_targets(self, definition: McpDefinition, path: Path) -> None:
        for index, tool in enumerate(definition.tools):
            if tool.kind == "view":
                assert tool.view is not None
                target = tool.view
                kind = "view"
                catalog: Mapping[tuple[str, int], Any] = self.views
            elif tool.kind == "artifact":
                assert tool.artifact is not None
                target = tool.artifact
                kind = "artifact"
                catalog = self.artifacts
            elif tool.kind == "ingest":
                assert tool.collection is not None
                target = tool.collection
                kind = "collection"
                catalog = self.collections
            else:
                continue
            # McpToolDefinition already enforces exact syntax; keeping this
            # defensive conversion turns programmatic construction errors into
            # normal startup DefinitionErrors as well.
            try:
                name, version = split_exact_reference(target)
            except ValueError as exc:
                raise DefinitionError(
                    "mcp_reference", str(exc), file=path, path=f"tools[{index}].{kind}"
                ) from exc
            if (name, int(version)) not in catalog:
                raise DefinitionError(
                    "reference",
                    f"MCP tool {tool.name!r} references unknown {kind} {target!r}",
                    file=path,
                    path=f"tools[{index}].{kind}",
                )

    def _load_packages(self) -> None:
        for path in _optional_yaml_files(self.settings.packages_dir):
            raw = _mapping(load_yaml_file(path), path, "package")
            documents = raw.pop("packages", None) if set(raw) == {"packages"} else None
            if documents is None:
                documents = [raw]
            if not isinstance(documents, list):
                raise DefinitionError("shape", "packages must be a list", file=path)
            for index, document in enumerate(documents):
                definition = _parse(PackageDefinition, document, path, context=f"packages[{index}]")
                key = (definition.name, definition.version)
                self._duplicate("package", key, path)
                self._validate_package(definition, path)
                self.packages[key] = _hashed(definition)

    def _validate_package(self, definition: PackageDefinition, path: Path) -> None:
        exact_groups: tuple[tuple[str, tuple[str, ...], Mapping[tuple[str, int], Any]], ...] = (
            ("collection", definition.collections, self.collections),
            ("view", definition.views, self.views),
            ("artifact", definition.artifacts, self.artifacts),
        )
        for kind, references, catalog in exact_groups:
            for reference in references:
                try:
                    name, version = split_exact_reference(reference)
                except ValueError as exc:
                    raise DefinitionError("package_reference", str(exc), file=path) from exc
                if (name, int(version)) not in catalog:
                    raise DefinitionError(
                        "reference", f"package references unknown {kind} {reference!r}", file=path
                    )
        if definition.mcp is not None:
            try:
                mcp_name, mcp_version = split_exact_reference(definition.mcp)
            except ValueError as exc:
                raise DefinitionError("package_reference", str(exc), file=path, path="mcp") from exc
            mcp = self.mcps.get((mcp_name, int(mcp_version)))
            if mcp is None:
                raise DefinitionError(
                    "reference",
                    f"package references unknown MCP {definition.mcp!r}",
                    file=path,
                    path="mcp",
                )
            self._validate_package_mcp_binding(definition, mcp, path)
        for processor in definition.processors:
            if processor not in self.processors and processor not in self.derivations:
                raise DefinitionError(
                    "reference", f"package references unknown processor {processor!r}", file=path
                )
        for trigger in definition.triggers:
            if trigger not in self.triggers:
                raise DefinitionError(
                    "reference", f"package references unknown trigger {trigger!r}", file=path
                )
        for profile in (*definition.search_profiles, *definition.optional_search_profiles):
            if profile not in self.search_profiles:
                raise DefinitionError(
                    "reference", f"package references unknown search profile {profile!r}", file=path
                )
        for retention in definition.retentions:
            try:
                collection_name, collection_version = split_exact_reference(retention.collection)
            except ValueError as exc:
                raise DefinitionError("package_reference", str(exc), file=path) from exc
            collection_key = (collection_name, int(collection_version))
            collection = self.collections.get(collection_key)
            if collection is None:
                raise DefinitionError(
                    "reference",
                    f"retention {retention.name!r} references unknown collection "
                    f"{retention.collection!r}",
                    file=path,
                )
            if retention.collection not in definition.collections:
                raise DefinitionError(
                    "package_dependency",
                    f"retention {retention.name!r} targets collection "
                    f"{retention.collection!r} omitted from its package",
                    file=path,
                )
            if collection.mode == "event":
                raise DefinitionError(
                    "retention",
                    f"retention {retention.name!r} requires a keyed or mixed collection",
                    file=path,
                )
            try:
                croniter(retention.cron, datetime.now(UTC))
            except (KeyError, ValueError) as exc:
                raise DefinitionError(
                    "cron", str(exc), file=path, path=f"retentions.{retention.name}.cron"
                ) from exc
        self._validate_package_closure(definition, path)

    @staticmethod
    def _validate_package_mcp_binding(
        package: PackageDefinition,
        mcp: McpDefinition,
        path: Path,
    ) -> None:
        """Ensure MCP tools cannot widen the package's declared surface."""

        package_views = set(package.views)
        package_artifacts = set(package.artifacts)
        for index, tool in enumerate(mcp.tools):
            if tool.kind == "view":
                assert tool.view is not None
                if tool.view not in package_views:
                    raise DefinitionError(
                        "package_dependency",
                        f"MCP tool {tool.name!r} targets view {tool.view!r} omitted from its package",
                        file=path,
                        path=f"mcp.tools[{index}].view",
                    )
            elif tool.kind == "artifact":
                assert tool.artifact is not None
                if tool.artifact not in package_artifacts:
                    raise DefinitionError(
                        "package_dependency",
                        f"MCP tool {tool.name!r} targets artifact {tool.artifact!r} "
                        "omitted from its package",
                        file=path,
                        path=f"mcp.tools[{index}].artifact",
                    )

    def _validate_package_closure(self, package: PackageDefinition, path: Path) -> None:
        collection_keys = {
            (name, int(version))
            for reference in package.collections
            for name, version in [split_exact_reference(reference)]
        }
        view_keys = {
            (name, int(version))
            for reference in package.views
            for name, version in [split_exact_reference(reference)]
        }
        artifact_keys = {
            (name, int(version))
            for reference in package.artifacts
            for name, version in [split_exact_reference(reference)]
        }
        processors = set(package.processors)
        triggers = set(package.triggers)
        required_profiles = set(package.search_profiles)
        all_profiles = required_profiles | set(package.optional_search_profiles)

        def require_collection(key: tuple[str, int], reason: str) -> None:
            if key not in collection_keys:
                raise DefinitionError(
                    "package_dependency",
                    f"package omits collection {key[0]}@{key[1]} required by {reason}",
                    file=path,
                )

        def require_processor(name: str, reason: str) -> None:
            if name not in processors:
                raise DefinitionError(
                    "package_dependency",
                    f"package omits processor {name!r} required by {reason}",
                    file=path,
                )

        def require_profile(name: str, reason: str, *, optional_ok: bool = True) -> None:
            available = all_profiles if optional_ok else required_profiles
            if name not in available:
                raise DefinitionError(
                    "package_dependency",
                    f"package omits search profile {name!r} required by {reason}",
                    file=path,
                )

        for key in collection_keys:
            collection = self.collections[key]
            for processor in (
                *collection.required_processors,
                *collection.optional_processors,
            ):
                require_processor(processor, f"collection {collection.name}@{collection.version}")
            require_profile(
                collection.search_profile,
                f"collection {collection.name}@{collection.version} default binding",
                optional_ok=False,
            )
            for profile in collection.allowed_search_profiles:
                require_profile(
                    profile,
                    f"collection {collection.name}@{collection.version} allowed binding",
                )

        for name in processors:
            if name in self.processors:
                continue
            derivation = self.derivations.get(name)
            if derivation is None:
                continue
            for source_name, source in derivation.sources.items():
                if isinstance(source, StreamSource | CurrentSource):
                    source_collections = source.collections
                    version_map = source.collection_versions
                elif isinstance(source, RecordSource):
                    assert source.collection_version is not None
                    require_collection(
                        (source.collection, source.collection_version),
                        f"processor {name!r} source {source_name!r}",
                    )
                    continue
                else:
                    continue
                for collection_name in source_collections:
                    if collection_name == "_system":
                        continue
                    versions = version_map.get(collection_name)
                    keys = (
                        {(collection_name, version) for version in versions}
                        if versions
                        else {(collection_name, self.active_collections[collection_name])}
                    )
                    for key in keys:
                        require_collection(key, f"processor {name!r} source {source_name!r}")
            assert derivation.emit.collection_version is not None
            require_collection(
                (derivation.emit.collection, derivation.emit.collection_version),
                f"processor {name!r} emission",
            )

        for name in triggers:
            trigger = self.triggers[name]
            require_processor(trigger.processor, f"trigger {name!r} target")
            if trigger.accumulator is not None:
                metric = trigger.accumulator.metric
                if isinstance(metric, str):
                    metric_name = metric
                elif metric.scorer is not None:
                    metric_name = metric.scorer
                else:
                    metric_name = metric.annotation
                if metric_name is not None and metric_name != "count":
                    require_processor(metric_name, f"trigger {name!r} accumulator")
            for condition, scope in trigger.observed_scopes.items():
                for collection_name in scope.collections:
                    if collection_name == "_system":
                        continue
                    versions = scope.collection_versions.get(collection_name)
                    keys = (
                        {(collection_name, version) for version in versions}
                        if versions
                        else {(collection_name, self.active_collections[collection_name])}
                    )
                    for key in keys:
                        require_collection(key, f"trigger {name!r} {condition} scope")

        for key in view_keys:
            view = self.views[key]
            if view.kind in {"graph", "graph_orphans"}:
                assert view.graph is not None
                names = (
                    (view.graph.edges,)
                    if view.graph.nodes is None
                    else (view.graph.edges, view.graph.nodes)
                )
                collections = tuple(
                    self.collections[(name, self.active_collections[name])] for name in names
                )
            else:
                assert view.query is not None
                spec = SearchSpec.model_validate(self._render_static(view.query, view.parameters))
                collections = self._package_collections_for_search_spec(
                    spec, path, f"view {view.name}"
                )
            for collection in collections:
                require_collection(
                    (collection.name, collection.version),
                    f"view {view.name}@{view.version}",
                )
                require_profile(
                    collection.search_profile,
                    f"view {view.name}@{view.version}",
                    optional_ok=False,
                )

        for key in artifact_keys:
            artifact = self.artifacts[key]
            if artifact.candidate_processor is not None:
                require_processor(
                    artifact.candidate_processor,
                    f"artifact {artifact.name}@{artifact.version}",
                )
            for block in artifact.blocks.values():
                if block.document is not None:
                    for collection_name in block.document.collections:
                        require_collection(
                            (collection_name, self.active_collections[collection_name]),
                            f"artifact {artifact.name}@{artifact.version} document block",
                        )
                else:
                    assert block.view is not None
                    view_name, view_version = split_exact_reference(block.view)
                    view_key = (view_name, int(view_version))
                    if view_key not in view_keys:
                        raise DefinitionError(
                            "package_dependency",
                            f"package omits view {block.view!r} required by artifact "
                            f"{artifact.name}@{artifact.version}",
                            file=path,
                        )
            if artifact.snapshot is not None:
                snapshot_version = self.active_collections[artifact.snapshot.collection]
                require_collection(
                    (artifact.snapshot.collection, snapshot_version),
                    f"artifact {artifact.name}@{artifact.version} snapshot",
                )
            if artifact.learning is not None:
                learning_name, learning_version = split_exact_reference(artifact.learning.artifact)
                if (learning_name, int(learning_version)) not in artifact_keys:
                    raise DefinitionError(
                        "package_dependency",
                        f"package omits artifact {artifact.learning.artifact!r} named as the "
                        f"learning target of {artifact.name}@{artifact.version}",
                        file=path,
                    )

        used_profiles = {
            profile
            for key in collection_keys
            for profile in self.collections[key].all_search_profiles
        }
        orphan_profiles = all_profiles - used_profiles
        if orphan_profiles:
            raise DefinitionError(
                "package_dependency",
                f"package search profiles are not allowed by any included collection: "
                f"{sorted(orphan_profiles)}",
                file=path,
            )
        for profile_name in required_profiles:
            profile = self.search_profiles[profile_name]
            descriptor = backend_descriptor(profile.backend)
            if profile.enabled_if_credentials and not descriptor.usable(self.settings):
                raise DefinitionError(
                    "profile_unavailable",
                    f"required package profile {profile_name!r} lacks credentials",
                    file=path,
                )

    def _all_collection_keys(self, name: str) -> set[tuple[str, int]]:
        return {key for key in self.collections if key[0] == name}

    def _collections_for_search_spec(
        self, spec: SearchSpec, path: Path, field: str
    ) -> tuple[CollectionDefinition, ...]:
        sources = (
            spec.sources
            if spec.sources is not None
            else (
                SearchSource(
                    name="source",
                    mode=self._single_mode(spec),
                    scope=spec.scope or {},
                    where=spec.where,
                    order_by=spec.order_by,
                    params=spec.params,
                    rank=spec.rank,
                    k=spec.k,
                ),
            )
        )
        return tuple(
            collection
            for source in sources
            for collection in self._search_scope_collections(source, path, field)
        )

    def _package_collections_for_search_spec(
        self, spec: SearchSpec, path: Path, field: str
    ) -> tuple[CollectionDefinition, ...]:
        sources = (
            spec.sources
            if spec.sources is not None
            else (
                SearchSource(
                    name="source",
                    mode=self._single_mode(spec),
                    scope=spec.scope or {},
                    where=spec.where,
                    order_by=spec.order_by,
                    params=spec.params,
                    rank=spec.rank,
                    k=spec.k,
                ),
            )
        )
        resolved: list[CollectionDefinition] = []
        for source in sources:
            for name in source.scope.collections:
                versions = source.scope.collection_versions.get(name) or (
                    self.active_collections[name],
                )
                for version in versions:
                    try:
                        resolved.append(self.collections[(name, version)])
                    except KeyError as exc:
                        raise DefinitionError(
                            "reference",
                            f"unknown collection {name}@{version}",
                            file=path,
                            path=field,
                        ) from exc
        return tuple(resolved)

    def _load_overrides(self) -> None:
        overrides = DeploymentOverrides()
        path = self.settings.search_profile_overrides_file
        if path is not None:
            raw = load_yaml_file(path, required=False)
            if raw is not None:
                overrides = _parse(DeploymentOverrides, raw, path)
        collection_names = {name for name, _ in self.collections}
        for name in sorted(collection_names):
            active = self.collections[(name, self.active_collections[name])]
            selected = overrides.collection_profiles.get(name, active.search_profile)
            incompatible_versions = [
                version
                for (collection_name, version), definition in self.collections.items()
                if collection_name == name and selected not in definition.all_search_profiles
            ]
            if incompatible_versions:
                raise DefinitionError(
                    "deployment_binding",
                    f"profile {selected!r} is not allowed by every version of {name!r}; "
                    f"incompatible versions={sorted(incompatible_versions)}",
                    file=path,
                )
            profile = self.search_profiles.get(selected)
            if profile is None:
                raise DefinitionError(
                    "reference", f"unknown override profile {selected!r}", file=path
                )
            descriptor = backend_descriptor(profile.backend)
            if profile.enabled_if_credentials and not descriptor.usable(self.settings):
                raise DefinitionError(
                    "profile_unavailable",
                    f"selected profile {selected!r} lacks credentials",
                    file=path,
                )
            self.bindings[name] = selected
        unknown = set(overrides.collection_profiles) - collection_names
        if unknown:
            raise DefinitionError(
                "reference",
                f"overrides reference unknown collections: {sorted(unknown)}",
                file=path,
            )

    def _validate_global_graph(self) -> None:
        assert self.models is not None
        assert self.rank_defaults is not None
        collection_names = {name for name, _ in self.collections}
        missing_active = collection_names - set(self.active_collections)
        if missing_active:
            raise DefinitionError(
                "active_missing",
                f"collections lack active versions: {sorted(missing_active)}",
            )
        for key, collection in self.collections.items():
            path = self.paths[("collection", key)]
            for profile in collection.all_search_profiles:
                if profile not in self.search_profiles:
                    raise DefinitionError(
                        "reference",
                        f"collection references unknown search profile {profile!r}",
                        file=path,
                    )
            for processor_name in (
                *collection.required_processors,
                *collection.optional_processors,
            ):
                processor = self.processors.get(processor_name)
                if processor is None:
                    raise DefinitionError(
                        "reference",
                        f"collection references unknown processor {processor_name!r}",
                        file=path,
                    )
                if collection.name not in processor.input.collections:
                    raise DefinitionError(
                        "processor_scope",
                        f"processor {processor_name!r} cannot consume collection {collection.name!r}",
                        file=path,
                    )
                if processor_name in collection.required_processors and processor.input.types:
                    raise DefinitionError(
                        "required_processor_scope",
                        f"required processor {processor_name!r} excludes admitted record types",
                        file=path,
                    )
                if (
                    processor_name in collection.required_processors
                    and processor.kind == "json"
                    and processor.default_output is None
                ):
                    raise DefinitionError(
                        "required_processor_default",
                        f"required processor {processor_name!r} has no terminal default",
                        file=path,
                    )

        collection_names = {name for name, _ in self.collections}
        for processor in self.processors.values():
            missing_inputs = set(processor.input.collections) - collection_names
            if missing_inputs:
                raise DefinitionError(
                    "reference",
                    f"processor {processor.name!r} references unknown collections: "
                    f"{sorted(missing_inputs)}",
                    file=self.paths[("processor", processor.name)],
                    path="input.collections",
                )
            if processor.model is not None:
                alias = self._alias(
                    processor.model,
                    self.paths[("processor", processor.name)],
                    f"processors.{processor.name}.model",
                )
                self._validate_step_params(alias, {}, self.paths[("processor", processor.name)], 0)

        # A catalog with no definitions at all has no scores to name, and that
        # is the state a service runs in until a workspace publishes one. The
        # setting is still checked against every catalog that declares
        # anything, which is where a wrong score name actually does harm.
        if self.collections and self.settings.context_doc_order_score not in self.score_owners:
            raise DefinitionError(
                "reference",
                f"CONTEXT_DOC_ORDER_SCORE names unknown score "
                f"{self.settings.context_doc_order_score!r}",
            )
        for name, trigger in self.triggers.items():
            target = self.derivations.get(trigger.processor)
            path = self.paths[("trigger", name)]
            if target is None:
                raise DefinitionError(
                    "reference",
                    f"trigger targets unknown derive processor {trigger.processor!r}",
                    file=path,
                )
            self._validate_trigger(trigger, target, path, "trigger")

        # Revalidate named search definitions after deployment bindings are known.
        for key, view in self.views.items():
            self._validate_view(view, self.paths[("view", key)])

        for derivation in self.derivations.values():
            self._validate_context_views(derivation)

        self._validate_automatic_graph()
        self._build_processor_config_hashes()

    def _validate_learning_reference(self, definition: ArtifactDefinition, path: Path) -> None:
        """Bind a learning target to the reviewed artifact that owns its promotion."""

        learning = definition.learning
        assert learning is not None
        name, version = split_exact_reference(learning.artifact)
        target = self.artifacts.get((name, int(version)))
        if target is None:
            raise DefinitionError(
                "reference",
                f"unknown learning artifact {learning.artifact!r}",
                file=path,
                path="learning.artifact",
            )
        if target.lifecycle != "reviewed":
            raise DefinitionError(
                "learning_target",
                f"learning artifact {learning.artifact!r} is not reviewed; only a reviewed "
                "artifact has a promotion lifecycle a candidate can target",
                file=path,
                path="learning.artifact",
            )
        block = definition.blocks[learning.target_block]
        assert block.document is not None  # checked when the artifact was parsed
        owned = {
            collection
            for owner_block in target.blocks.values()
            if owner_block.document is not None
            for collection in owner_block.document.collections
        }
        unowned = sorted(set(block.document.collections) - owned)
        if unowned:
            raise DefinitionError(
                "learning_target",
                f"learning target block reads collection(s) {unowned} that reviewed artifact "
                f"{learning.artifact!r} does not maintain",
                file=path,
                path=f"blocks.{learning.target_block}.document.collections",
            )

    def _validate_context_views(self, derivation: PipelineDefinition) -> None:
        """Resolve view sources once every view definition is loaded."""

        path = self.paths[("processor", derivation.name)]
        for name, source in derivation.sources.items():
            if not isinstance(source, ViewSource):
                continue
            view = self._resolve_context_view(source, name, path)
            unknown = sorted(set(source.params) - set(view.parameters))
            if unknown:
                raise DefinitionError(
                    "view_parameter",
                    f"view source {name!r} supplies unknown parameter(s): {unknown}",
                    file=path,
                    path=f"sources.{name}.params",
                )
            for parameter_name, parameter in view.parameters.items():
                supplied = source.params.get(parameter_name)
                if supplied is None:
                    if parameter.required:
                        raise DefinitionError(
                            "view_parameter",
                            f"view source {name!r} omits required parameter {parameter_name!r}",
                            file=path,
                            path=f"sources.{name}.params",
                        )
                    continue
                if self._is_template_operand(supplied):
                    continue  # resolved per run from subject/run values
                if not parameter_value_matches(parameter, supplied):
                    raise DefinitionError(
                        "view_parameter",
                        f"view source {name!r} parameter {parameter_name!r} does not "
                        f"match type {parameter.type!r}",
                        file=path,
                        path=f"sources.{name}.params.{parameter_name}",
                    )

    def _resolve_context_view(self, source: ViewSource, name: str, path: Path) -> ViewDefinition:
        reference = source.view
        if "@" in reference:
            try:
                view_name, version = split_exact_reference(reference)
            except ValueError as exc:
                raise DefinitionError(
                    "reference", str(exc), file=path, path=f"sources.{name}.view"
                ) from exc
            key = (view_name, int(version))
        else:
            active = self.active_views.get(reference)
            if active is None:
                raise DefinitionError(
                    "reference",
                    f"view source {name!r} references unknown view {reference!r}",
                    file=path,
                    path=f"sources.{name}.view",
                )
            key = (reference, active)
        view = self.views.get(key)
        if view is None:
            raise DefinitionError(
                "reference",
                f"view source {name!r} references unknown view {key[0]}@{key[1]}",
                file=path,
                path=f"sources.{name}.view",
            )
        return view

    def _validate_automatic_graph(self) -> None:
        edges: dict[str, set[str]] = {name: set() for name in self.derivations}
        for producer_name, producer in self.derivations.items():
            emission = producer.emit
            for consumer_name, consumer in self.derivations.items():
                triggers = [
                    trigger
                    for trigger in self.triggers.values()
                    if trigger.processor == consumer_name
                ]

                def emission_activates(
                    scope: Any,
                    emission: Any = emission,
                    producer: str = producer_name,
                    consumer: str = consumer_name,
                ) -> bool:
                    if producer == consumer and getattr(scope, "ignore_own_outputs", False):
                        return False
                    return (
                        emission.collection in scope.collections
                        and (not scope.types or emission.type in scope.types)
                        and emission_status(emission) in scope.statuses
                        and (
                            not scope.collection_versions.get(emission.collection)
                            or emission.collection_version
                            in scope.collection_versions[emission.collection]
                        )
                    )

                for trigger in triggers:
                    scope_matches = any(
                        emission_activates(scope) for scope in trigger.observed_scopes.values()
                    )
                    input_matches = emission_activates(consumer.driver)
                    guarded_matches = (
                        trigger.accumulator is not None
                        or trigger.read
                        or trigger.census is not None
                        or trigger.lifecycle is not None
                    ) and input_matches
                    if scope_matches or guarded_matches:
                        edges[producer_name].add(consumer_name)
        indegree = dict.fromkeys(edges, 0)
        for targets in edges.values():
            for target in targets:
                indegree[target] += 1
        queue = deque(sorted(name for name, degree in indegree.items() if degree == 0))
        distance = dict.fromkeys(edges, 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for target in sorted(edges[node]):
                distance[target] = max(distance[target], distance[node] + 1)
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(edges):
            cycle_nodes = sorted(name for name, degree in indegree.items() if degree > 0)
            raise DefinitionError(
                "automatic_cycle",
                f"automatic derivation dependency cycle: {cycle_nodes}",
            )
        if distance and max(distance.values()) > self.settings.max_derivation_depth:
            raise DefinitionError(
                "automatic_depth",
                "automatic derivation path exceeds MAX_DERIVATION_DEPTH",
            )

    def _build_processor_config_hashes(self) -> None:
        assert self.models is not None
        for name, definition in {
            **self.processors,
            **self.derivations,
        }.items():
            alias_names: set[str] = set()
            embedding: dict[str, Any] | None = None
            if isinstance(definition, ProcessorDefinition):
                if definition.model is not None:
                    alias_names.add(definition.model)
                elif definition.kind == "embedding":
                    # An embedding processor's output is only comparable within
                    # one endpoint/model/dimension/space, so the whole embedding
                    # declaration is part of what its annotations mean.
                    embedding = self.models.embedding.model_dump(mode="json")
            elif isinstance(definition, PipelineDefinition):
                for task in definition.tasks:
                    config = task_adapter(task.use).validate_config(task.config)
                    if isinstance(config, LLMTaskConfig):
                        alias_names.add(
                            config.model or definition.model or self.models.defaults.derivation
                        )
            aliases = {
                alias: self.models.aliases[alias].model_dump(mode="json")
                for alias in sorted(alias_names)
            }
            task_hashes = (
                task_implementation_hashes([task.use for task in definition.tasks])
                if isinstance(definition, PipelineDefinition)
                else {}
            )
            self.processor_config_hashes[name] = sha256_canonical(
                {
                    "definition": _dump(definition),
                    "resolved_aliases": aliases,
                    "resolved_embedding": embedding,
                    "task_implementations": task_hashes,
                }
            )

    def _freeze(self) -> DefinitionCatalog:
        assert self.models is not None
        assert self.rank_defaults is not None
        rank_hash = sha256_canonical(self.rank_defaults.model_dump(mode="json"))
        payload = {
            "models": self.models.model_dump(mode="json"),
            "processors": [_dump(self.processors[name]) for name in sorted(self.processors)],
            "rank": self.rank_defaults.model_dump(mode="json"),
            "search_profiles": [
                _dump(self.search_profiles[name]) for name in sorted(self.search_profiles)
            ],
            "collections": [_dump(self.collections[key]) for key in sorted(self.collections)],
            "derivations": [_dump(self.derivations[name]) for name in sorted(self.derivations)],
            "triggers": [
                self.triggers[name].model_dump(mode="json", exclude={"definition_hash"})
                for name in sorted(self.triggers)
            ],
            "views": [_dump(self.views[key]) for key in sorted(self.views)],
            "artifacts": [_dump(self.artifacts[key]) for key in sorted(self.artifacts)],
            "mcps": [_dump(self.mcps[key]) for key in sorted(self.mcps)],
            "packages": [_dump(self.packages[key]) for key in sorted(self.packages)],
            "active": {
                "collections": dict(sorted(self.active_collections.items())),
                "views": dict(sorted(self.active_views.items())),
                "artifacts": dict(sorted(self.active_artifacts.items())),
            },
            "deployment_bindings": dict(sorted(self.bindings.items())),
            "task_implementations": task_implementation_hashes(
                [task.use for definition in self.derivations.values() for task in definition.tasks]
            ),
        }
        frozen_models = deep_freeze(self.models)
        frozen_processors = {name: deep_freeze(value) for name, value in self.processors.items()}
        frozen_rank = deep_freeze(self.rank_defaults)
        frozen_profiles = {name: deep_freeze(value) for name, value in self.search_profiles.items()}
        frozen_collections = {key: deep_freeze(value) for key, value in self.collections.items()}
        frozen_derivations = {name: deep_freeze(value) for name, value in self.derivations.items()}
        frozen_triggers = {name: deep_freeze(value) for name, value in self.triggers.items()}
        frozen_views = {key: deep_freeze(value) for key, value in self.views.items()}
        frozen_artifacts = {key: deep_freeze(value) for key, value in self.artifacts.items()}
        frozen_mcps = {key: deep_freeze(value) for key, value in self.mcps.items()}
        frozen_packages = {key: deep_freeze(value) for key, value in self.packages.items()}
        return DefinitionCatalog(
            models=frozen_models,
            processors=MappingProxyType(frozen_processors),
            score_names=frozenset(self.score_owners),
            score_owners=MappingProxyType(dict(self.score_owners)),
            rank_defaults=frozen_rank,
            rank_hash=rank_hash,
            search_profiles=MappingProxyType(frozen_profiles),
            collections=MappingProxyType(frozen_collections),
            derivations=MappingProxyType(frozen_derivations),
            triggers=MappingProxyType(frozen_triggers),
            views=MappingProxyType(frozen_views),
            artifacts=MappingProxyType(frozen_artifacts),
            mcps=MappingProxyType(frozen_mcps),
            packages=MappingProxyType(frozen_packages),
            deployment_bindings=MappingProxyType(dict(self.bindings)),
            active_collections=MappingProxyType(dict(self.active_collections)),
            active_views=MappingProxyType(dict(self.active_views)),
            active_artifacts=MappingProxyType(dict(self.active_artifacts)),
            processor_config_hashes=MappingProxyType(dict(self.processor_config_hashes)),
            catalog_hash=sha256_canonical(payload),
        )


def load_definition_catalog(
    settings: Settings,
    *,
    source: DefinitionSources | None = None,
) -> DefinitionCatalog:
    """Load and globally validate configured YAML or Python definitions."""

    # A direct catalog compile must see the same trusted Task registry as API
    # and worker startup.  Importing a module is idempotent, so their explicit
    # startup imports remain safe and external callers cannot compile a
    # catalog that will fail only after deployment.
    import_task_modules(settings.task_modules)
    if source is not None:
        return source.compile(settings)
    return _CatalogBuilder(settings).build()


def compile_definition_catalog(
    settings: Settings,
    source: DefinitionSources,
) -> DefinitionCatalog:
    """Compile an in-memory Python definition source through startup validation."""

    return source.compile(settings)
