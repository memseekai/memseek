"""An explicit, navigable catalog source map, shared by disk and uploads."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import Field, ValidationError

from memseek.config import Settings

from .base import PublicName, SemVer, StrictModel
from .errors import DefinitionError
from .models import TombstoneRetention
from .yaml import load_yaml_text

if TYPE_CHECKING:
    from .loader import DefinitionCatalog


FAMILIES = (
    "collections",
    "processors",
    "derivations",
    "triggers",
    "views",
    "artifacts",
    "computers",
    "programs",
    "agents",
    "context_policies",
    "toolsets",
    "mcp",
)
SINGLE = {"derivations", "triggers", "mcp"}
UNVERSIONED = {"processors", "derivations", "triggers"}


class CatalogConfig(StrictModel):
    models: str = Field(description="Catalog-relative path to model aliases and providers.")
    ranking: str = Field(description="Catalog-relative path to ranking configuration.")
    search_profiles: str = Field(description="Catalog-relative path to search profiles.")


class CatalogManifest(StrictModel):
    """The table of contents of one complete, installable release."""

    name: PublicName
    version: SemVer
    description: str = ""
    config: CatalogConfig
    collections: dict[str, str] = Field(default_factory=dict)
    processors: dict[str, str] = Field(default_factory=dict)
    derivations: dict[str, str] = Field(default_factory=dict)
    triggers: dict[str, str] = Field(default_factory=dict)
    views: dict[str, str] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    computers: dict[str, str] = Field(default_factory=dict)
    programs: dict[str, str] = Field(default_factory=dict)
    agents: dict[str, str] = Field(default_factory=dict)
    context_policies: dict[str, str] = Field(default_factory=dict)
    toolsets: dict[str, str] = Field(default_factory=dict)
    mcp: dict[str, str] = Field(default_factory=dict)
    expose_mcp: str | None = Field(default=None, description="Explicit MCP interface to expose.")
    optional_search_profiles: tuple[PublicName, ...] = ()
    retentions: tuple[TombstoneRetention, ...] = ()

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"


def source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix not in {".yaml", ".yml"}
        or value != path.as_posix()
    ):
        raise DefinitionError("file_path", f"expected a catalog-relative YAML path, got {value!r}")
    if value == "catalog.yaml":
        raise DefinitionError("file_path", "catalog.yaml cannot include itself")
    return value


def yaml_locations(text: str) -> dict[str, tuple[int, int]]:
    """Index YAML field paths without adding metadata to semantic values."""
    result: dict[str, tuple[int, int]] = {}
    node = yaml.compose(text)

    def visit(item: Any, path: str, ancestors: frozenset[int]) -> None:
        if item is None or id(item) in ancestors:
            return
        result[path] = (item.start_mark.line + 1, item.start_mark.column + 1)
        ancestors = ancestors | {id(item)}
        if isinstance(item, yaml.MappingNode):
            for key, value in item.value:
                child = f"{path}.{key.value}" if path else key.value
                visit(value, child, ancestors)
        elif isinstance(item, yaml.SequenceNode):
            for index, value in enumerate(item.value):
                visit(value, f"{path}[{index}]", ancestors)

    visit(node, "", frozenset())
    return result


def located(error: DefinitionError, files: Mapping[str, str]) -> DefinitionError:
    if error.line is not None or error.file is None or error.file not in files:
        return error
    try:
        locations = yaml_locations(files[error.file])
    except yaml.YAMLError:
        return error
    path = error.path
    while path not in locations and path:
        parent = re.sub(r"(?:\.[^.\[]+|\[\d+\])$", "", path)
        path = parent if parent != path else ""
    line, column = locations.get(path, (1, 1))
    return DefinitionError(
        error.code, error.message, file=error.file, path=error.path, line=line, column=column
    )


def parse_manifest(text: str) -> CatalogManifest:
    raw = load_yaml_text(text, source="catalog.yaml")
    try:
        return CatalogManifest.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        raise located(
            DefinitionError(
                "schema", error["msg"], file="catalog.yaml", path=".".join(map(str, error["loc"]))
            ),
            {"catalog.yaml": text},
        ) from exc


def discover_catalog(directory: Path | None = None) -> Path:
    if directory is not None:
        root = directory.resolve()
        if not (root / "catalog.yaml").is_file():
            raise DefinitionError(
                "catalog_file",
                "expected catalog.yaml; migrate and republish legacy catalogs",
                file=root / "catalog.yaml",
            )
        return root
    current = Path.cwd().resolve()
    for root in (current, *current.parents):
        if (root / "catalog.yaml").is_file():
            return root
    raise DefinitionError("catalog_file", "no catalog.yaml found in this directory or its parents")


def read_catalog_files(root: Path) -> dict[str, str]:
    root = discover_catalog(root)
    entry = root / "catalog.yaml"
    if not entry.resolve().is_relative_to(root):
        raise DefinitionError("file_path", "entry point escapes catalog root", file=entry)
    files = {"catalog.yaml": entry.read_text(encoding="utf-8")}
    manifest = parse_manifest(files["catalog.yaml"])
    paths = set(manifest.config.model_dump().values())
    for family in FAMILIES:
        paths.update(getattr(manifest, family).values())
    for name in sorted(paths):
        name = source_path(name)
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise DefinitionError("file_path", "source escapes catalog root", file=name)
        try:
            files[name] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DefinitionError("file_missing", str(exc), file=name) from exc
    return files


@dataclass(frozen=True)
class CatalogSources:
    manifest: CatalogManifest
    documents: dict[str, tuple[tuple[Path, Any], ...]]
    locations: tuple[dict[str, Any], ...]


def resolve_sources(files: Mapping[str, str]) -> CatalogSources:
    if "catalog.yaml" not in files:
        raise DefinitionError(
            "catalog_file",
            "catalog.yaml is required; migrate and republish legacy catalogs",
            file="catalog.yaml",
        )
    manifest = parse_manifest(files["catalog.yaml"])
    parsed: dict[str, Any] = {}
    owners: dict[str, str] = {}
    documents: dict[str, tuple[tuple[Path, Any], ...]] = {}
    locations: list[dict[str, Any]] = []

    def read(name: str, owner: str) -> Any:
        name = source_path(name)
        if name not in files:
            raise DefinitionError(
                "file_missing",
                f"source file {name!r} does not exist",
                file="catalog.yaml",
                path=owner,
            )
        if name in owners and owners[name] != owner:
            raise DefinitionError(
                "source_family",
                f"{name!r} is already used by {owners[name]}",
                file="catalog.yaml",
                path=owner,
            )
        owners[name] = owner
        if name not in parsed:
            parsed[name] = load_yaml_text(files[name], source=name)
        return parsed[name]

    try:
        for key, field in (
            ("models", "models_file"),
            ("ranking", "rank_default_file"),
            ("search_profiles", "search_profiles_file"),
        ):
            name = getattr(manifest.config, key)
            documents[field] = ((Path(name), read(name, f"config.{key}")),)
        for family in FAMILIES:
            entries = getattr(manifest, family)
            selected = []
            found: set[str] = set()
            for name in sorted(set(entries.values())):
                raw = read(name, family)
                if family in SINGLE:
                    values = [raw]
                else:
                    if (
                        not isinstance(raw, dict)
                        or set(raw) != {family}
                        or not isinstance(raw[family], list)
                    ):
                        raise DefinitionError(
                            "shape", f"root must contain exactly a {family} list", file=name
                        )
                    values = raw[family]
                references = [
                    (
                        value.get("name")
                        if family in UNVERSIONED
                        else f"{value.get('name')}@{value.get('version')}"
                    )
                    for value in values
                    if isinstance(value, dict)
                ]
                actual = set(references)
                if len(actual) != len(references):
                    raise DefinitionError(
                        "duplicate", f"duplicate {family} definition in source file", file=name
                    )
                for ref, target in entries.items():
                    if target == name and ref not in actual:
                        candidates = difflib.get_close_matches(
                            ref, sorted(str(item) for item in actual), n=1
                        )
                        hint = f"; did you mean {candidates[0]!r}?" if candidates else ""
                        raise DefinitionError(
                            "source_reference",
                            f"{ref!r} is not defined in {name!r}{hint}",
                            file="catalog.yaml",
                            path=f"{family}.{ref}",
                        )
                marks = yaml_locations(files[name])
                for index, value in enumerate(values):
                    where = "" if family in SINGLE else f"{family}[{index}]"
                    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                        raise DefinitionError(
                            "schema", "definition requires a name", file=name, path=where
                        )
                    ref = (
                        value["name"]
                        if family in UNVERSIONED
                        else f"{value['name']}@{value.get('version')}"
                    )
                    if ref in found:
                        raise DefinitionError(
                            "duplicate",
                            f"duplicate {family} definition {ref!r}",
                            file=name,
                            path=where,
                        )
                    found.add(ref)
                    if entries.get(ref) != name:
                        raise DefinitionError(
                            "source_reference",
                            f"list {ref!r}: {name} under {family} in catalog.yaml",
                            file=name,
                            path=where,
                        )
                    line, column = marks.get(f"{where}.name" if where else "name", (1, 1))
                    locations.append(
                        {
                            "kind": family,
                            "reference": ref,
                            "file": name,
                            "line": line,
                            "column": column,
                        }
                    )
                selected.append((Path(name), raw))
            for ref in entries.keys() - found:
                hint = difflib.get_close_matches(ref, sorted(found), n=1)
                suffix = f"; did you mean {hint[0]!r}?" if hint else ""
                raise DefinitionError(
                    "source_reference",
                    f"{ref!r} is not defined in {entries[ref]!r}{suffix}",
                    file="catalog.yaml",
                    path=f"{family}.{ref}",
                )
            field = "processors_file" if family == "processors" else f"{family}_dir"
            documents[field] = tuple(selected)

        profiles_path, profiles_document = documents["search_profiles_file"][0]
        profiles = (
            profiles_document.get("profiles", {}) if isinstance(profiles_document, dict) else {}
        )
        if not isinstance(profiles, dict):
            raise DefinitionError("shape", "profiles must be a mapping", file=profiles_path)
        optional = set(manifest.optional_search_profiles)
        if optional - profiles.keys():
            raise DefinitionError(
                "reference",
                f"unknown optional search profiles: {sorted(optional - profiles.keys())}",
                file="catalog.yaml",
                path="optional_search_profiles",
            )
        package: dict[str, Any] = {"name": manifest.name, "version": manifest.version}
        for family in FAMILIES:
            if family not in {"derivations", "mcp"}:
                package[family] = sorted(getattr(manifest, family))
        package["processors"] = sorted([*package["processors"], *manifest.derivations])
        for _path, derivation in documents["derivations_dir"]:
            if derivation.get("trigger") is not None:
                package["triggers"].append(f"{derivation['name']}.default")
        package["triggers"].sort()
        package.update(
            search_profiles=sorted(profiles.keys() - optional),
            optional_search_profiles=sorted(optional),
            mcp=manifest.expose_mcp,
            retentions=[value.model_dump(mode="json") for value in manifest.retentions],
        )
        documents["packages_dir"] = ((Path("catalog.yaml"), package),)
        return CatalogSources(manifest, documents, tuple(locations))
    except DefinitionError as exc:
        raise located(exc, files) from exc


def compile_catalog_files(
    settings: Settings, files: Mapping[str, str], *, apply_overrides: bool = False
) -> DefinitionCatalog:
    from memseek.derive.tasks import import_task_modules

    from .loader import _CatalogBuilder

    source = resolve_sources(files)
    # Builder path fields are internal presence flags, never discovery inputs.
    configured: dict[str, Any] = {
        f"{family}_dir": Path() if source.documents.get(f"{family}_dir") else None
        for family in (*FAMILIES, "packages")
        if family != "processors"
    }
    configured["search_profile_overrides_file"] = None
    builder_settings = settings.model_copy(update=configured)
    import_task_modules(settings.task_modules)
    try:
        documents = dict(source.documents)
        if apply_overrides and settings.search_profile_overrides_file is not None:
            from .yaml import load_yaml_file

            path = settings.search_profile_overrides_file
            documents["search_profile_overrides_file"] = ((path, load_yaml_file(path)),)
            builder_settings = builder_settings.model_copy(
                update={"search_profile_overrides_file": path}
            )
        catalog = _CatalogBuilder(builder_settings, documents=documents).build()
        locations = list(source.locations)
        for location in source.locations:
            if location["kind"] == "derivations":
                locations.append({**location, "kind": "processors"})
                trigger = f"{location['reference']}.default"
                if trigger in catalog.triggers:
                    locations.append({**location, "kind": "triggers", "reference": trigger})
        return replace(
            catalog, source_locations=tuple(MappingProxyType(item) for item in locations)
        )
    except DefinitionError as exc:
        raise located(exc, files) from exc
