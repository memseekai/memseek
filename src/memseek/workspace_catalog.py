"""Workspace-scoped definition package storage and compilation."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import (
    DefinitionCatalog,
    DefinitionError,
    PackageDefinition,
    load_definition_catalog,
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
from memseek.definitions.yaml import load_yaml_text
from memseek.locks import acquire_workspace_lock

_MAX_FILES = 256
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_CATALOG_DIRECTORIES = {
    "collections",
    "derivations",
    "triggers",
    "views",
    "artifacts",
    "mcp",
    "packages",
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


def _copy_source(source: Path | None, destination: Path) -> None:
    # Unconfigured means the deployment ships no definitions of this kind, so
    # the overlay gets an empty directory rather than inheriting whatever is on
    # disk next to the service.
    if source is None:
        destination.mkdir(parents=True, exist_ok=True)
        return
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise WorkspaceCatalogError("base_catalog", f"definition path does not exist: {source}")


def _copy_fragments(source: Path | None, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if source is None:
        return
    if source.is_dir():
        for path in sorted(
            (*source.glob("*.yaml"), *source.glob("*.yml")), key=lambda item: item.name
        ):
            shutil.copy2(path, destination / f"base-{path.name}")
    elif source.is_file():
        shutil.copy2(source, destination / "base.yaml")
    else:
        raise WorkspaceCatalogError("base_catalog", f"definition path does not exist: {source}")


def _write_user_file(root: Path, relative: str, text: str) -> None:
    path = PurePosixPath(relative)
    parts = path.parts
    if parts[0] in _CATALOG_DIRECTORIES:
        destination = root / parts[0] / Path(*parts[1:])
    elif parts[:2] == ("conf", "processors"):
        destination = root / "conf" / "processors" / Path(*parts[2:])
    elif parts[:2] == ("conf", "search_profiles"):
        destination = root / "conf" / "search_profiles" / Path(*parts[2:])
    elif relative == "conf/processors.yaml":
        destination = root / "conf" / "processors" / "user.yaml"
    elif relative == "conf/search_profiles.yaml":
        destination = root / "conf" / "search_profiles" / "user.yaml"
    elif relative in {"conf/models.yaml", "conf/rank_default.yaml"}:
        destination = root / Path(*parts)
    else:
        raise WorkspaceCatalogError("file_path", f"unsupported definition path {relative!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def _compile_overlay(settings: Settings, files: Mapping[str, str]) -> DefinitionCatalog:
    with tempfile.TemporaryDirectory(prefix="memseek-workspace-catalog-") as temporary:
        root = Path(temporary)
        user_definition_catalog = any(
            path.split("/", 1)[0] in _CATALOG_DIRECTORIES for path in files
        )
        for field, name in (
            ("collections_dir", "collections"),
            ("derivations_dir", "derivations"),
            ("triggers_dir", "triggers"),
            ("views_dir", "views"),
            ("artifacts_dir", "artifacts"),
            ("mcp_dir", "mcp"),
            ("packages_dir", "packages"),
        ):
            destination = root / name
            if user_definition_catalog:
                destination.mkdir(parents=True, exist_ok=True)
            elif field == "mcp_dir" and (
                getattr(settings, field) is None or not getattr(settings, field).exists()
            ):
                # MCP is optional for legacy/base catalogs.  Keep the overlay
                # layout deterministic without requiring an empty directory
                # in every deployment.
                destination.mkdir(parents=True, exist_ok=True)
            else:
                _copy_source(getattr(settings, field), destination)
        conf = root / "conf"
        conf.mkdir()
        models_path = conf / "models.yaml"
        rank_path = conf / "rank_default.yaml"
        if "conf/models.yaml" in files:
            _write_user_file(root, "conf/models.yaml", files["conf/models.yaml"])
        else:
            _copy_source(settings.models_file, models_path)
        if "conf/rank_default.yaml" in files:
            _write_user_file(root, "conf/rank_default.yaml", files["conf/rank_default.yaml"])
        else:
            _copy_source(settings.rank_default_file, rank_path)
        processors_path = conf / "processors"
        search_profiles_path = conf / "search_profiles"
        processors_path.mkdir(parents=True, exist_ok=True)
        search_profiles_path.mkdir(parents=True, exist_ok=True)
        if not user_definition_catalog and not any(
            path == "conf/processors.yaml" or path.startswith("conf/processors/") for path in files
        ):
            _copy_fragments(settings.processors_file, processors_path)
        if not any(
            path == "conf/search_profiles.yaml" or path.startswith("conf/search_profiles/")
            for path in files
        ):
            _copy_fragments(settings.search_profiles_file, search_profiles_path)
        for relative, text in files.items():
            _write_user_file(root, relative, text)
        compiled_settings = settings.model_copy(
            update={
                "models_file": models_path,
                "rank_default_file": rank_path,
                "processors_file": processors_path,
                "search_profiles_file": search_profiles_path,
                "collections_dir": root / "collections",
                "derivations_dir": root / "derivations",
                "triggers_dir": root / "triggers",
                "views_dir": root / "views",
                "artifacts_dir": root / "artifacts",
                "mcp_dir": root / "mcp",
                "packages_dir": root / "packages",
                "search_profile_overrides_file": None,
            }
        )
        try:
            return load_definition_catalog(compiled_settings)
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
        self._lock = asyncio.Lock()

    async def get(self, workspace: str) -> DefinitionCatalog:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "select catalog_hash, files from workspace_catalog where workspace = %s",
                (workspace,),
            )
            row = await result.fetchone()
        if row is None:
            # A workspace that published nothing falls back only to a catalog
            # the operator explicitly configured. With no configured catalog —
            # the shipped default — there is nothing to fall back to, and
            # saying so is far better than serving definitions that merely
            # happened to sit on disk beside the service.
            if not self.settings.has_configured_catalog:
                raise WorkspaceCatalogError(
                    "no_catalog",
                    f"workspace {workspace!r} has no published catalog; publish a package first",
                    status=409,
                )
            return self.default_catalog
        catalog_hash = str(row["catalog_hash"])
        cached = self._cache.get(workspace)
        if cached is not None and cached[0] == catalog_hash:
            return cached[1]
        files = row["files"]
        if not isinstance(files, Mapping):
            raise WorkspaceCatalogError(
                "catalog_storage", "stored workspace catalog is invalid", status=503
            )
        async with self._lock:
            cached = self._cache.get(workspace)
            if cached is not None and cached[0] == catalog_hash:
                return cached[1]
            catalog = _compile_overlay(self.settings, files)
            if catalog.catalog_hash != catalog_hash:
                raise WorkspaceCatalogError(
                    "catalog_storage", "stored catalog hash mismatch", status=503
                )
            self._cache[workspace] = (catalog_hash, catalog)
            return catalog

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
        """Resolve the one package whose declared interface a workspace may expose.

        An installed workspace catalog persists an exact package reference, and
        that is how a package is normally selected. A workspace that published
        nothing may use an explicitly configured catalog when it holds exactly
        one package — but a service with no configured catalog has no such
        fallback, so it exposes nothing rather than guessing.
        """

        selected_catalog = catalog if catalog is not None else await self.get(workspace)
        metadata = await self.metadata(workspace)
        package_metadata = metadata["package"]
        if package_metadata is not None:
            try:
                return selected_catalog.resolve_package(
                    str(package_metadata["name"]), str(package_metadata["version"])
                )
            except (KeyError, TypeError) as exc:
                raise WorkspaceCatalogError(
                    "catalog_storage", "stored workspace package is invalid", status=503
                ) from exc

        if metadata["source"] == "default" and len(selected_catalog.packages) == 1:
            return next(iter(selected_catalog.packages.values()))
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
        # Parse every submitted YAML once at the HTTP boundary. Compilation below
        # performs the complete family-specific validation and reference checks.
        for path, text in files.items():
            load_yaml_text(text, source=path)
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

        files, catalog, package_name, package_version = self._compile_request(request)
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
        files, catalog, package_name, package_version = self._compile_request(request)
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
