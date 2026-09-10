"""Workspace-scoped definition package storage and compilation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from weakref import WeakValueDictionary

from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import (
    DefinitionCatalog,
    DefinitionError,
    PackageDefinition,
)
from memseek.definitions.base import split_exact_reference
from memseek.definitions.compat import (
    Blocker,
    CompatibilityReport,
    HashRewrite,
    StoredGroup,
    classify_catalogs,
    plan_stored_groups,
)
from memseek.definitions.loader import _CatalogBuilder
from memseek.definitions.yaml import load_yaml_file, load_yaml_text, yaml_files
from memseek.derive.tasks import import_task_modules
from memseek.locks import acquire_workspace_lock

_MAX_FILES = 256
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_CATALOG_DIRECTORIES = {
    "agents",
    "collections",
    "computers",
    "context_policies",
    "derivations",
    "triggers",
    "views",
    "artifacts",
    "mcp",
    "packages",
    "programs",
    "toolsets",
}


class WorkspaceCatalogRequest(BaseModel):
    """Authenticated package upload accepted by ``POST /catalog``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str = Field(min_length=3, max_length=128)
    files: dict[str, str] = Field(min_length=1, max_length=_MAX_FILES)

    @field_validator("package")
    @classmethod
    def exact_package_reference(cls, value: str) -> str:
        try:
            split_exact_reference(value, semver=True)
        except ValueError as exc:
            raise ValueError("package must be an exact name@semver reference") from exc
        return value


class WorkspaceCatalogError(ValueError):
    """Expected catalog upload or compatibility failure."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status: int = 422,
        report: CompatibilityReport | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        # A refused publish carries the same report a preflight would have
        # returned, so the failure names every blocker instead of one sentence.
        self.report = report
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class WorkspaceCatalogResult:
    workspace: str
    package: str
    catalog_hash: str
    files: tuple[str, ...]
    rewritten_records: int = 0

    def as_json(self) -> dict[str, Any]:
        name, version = split_exact_reference(self.package, semver=True)
        return {
            "workspace": self.workspace,
            "package": {"name": name, "version": version},
            "catalog_hash": self.catalog_hash,
            "files": list(self.files),
            "loaded": True,
            "rewritten_records": self.rewritten_records,
        }


def _normalize_files(files: Mapping[str, str]) -> dict[str, str]:
    if not files:
        raise WorkspaceCatalogError("empty_files", "files must contain at least one YAML file")
    if len(files) > _MAX_FILES:
        raise WorkspaceCatalogError("file_limit", f"at most {_MAX_FILES} files may be uploaded")
    normalized: dict[str, str] = {}
    total = 0
    for raw_name, text in files.items():
        if not isinstance(raw_name, str) or not isinstance(text, str):
            raise WorkspaceCatalogError("file_shape", "files must map path strings to YAML text")
        name = PurePosixPath(raw_name)
        if "\x00" in raw_name or name.is_absolute() or ".." in name.parts or name.name in {"", "."}:
            raise WorkspaceCatalogError("file_path", f"invalid definition path {raw_name!r}")
        if name.suffix not in {".yaml", ".yml"}:
            raise WorkspaceCatalogError("file_type", f"definition path must be YAML: {raw_name!r}")
        if not name.parts or (
            name.parts[0] not in _CATALOG_DIRECTORIES
            and not (name.parts[0] == "conf" and len(name.parts) >= 2)
        ):
            raise WorkspaceCatalogError(
                "file_path",
                f"definition path is outside the catalog layout: {raw_name!r}",
            )
        size = len(text.encode("utf-8"))
        if size > _MAX_FILE_BYTES:
            raise WorkspaceCatalogError(
                "file_limit", f"definition file exceeds {_MAX_FILE_BYTES} bytes: {raw_name!r}"
            )
        total += size
        normalized[str(name)] = text
    if total > _MAX_TOTAL_BYTES:
        raise WorkspaceCatalogError(
            "file_limit", f"catalog upload exceeds {_MAX_TOTAL_BYTES} bytes"
        )
    return dict(sorted(normalized.items()))


def _compile_overlay(settings: Settings, files: Mapping[str, str]) -> DefinitionCatalog:
    # Parse even nested files that the family loaders do not select, matching
    # upload validation without a second YAML parse during compilation.
    parsed = {name: load_yaml_text(text, source=name) for name, text in files.items()}
    documents: dict[str, tuple[tuple[Path, Any], ...]] = {}
    configured: dict[str, Path | None] = {"search_profile_overrides_file": None}
    uploaded_sections = {path.split("/", 1)[0] for path in files}
    user_catalog = bool(uploaded_sections & _CATALOG_DIRECTORIES)

    def base_files(source: Path | None) -> tuple[Path, ...]:
        if source is None:
            return ()
        if source.is_dir():
            return yaml_files(source)
        if source.is_file():
            return (source,)
        raise WorkspaceCatalogError("base_catalog", f"definition path does not exist: {source}")

    for family in sorted(_CATALOG_DIRECTORIES):
        field = f"{family}_dir"
        configured[field] = Path(family)
        if user_catalog:
            if family not in uploaded_sections:
                configured[field] = None
            selected = {
                Path(name): value
                for name, value in parsed.items()
                if Path(name).parent == Path(family)
            }
        else:
            source = getattr(settings, field)
            # A base catalog that predates a family, or simply declares none,
            # is absent rather than broken.
            if family in {"mcp", "toolsets"} and (source is None or not source.exists()):
                source = None
            paths = base_files(source)
            if source is not None and source.is_file():
                raise WorkspaceCatalogError(
                    "definition",
                    str(
                        DefinitionError(
                            "directory_type", "definition path is not a directory", file=family
                        )
                    ),
                )
            selected = {Path(family) / path.name: load_yaml_file(path) for path in paths}
        documents[field] = tuple(sorted(selected.items()))

    for family in ("models", "rank_default", "processors", "search_profiles"):
        field = f"{family}_file"
        name = f"conf/{family}.yaml"
        configured[field] = Path(name)
        if family in {"models", "rank_default"}:
            value = parsed.get(name)
            if name not in parsed:
                source = getattr(settings, field)
                base_files(source)  # Preserve missing deployment-file diagnostics.
                value = load_yaml_file(source)
            documents[field] = ((Path(name), value),)
            continue
        selected = {}
        uploaded = name in parsed or any(key.startswith(f"conf/{family}/") for key in parsed)
        if not uploaded and (family != "processors" or not user_catalog):
            source = getattr(settings, field)
            for path in base_files(source):
                label = f"base-{path.name}" if source.is_dir() else "base.yaml"
                selected[Path(f"conf/{family}/{label}")] = load_yaml_file(path)
        for key, value in parsed.items():
            if key == name:
                selected[Path(f"conf/{family}/user.yaml")] = value
            elif Path(key).parent == Path(f"conf/{family}"):
                selected[Path(key)] = value
        documents[field] = tuple(sorted(selected.items()))

    for name in files:
        parts = PurePosixPath(name).parts
        if (
            parts[0] not in _CATALOG_DIRECTORIES
            and parts[:2] not in {("conf", "processors"), ("conf", "search_profiles")}
            and name
            not in {
                "conf/models.yaml",
                "conf/rank_default.yaml",
                "conf/processors.yaml",
                "conf/search_profiles.yaml",
            }
        ):
            raise WorkspaceCatalogError("file_path", f"unsupported definition path {name!r}")
    try:
        import_task_modules(settings.task_modules)
        return _CatalogBuilder(settings.model_copy(update=configured), documents=documents).build()
    except DefinitionError as exc:
        raise WorkspaceCatalogError("definition", str(exc)) from exc


async def _stored_groups(conn: DatabaseConnection, workspace: str) -> tuple[StoredGroup, ...]:
    """Every distinct public collection identity a workspace holds, with counts."""

    result = await conn.execute(
        """
        select collection, collection_version, collection_hash, count(*) as rows
        from record
        where workspace = %s and collection <> '_system'
        group by collection, collection_version, collection_hash
        order by collection, collection_version, collection_hash
        """,
        (workspace,),
    )
    return tuple(
        StoredGroup(
            collection=str(row["collection"]),
            version=int(row["collection_version"]),
            contract_hash=str(row["collection_hash"]),
            rows=int(row["rows"]),
        )
        for row in await result.fetchall()
    )


async def _verify_rewrites(
    conn: DatabaseConnection,
    workspace: str,
    rewrites: tuple[HashRewrite, ...],
    *,
    incoming: DefinitionCatalog,
    max_rows: int,
) -> tuple[tuple[HashRewrite, ...], tuple[Blocker, ...]]:
    """Check the stored values a newly declared property could contradict.

    A property added to a schema that already allowed arbitrary keys is only
    provably additive once the rows that carry that key are known to satisfy it.
    The scan is bounded: above ``max_rows`` the publish asks for a new collection
    version rather than reading an unbounded table inside a transaction.
    """

    accepted: list[HashRewrite] = []
    blockers: list[Blocker] = []
    for rewrite in rewrites:
        if not (rewrite.verify_keys or rewrite.verify_absent_annotations):
            accepted.append(rewrite)
            continue
        definition = incoming.collections[(rewrite.collection, rewrite.version)]
        properties = definition.content_schema.get("properties") or {}
        reasons: list[str] = []
        for annotation in rewrite.verify_absent_annotations:
            # Repointing a field onto a superseding annotation only preserves every
            # stored read while no row holds the newer annotation yet.
            present = await conn.execute(
                """
                select count(*) as present
                from record
                where workspace = %s and collection = %s and collection_version = %s
                  and collection_hash = %s and annotations ? %s
                """,
                (
                    workspace,
                    rewrite.collection,
                    rewrite.version,
                    rewrite.stored_hash,
                    annotation,
                ),
            )
            present_row = await present.fetchone()
            if present_row and int(present_row["present"]):
                reasons.append(
                    f"{present_row['present']} existing record(s) already hold a "
                    f"{annotation!r} annotation, so repointing the field would change "
                    "what they read"
                )
        for key in rewrite.verify_keys:
            subschema = properties.get(key)
            if subschema is None:  # pragma: no cover - the planner only names declared keys
                continue
            counted = await conn.execute(
                """
                select count(*) as present
                from record
                where workspace = %s and collection = %s and collection_version = %s
                  and collection_hash = %s and content ? %s
                """,
                (workspace, rewrite.collection, rewrite.version, rewrite.stored_hash, key),
            )
            counted_row = await counted.fetchone()
            present = int(counted_row["present"]) if counted_row else 0
            if present == 0:
                continue
            if present > max_rows:
                reasons.append(
                    f"{present} existing records already carry {key!r}, above the "
                    f"ADDITIVE_VERIFY_MAX_ROWS limit of {max_rows}"
                )
                continue
            validator = Draft202012Validator(subschema, format_checker=FormatChecker())
            values = await conn.execute(
                """
                select id::text as id, content -> %s as value
                from record
                where workspace = %s and collection = %s and collection_version = %s
                  and collection_hash = %s and content ? %s
                order by seq
                limit %s
                """,
                (
                    key,
                    workspace,
                    rewrite.collection,
                    rewrite.version,
                    rewrite.stored_hash,
                    key,
                    max_rows,
                ),
            )
            for row in await values.fetchall():
                error = next(iter(validator.iter_errors(row["value"])), None)
                if error is not None:
                    reasons.append(
                        f"record {row['id']} holds a {key!r} value the new schema rejects: "
                        f"{error.message}"
                    )
                    break
        if reasons:
            blockers.append(
                Blocker(
                    collection=rewrite.collection,
                    version=rewrite.version,
                    stored_hash=rewrite.stored_hash,
                    rows=rewrite.rows,
                    reasons=tuple(reasons),
                    required_action=(
                        f"declare the property in {rewrite.collection} version "
                        f"{rewrite.version + 1} instead, or correct the offending records first"
                    ),
                )
            )
            continue
        accepted.append(rewrite)
    return tuple(accepted), tuple(blockers)


async def _apply_rewrites(
    conn: DatabaseConnection, workspace: str, rewrites: tuple[HashRewrite, ...]
) -> int:
    """Move stored records onto their new contract hash. Returns rows rewritten."""

    total = 0
    for rewrite in rewrites:
        result = await conn.execute(
            """
            update record
            set collection_hash = %s
            where workspace = %s and collection = %s and collection_version = %s
              and collection_hash = %s
            """,
            (
                rewrite.target_hash,
                workspace,
                rewrite.collection,
                rewrite.version,
                rewrite.stored_hash,
            ),
        )
        total += int(result.rowcount or 0)
    return total


async def _annotation_vintage(
    conn: DatabaseConnection, workspace: str, catalog: DefinitionCatalog
) -> tuple[dict[str, Any], ...]:
    """Count annotations whose stored config hash is no longer the current one.

    This is what a changed processor prompt looks like from the data's side: the
    values are still there and still valid, they were simply produced by a
    configuration the catalog no longer describes.
    """

    expected = {name: catalog.processor_config_hashes[name] for name in sorted(catalog.processors)}
    if not expected:
        return ()
    result = await conn.execute(
        """
        select processor.name as processor,
               count(*) as stale
        from record
        cross join lateral (
          select key as name,
                 coalesce(
                   value ->> 'processor_config_hash',
                   value ->> 'config_hash',
                   value ->> 'processor_hash'
                 ) as stored_hash
          from jsonb_each(record.annotation_meta)
        ) processor
        left join lateral jsonb_each_text(%s) expected
          on expected.key = processor.name
        where record.workspace = %s
          and record.collection <> '_system'
          and processor.stored_hash is not null
          and expected.value is not null
          and processor.stored_hash <> expected.value
        group by processor.name
        order by processor.name
        """,
        (Jsonb(expected), workspace),
    )
    return tuple(
        {"processor": str(row["processor"]), "stale_annotations": int(row["stale"])}
        for row in await result.fetchall()
    )


class WorkspaceCatalogRegistry:
    """Resolve the immutable catalog selected by each workspace."""

    def __init__(
        self,
        pool: DatabasePool,
        settings: Settings,
        default_catalog: DefinitionCatalog,
    ) -> None:
        self.pool = pool
        self.settings = settings
        self.default_catalog = default_catalog
        self._cache: dict[str, tuple[str, DefinitionCatalog]] = {}
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def get(self, workspace: str) -> DefinitionCatalog:
        catalog, _package = await self.resolve(workspace)
        return catalog

    async def resolve(self, workspace: str) -> tuple[DefinitionCatalog, PackageDefinition | None]:
        # The CASE keeps large YAML payloads in PostgreSQL on a cache hit. Both
        # identities and any fetched documents belong to this one read snapshot.
        async with self._locks.setdefault(workspace, asyncio.Lock()):
            cached = self._cache.get(workspace)
            async with self.pool.connection() as conn:
                result = await conn.execute(
                    """
                    select catalog_hash, package_name, package_version,
                           case when catalog_hash = %s then null else files end as files
                    from workspace_catalog where workspace = %s
                    """,
                    (cached[0] if cached else None, workspace),
                )
                row = await result.fetchone()
            if row is None:
                self._cache.pop(workspace, None)
                if not self.settings.has_configured_catalog:
                    raise WorkspaceCatalogError(
                        "no_catalog",
                        f"workspace {workspace!r} has no published catalog; publish a package first",
                        status=409,
                    )
                catalog = self.default_catalog
                package = (
                    next(iter(catalog.packages.values())) if len(catalog.packages) == 1 else None
                )
                return catalog, package
            catalog_hash = str(row["catalog_hash"])
            if cached is not None and cached[0] == catalog_hash:
                catalog = cached[1]
            else:
                files = row["files"]
                if not isinstance(files, Mapping):
                    raise WorkspaceCatalogError(
                        "catalog_storage", "stored workspace catalog is invalid", status=503
                    )
                catalog = await asyncio.to_thread(_compile_overlay, self.settings, files)
                if catalog.catalog_hash != catalog_hash:
                    raise WorkspaceCatalogError(
                        "catalog_storage", "stored catalog hash mismatch", status=503
                    )
                self._cache[workspace] = (catalog_hash, catalog)
            try:
                package = catalog.resolve_package(
                    str(row["package_name"]), str(row["package_version"])
                )
            except (KeyError, TypeError) as exc:
                raise WorkspaceCatalogError(
                    "catalog_storage", "stored workspace package is invalid", status=503
                ) from exc
            return catalog, package

    async def metadata(self, workspace: str) -> dict[str, Any]:
        """Return the selected package identity without exposing YAML contents."""

        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                select package_name, package_version, catalog_hash,
                       jsonb_object_keys(files) as file_name
                from workspace_catalog
                where workspace = %s
                """,
                (workspace,),
            )
            rows = await result.fetchall()
        if not rows:
            # "none" is distinct from "default" on purpose: a caller must be
            # able to tell "this workspace published nothing and the service
            # offers nothing" from "this workspace published nothing and falls
            # back to the catalog the operator configured".
            if not self.settings.has_configured_catalog:
                return {
                    "workspace": workspace,
                    "source": "none",
                    "package": None,
                    "catalog_hash": None,
                    "files": [],
                }
            return {
                "workspace": workspace,
                "source": "default",
                "package": None,
                "catalog_hash": self.default_catalog.catalog_hash,
                "files": [],
            }
        first = rows[0]
        return {
            "workspace": workspace,
            "source": "workspace",
            "package": {"name": first["package_name"], "version": first["package_version"]},
            "catalog_hash": first["catalog_hash"],
            "files": sorted(str(row["file_name"]) for row in rows),
        }

    async def selected_package(
        self,
        workspace: str,
        *,
        catalog: DefinitionCatalog | None = None,
    ) -> PackageDefinition | None:
        """Resolve the currently selected package; retained for existing callers."""

        if catalog is None:
            _catalog, package = await self.resolve(workspace)
            return package
        metadata = await self.metadata(workspace)
        package = metadata["package"]
        if package is not None:
            try:
                return catalog.resolve_package(str(package["name"]), str(package["version"]))
            except (KeyError, TypeError) as exc:
                raise WorkspaceCatalogError(
                    "catalog_storage", "stored workspace package is invalid", status=503
                ) from exc
        if metadata["source"] == "default" and len(catalog.packages) == 1:
            return next(iter(catalog.packages.values()))
        return None

    def _compile_request(
        self, request: WorkspaceCatalogRequest
    ) -> tuple[
        dict[str, str],
        DefinitionCatalog,
        str,
        str,
    ]:
        """Normalize, parse, compile, and resolve one upload's declared package."""

        files = _normalize_files(request.files)
        package_name, package_version = split_exact_reference(request.package, semver=True)
        if not any(path.startswith("packages/") for path in files):
            raise WorkspaceCatalogError(
                "package_file", "upload must include a packages/*.yaml file"
            )
        catalog = _compile_overlay(self.settings, files)
        try:
            catalog.resolve_package(str(package_name), str(package_version))
        except KeyError as exc:
            raise WorkspaceCatalogError(
                "package_reference", f"uploaded package {request.package!r} was not loaded"
            ) from exc
        return files, catalog, str(package_name), str(package_version)

    async def _previous(self, workspace: str) -> DefinitionCatalog:
        """The catalog a publish is judged against, empty on a first publish.

        A workspace with nothing installed has nothing to be incompatible with,
        so publishing into it must not be blocked by the absence it is about to
        fix. Every other caller of `get` still gets the 409.
        """

        try:
            return await self.get(workspace)
        except WorkspaceCatalogError as exc:
            if exc.code != "no_catalog":
                raise
            return self.default_catalog

    async def preflight(
        self,
        workspace: str,
        request: WorkspaceCatalogRequest,
    ) -> tuple[CompatibilityReport, DefinitionCatalog, dict[str, str], str, str]:
        """Report what publishing this upload would do, without installing it.

        The report is produced by the same classifier and planner the publish
        itself uses, so a clean preflight is a genuine guarantee rather than an
        estimate.
        """

        files, catalog, package_name, package_version = await asyncio.to_thread(
            self._compile_request, request
        )
        previous = await self._previous(workspace)
        report = await self._compatibility(workspace, previous=previous, incoming=catalog)
        return report, catalog, files, package_name, package_version

    async def compatibility(self, workspace: str) -> CompatibilityReport:
        """Report the installed catalog's standing against its own stored records."""

        catalog = await self.get(workspace)
        return await self._compatibility(workspace, previous=catalog, incoming=catalog)

    async def _compatibility(
        self,
        workspace: str,
        *,
        previous: DefinitionCatalog,
        incoming: DefinitionCatalog,
    ) -> CompatibilityReport:
        async with self.pool.connection() as conn:
            groups = await _stored_groups(conn, workspace)
            rewrites, blockers = plan_stored_groups(groups, previous=previous, incoming=incoming)
            verified_rewrites, verification_blockers = await _verify_rewrites(
                conn,
                workspace,
                rewrites,
                incoming=incoming,
                max_rows=self.settings.additive_verify_max_rows,
            )
            vintage = await _annotation_vintage(conn, workspace, incoming)
        notes: list[str] = []
        if verified_rewrites:
            notes.append(
                f"{sum(item.rows for item in verified_rewrites)} record(s) have their stored "
                "contract hash rewritten forward on publish"
            )
        if vintage:
            notes.append(
                "annotations written under a superseded processor configuration are never "
                "recomputed; use a backfill to reach them"
            )
        return CompatibilityReport(
            workspace=workspace,
            changes=classify_catalogs(previous, incoming),
            rewrites=verified_rewrites,
            blockers=(*blockers, *verification_blockers),
            annotation_vintage=vintage,
            stored_rows=sum(group.rows for group in groups),
            notes=tuple(notes),
        )

    async def install(
        self,
        workspace: str,
        request: WorkspaceCatalogRequest,
    ) -> WorkspaceCatalogResult:
        files, catalog, package_name, package_version = await asyncio.to_thread(
            self._compile_request, request
        )
        async with self.pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, workspace)
            existing = await conn.execute(
                "select id from workspace where id = %s for share", (workspace,)
            )
            if await existing.fetchone() is None:
                raise WorkspaceCatalogError(
                    "workspace_not_found", "workspace does not exist", status=404
                )
            previous = await self._previous(workspace)
            groups = await _stored_groups(conn, workspace)
            rewrites, blockers = plan_stored_groups(groups, previous=previous, incoming=catalog)
            rewrites, verification_blockers = await _verify_rewrites(
                conn,
                workspace,
                rewrites,
                incoming=catalog,
                max_rows=self.settings.additive_verify_max_rows,
            )
            blockers = (*blockers, *verification_blockers)
            if blockers:
                report = CompatibilityReport(
                    workspace=workspace,
                    changes=classify_catalogs(previous, catalog),
                    blockers=blockers,
                    stored_rows=sum(group.rows for group in groups),
                )
                raise WorkspaceCatalogError(
                    "catalog_incompatible",
                    "existing records require a collection migration before replacement",
                    status=409,
                    report=report,
                )
            # Rewriting forward inside the publish transaction keeps exactly one
            # identity live for a version: either the whole publish lands or none
            # of it does.
            rewritten = await _apply_rewrites(conn, workspace, rewrites)
            await conn.execute(
                """
                insert into workspace_catalog (
                  workspace, package_name, package_version, catalog_hash, files
                ) values (%s, %s, %s, %s, %s)
                on conflict (workspace) do update set
                  package_name = excluded.package_name,
                  package_version = excluded.package_version,
                  catalog_hash = excluded.catalog_hash,
                  files = excluded.files,
                  updated_at = clock_timestamp()
                """,
                (workspace, package_name, package_version, catalog.catalog_hash, Jsonb(files)),
            )
        self._cache[workspace] = (catalog.catalog_hash, catalog)
        return WorkspaceCatalogResult(
            workspace=workspace,
            package=request.package,
            catalog_hash=catalog.catalog_hash,
            files=tuple(files),
            rewritten_records=rewritten,
        )

    async def clear(self, workspace: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await acquire_workspace_lock(conn, workspace)
            await conn.execute("delete from workspace_catalog where workspace = %s", (workspace,))
        self._cache.pop(workspace, None)


__all__ = [
    "WorkspaceCatalogError",
    "WorkspaceCatalogRegistry",
    "WorkspaceCatalogRequest",
    "WorkspaceCatalogResult",
]
