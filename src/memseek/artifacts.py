"""Deterministic artifact rendering, snapshotting, and snapshot reads.

An artifact definition is a versioned render recipe over document and named
view blocks.  The renderer resolves blocks in declaration order under hard
token budgets, makes no LLM calls, and returns an exact provenance manifest.
Snapshotting persists one rendering as an ordinary record behind an
`operation=materialize` run so freshness and erasure use ordinary record
semantics.

Every character a model sees comes from the author's `template`.  The renderer
escapes block rows and parameter values so neither can close or forge an
element, then substitutes them literally; it adds no fence, attribute, or
explanatory sentence of its own.  An author who wants retrieved rows marked as
untrusted data writes that element around the block reference in the template,
where it is visible next to the instructions it qualifies.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

from memseek import __version__
from memseek.canonical_records import CanonicalRecordWrite, insert_canonical_record_tx
from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import ArtifactDefinition, DefinitionCatalog, parameters_json_schema
from memseek.definitions.base import split_exact_reference
from memseek.definitions.models import parameter_value_matches
from memseek.locks import acquire_entity_locks, acquire_workspace_lock
from memseek.render import RenderableRecord, escape_untrusted, render_record, render_rows
from memseek.search.named_views import execute_view
from memseek.templates import (
    TemplateError,
    render_object,
    render_template,
    require_known_references,
)


class ArtifactNotFound(Exception):
    def __init__(self, detail: str) -> None:
        self.code = "artifact_not_found"
        self.detail = detail
        super().__init__(detail)


class ArtifactRequestError(ValueError):
    """An artifact request is invalid or exceeds a configured bound."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _BlockResolution:
    name: str
    scope: dict[str, Any]
    scope_hash: str
    max_seq: int
    ids: tuple[UUID, ...]
    tokens: int
    ready: bool
    omitted: bool
    truncated: bool
    definition_refs: tuple[dict[str, Any], ...]
    # The block's escaped rows, joined and otherwise unadorned.  Any element
    # around them belongs to the artifact template.
    rendered: str
    # The keyed active heads this block read, in selection order.  Only a
    # document block has them, and only they can identify the exact promoted
    # value a learning target must be based on.
    heads: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    """One complete deterministic rendering plus its provenance manifest."""

    artifact: ArtifactDefinition
    parameters: dict[str, Any]
    blocks: tuple[_BlockResolution, ...]
    rendered: str
    rendered_sha256: str
    input_ids: tuple[UUID, ...]
    tokens: int
    truncated: bool
    package_ref: dict[str, Any] | None
    started_at: datetime
    learning_target: dict[str, Any] | None = None


def _tokens(value: str) -> int:
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _scope_hash(scope: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(scope).encode("utf-8")).hexdigest()


def package_ref(catalog: DefinitionCatalog, kind: str, reference: str) -> dict[str, Any] | None:
    """The manifest reference of the package binding one shipped definition."""

    for package in catalog.packages.values():
        listed: tuple[str, ...] = getattr(package, kind, ())
        if reference in listed:
            return {
                "kind": "package",
                "name": package.name,
                "version": package.version,
                "hash": package.definition_hash,
            }
    return None


def artifact_catalog_payload(catalog: DefinitionCatalog) -> dict[str, Any]:
    """Build the `GET /artifacts` listing from loaded definitions."""

    artifacts = []
    for (name, version), artifact in sorted(catalog.artifacts.items()):
        artifacts.append(
            {
                "name": name,
                "version": version,
                "hash": artifact.definition_hash,
                "active": catalog.active_artifacts.get(name) == version,
                "kind": artifact.kind,
                "lifecycle": artifact.lifecycle,
                "parameters": {
                    parameter_name: {
                        "type": parameter.type,
                        "required": parameter.required,
                        "default": parameter.default,
                    }
                    for parameter_name, parameter in artifact.parameters.items()
                },
                "input_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    **parameters_json_schema(artifact.parameters),
                },
                "blocks": sorted(artifact.blocks),
                "snapshot": (
                    None
                    if artifact.snapshot is None
                    else {
                        "collection": artifact.snapshot.collection,
                        "type": artifact.snapshot.type,
                        "key": artifact.snapshot.key,
                    }
                ),
                "candidate_processor": artifact.candidate_processor,
                "complete_keys": list(artifact.complete_keys),
            }
        )
    return {"artifacts": artifacts}


def resolve_artifact_parameters(
    artifact: ArtifactDefinition, supplied: dict[str, Any]
) -> dict[str, Any]:
    """Validate supplied values against the artifact's typed parameter schema."""

    unknown = sorted(set(supplied) - set(artifact.parameters))
    if unknown:
        raise ArtifactRequestError(
            "artifact_parameter", f"unknown artifact parameter(s): {', '.join(unknown)}"
        )
    resolved: dict[str, Any] = {}
    for name, parameter in artifact.parameters.items():
        if name in supplied:
            value = supplied[name]
        elif parameter.default is not None:
            value = parameter.default
        elif parameter.required:
            raise ArtifactRequestError(
                "artifact_parameter", f"missing required artifact parameter {name!r}"
            )
        else:
            continue
        if not parameter_value_matches(parameter, value):
            raise ArtifactRequestError(
                "artifact_parameter",
                f"artifact parameter {name!r} does not match its declared schema",
            )
        resolved[name] = value
    return resolved


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


_ROW_COLUMNS = """
    id, seq, collection, key, type, content, scores, occurred_at, enriched_at, depth, run_id
"""


def _renderable(row: dict[str, Any]) -> RenderableRecord:
    return RenderableRecord(
        id=row["id"],
        occurred_at=row["occurred_at"],
        collection=row["collection"],
        type=row["type"],
        content=row["content"],
        key=row["key"],
        scores=row["scores"],
    )


async def _scope_max_seq(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str | None,
    collections: list[str],
    status: str | None,
) -> int:
    clauses = ["workspace = %s", "collection = any(%s)"]
    params: list[Any] = [workspace, collections]
    if entity is not None:
        clauses.append("entity = %s")
        params.append(entity)
    if status is not None and status != "all":
        clauses.append("status = %s")
        params.append(status)
    result = await conn.execute(
        cast(
            LiteralString,
            f"select coalesce(max(seq), 0) as max_seq from record where {' and '.join(clauses)}",
        ),
        params,
    )
    row = await result.fetchone()
    return int(row["max_seq"]) if row is not None else 0


async def _document_rows(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    collections: list[str],
    status: str,
) -> list[dict[str, Any]]:
    clauses = ["workspace = %s", "entity = %s", "collection = any(%s)", "key is not null"]
    params: list[Any] = [workspace, entity, collections]
    if status != "all":
        clauses.append("status = %s")
        params.append(status)
    result = await conn.execute(
        cast(
            LiteralString,
            f"""
        select distinct on (collection, key) {_ROW_COLUMNS}
        from record
        where {" and ".join(clauses)}
        order by collection, key, seq desc
        """,
        ),
        params,
    )
    rows = [dict(row) for row in await result.fetchall()]
    return [row for row in rows if row["content"].get("tombstone") is not True]


def _pack_block(
    rows: list[dict[str, Any]],
    *,
    catalog: DefinitionCatalog,
    max_tokens: int,
    input_cap_left: int,
    seen: set[UUID],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Take the deterministic prefix of rows that fits both bounds."""

    selected: list[dict[str, Any]] = []
    lines: list[str] = []
    truncated = False
    used_new = 0
    for row in rows:
        is_new = row["id"] not in seen
        if is_new and used_new >= input_cap_left:
            truncated = True
            break
        line = render_record(_renderable(row), profile="compact", catalog=catalog)
        candidate = [*lines, line]
        if _tokens(render_rows(candidate, fence=None)) > max_tokens:
            truncated = True
            break
        lines.append(line)
        selected.append(row)
        if is_new:
            used_new += 1
    return selected, lines, truncated


async def resolve_artifact_blocks(
    pool: DatabasePool,
    *,
    workspace: str,
    artifact: ArtifactDefinition,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> ArtifactResolution:
    """Resolve every block in declaration order and render the template."""

    started_at = datetime.now(UTC)
    template_params = {key: _json_safe(value) for key, value in parameters.items()}
    try:
        require_known_references(
            artifact.template, set(artifact.parameters) | set(artifact.blocks), context="artifact"
        )
    except TemplateError as exc:
        raise ArtifactRequestError("artifact_template", str(exc)) from exc

    seen: set[UUID] = set()
    input_ids: list[UUID] = []
    blocks: list[_BlockResolution] = []
    truncated = False
    for block_name, block in artifact.blocks.items():
        refs: list[dict[str, Any]] = []
        try:
            if block.document is not None:
                rendered_scope = render_object(
                    {
                        "entity": block.document.entity,
                        "collections": list(block.document.collections),
                        "status": block.document.status,
                    },
                    template_params,
                )
                entity = str(rendered_scope["entity"])
                collections = [str(item) for item in rendered_scope["collections"]]
                status = str(rendered_scope["status"])
                scope: dict[str, Any] = {
                    "source": "document",
                    "entity": entity,
                    "collections": sorted(collections),
                    "status": status,
                }
                for collection_name in collections:
                    collection = catalog.resolve_collection(collection_name)
                    refs.append(
                        {
                            "kind": "collection",
                            "name": collection.name,
                            "version": collection.version,
                            "hash": collection.definition_hash,
                        }
                    )
                async with pool.connection() as conn:
                    rows = await _document_rows(
                        conn,
                        workspace=workspace,
                        entity=entity,
                        collections=collections,
                        status=status,
                    )
                    max_seq = await _scope_max_seq(
                        conn,
                        workspace=workspace,
                        entity=entity,
                        collections=collections,
                        status=status,
                    )
            else:
                view = catalog.resolve_view(cast(str, block.view))
                rendered_args = render_object(dict(block.args), template_params)
                scope = {
                    "source": "view",
                    "view": f"{view.name}@{view.version}",
                    "args": _json_safe(rendered_args),
                }
                refs.append(
                    {
                        "kind": "view",
                        "name": view.name,
                        "version": view.version,
                        "hash": view.definition_hash,
                    }
                )
                result = await execute_view(
                    pool,
                    workspace=workspace,
                    name=cast(str, block.view),
                    parameters=rendered_args,
                    catalog=catalog,
                    settings=settings,
                )
                hit_ids = [UUID(hit["id"]) for hit in result["hits"]]
                rows = []
                view_entity = rendered_args.get("entity")
                from memseek.search.named_views import _view_collections

                view_collections = _view_collections(view, catalog)
                scope["collections"] = view_collections
                scope["entity"] = view_entity if isinstance(view_entity, str) else None
                scope["status"] = "active"
                async with pool.connection() as conn:
                    if hit_ids:
                        loaded = await conn.execute(
                            f"select {_ROW_COLUMNS} from record"
                            " where workspace = %s and id = any(%s::uuid[])",
                            (workspace, hit_ids),
                        )
                        by_id = {row["id"]: dict(row) for row in await loaded.fetchall()}
                        rows = [by_id[item] for item in hit_ids if item in by_id]
                    max_seq = await _scope_max_seq(
                        conn,
                        workspace=workspace,
                        entity=str(view_entity) if isinstance(view_entity, str) else None,
                        collections=view_collections,
                        status="active",
                    )
        except ArtifactRequestError:
            raise
        except Exception as exc:
            if block.required:
                raise ArtifactRequestError(
                    "artifact_block",
                    f"required block {block_name!r} failed: {type(exc).__name__}",
                ) from exc
            blocks.append(
                _BlockResolution(
                    name=block_name,
                    scope={"source": "failed"},
                    scope_hash=_scope_hash({"source": "failed"}),
                    max_seq=0,
                    ids=(),
                    tokens=0,
                    ready=True,
                    omitted=True,
                    truncated=False,
                    definition_refs=(),
                    rendered="",
                )
            )
            continue

        cap_left = settings.max_artifact_input_records - len(seen)
        selected, lines, block_truncated = _pack_block(
            rows,
            catalog=catalog,
            max_tokens=block.max_tokens,
            input_cap_left=cap_left,
            seen=seen,
        )
        if rows and not selected and block.required:
            raise ArtifactRequestError(
                "artifact_block",
                f"required block {block_name!r} cannot fit any record in max_tokens",
            )
        for row in selected:
            if row["id"] not in seen:
                seen.add(row["id"])
                input_ids.append(row["id"])
        rendered_block = render_rows(lines, fence=None)
        truncated = truncated or block_truncated
        blocks.append(
            _BlockResolution(
                name=block_name,
                scope=scope,
                scope_hash=_scope_hash(scope),
                max_seq=max_seq,
                ids=tuple(row["id"] for row in selected),
                tokens=_tokens(rendered_block),
                ready=all(row["enriched_at"] is not None for row in selected),
                omitted=False,
                truncated=block_truncated,
                definition_refs=tuple(refs),
                rendered=rendered_block,
                heads=(
                    ()
                    if block.document is None
                    else tuple(
                        {
                            "collection": row["collection"],
                            "key": row["key"],
                            "record_id": str(row["id"]),
                            "run_id": None if row["run_id"] is None else str(row["run_id"]),
                        }
                        for row in selected
                        if row["key"] is not None
                    )
                ),
            )
        )

    # Both parameter values and block rows are escaped, then substituted
    # literally.  Escaping is what keeps a value inside whatever element the
    # template wraps it in; choosing that element is the author's job.
    variables: dict[str, Any] = {
        key: escape_untrusted(str(value)) for key, value in template_params.items()
    }
    for block_resolution in blocks:
        variables[block_resolution.name] = block_resolution.rendered
    try:
        rendered = render_template(artifact.template, variables)
    except TemplateError as exc:
        raise ArtifactRequestError("artifact_template", str(exc)) from exc
    if _tokens(rendered) > settings.max_artifact_render_tokens:
        raise ArtifactRequestError(
            "artifact_too_large",
            "rendering exceeds MAX_ARTIFACT_RENDER_TOKENS; lower block budgets",
            status=409,
        )
    return ArtifactResolution(
        artifact=artifact,
        parameters=parameters,
        blocks=tuple(blocks),
        rendered=rendered,
        rendered_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        input_ids=tuple(input_ids),
        tokens=_tokens(rendered),
        truncated=truncated,
        package_ref=package_ref(catalog, "artifacts", f"{artifact.name}@{artifact.version}"),
        started_at=started_at,
        learning_target=_resolve_learning_target(artifact, tuple(blocks), catalog),
    )


def _resolve_learning_target(
    artifact: ArtifactDefinition,
    blocks: tuple[_BlockResolution, ...],
    catalog: DefinitionCatalog,
) -> dict[str, Any] | None:
    """Bind the declared learning target to the exact heads that were in force.

    The resolved target is what makes delayed feedback actionable: a candidate
    must replace the version that actually influenced the external execution,
    not whatever happens to be active when the feedback arrives.  A block that
    read no head resolves to no target rather than to an empty base, so a
    signal can never be attributed to a version that was never used.
    """

    learning = artifact.learning
    if learning is None:
        return None
    block = next((item for item in blocks if item.name == learning.target_block), None)
    if block is None or block.omitted or not block.heads:
        return None
    name, version = split_exact_reference(learning.artifact)
    target = catalog.artifacts[(name, int(version))]
    run_ids = {head["run_id"] for head in block.heads}
    entity = block.scope.get("entity")
    return {
        "artifact": {
            "name": target.name,
            "version": target.version,
            "definition_hash": target.definition_hash,
            "kind": target.kind,
        },
        "entity": entity if isinstance(entity, str) else None,
        "block": block.name,
        "heads": [dict(head) for head in block.heads],
        # One promotion writes every head of a complete reviewed value, so a
        # single shared run is the exact base version.  Mixed runs mean the
        # heads were not promoted together; there is no single base to name.
        "base_run_id": run_ids.pop() if len(run_ids) == 1 else None,
    }


def _block_manifest(resolution: ArtifactResolution) -> dict[str, Any]:
    return {
        block.name: {
            "scope": block.scope,
            "scope_hash": block.scope_hash,
            "max_seq": block.max_seq,
            "ids": [str(item) for item in block.ids],
            "tokens": block.tokens,
            "ready": block.ready,
            "omitted": block.omitted,
            "truncated": block.truncated,
            "definition_refs": list(block.definition_refs),
        }
        for block in resolution.blocks
    }


def render_manifest(resolution: ArtifactResolution) -> dict[str, Any]:
    """The provenance manifest shared by render and snapshot responses."""

    artifact = resolution.artifact
    return {
        "artifact": {
            "name": artifact.name,
            "version": artifact.version,
            "hash": artifact.definition_hash,
            "kind": artifact.kind,
            "lifecycle": artifact.lifecycle,
        },
        "package": resolution.package_ref,
        "parameters": _json_safe(resolution.parameters),
        "blocks": _block_manifest(resolution),
        "input_record_ids": [str(item) for item in resolution.input_ids],
        "tokens": resolution.tokens,
        "truncated": resolution.truncated,
        "rendered_sha256": resolution.rendered_sha256,
    }


async def render_artifact(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Deterministically render one artifact and return text plus manifest."""

    resolution = await resolve_named_artifact(
        pool,
        workspace=workspace,
        name=name,
        parameters=parameters,
        catalog=catalog,
        settings=settings,
    )
    return {"rendered": resolution.rendered, "manifest": render_manifest(resolution)}


def _snapshot_entity(artifact: ArtifactDefinition, parameters: dict[str, Any]) -> str:
    snapshot = artifact.snapshot
    if snapshot is None:
        raise ArtifactRequestError(
            "artifact_snapshot", f"artifact {artifact.name!r} declares no snapshot target"
        )
    if snapshot.entity is not None:
        try:
            entity = render_template(
                snapshot.entity, {key: _json_safe(value) for key, value in parameters.items()}
            )
        except TemplateError as exc:
            raise ArtifactRequestError("artifact_parameter", str(exc)) from exc
    else:
        entity = str(parameters.get("entity", ""))
    if not entity or entity == "*":
        raise ArtifactRequestError(
            "artifact_snapshot", "snapshot target entity is empty; supply entity"
        )
    return entity


def _materialize_run_content(
    resolution: ArtifactResolution,
    *,
    run_id: UUID,
    output_id: UUID,
    settings: Settings,
) -> dict[str, Any]:
    completed = datetime.now(UTC)
    artifact = resolution.artifact
    definition_refs: list[dict[str, Any]] = [
        {
            "kind": "artifact",
            "name": artifact.name,
            "version": artifact.version,
            "hash": artifact.definition_hash,
        }
    ]
    for block in resolution.blocks:
        definition_refs.extend(block.definition_refs)
    if resolution.package_ref is not None:
        definition_refs.append(resolution.package_ref)
    return {
        "text": f"materialize {artifact.name} ok",
        "schema_version": 1,
        "engine_version": f"{__version__}+{settings.memseek_build_sha}",
        "operation": "materialize",
        "artifact": artifact.name,
        "status": "ok",
        "run_id": str(run_id),
        "parameters": _json_safe(resolution.parameters),
        "blocks": _block_manifest(resolution),
        "input_ids": [str(item) for item in resolution.input_ids],
        "output_ids": [str(output_id)],
        "rendered_sha256": resolution.rendered_sha256,
        "definition_refs": definition_refs,
        "started_at": resolution.started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "ms": max(0, int((completed - resolution.started_at).total_seconds() * 1_000)),
        "error_kind": None,
        "error": None,
    }


async def persist_artifact_snapshot(
    pool: DatabasePool,
    *,
    workspace: str,
    resolution: ArtifactResolution,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Persist one already-computed rendering as a provenance-carrying record.

    Callers that need both a render and its snapshot pass the same resolution
    to both, so the persisted record and any correlation handle name one
    identical rendered-content hash.
    """

    artifact = resolution.artifact
    snapshot = artifact.snapshot
    entity = _snapshot_entity(artifact, resolution.parameters)
    assert snapshot is not None  # _snapshot_entity rejects a missing target.
    collection = catalog.resolve_collection(snapshot.collection)
    content: dict[str, Any] = {
        "text": resolution.rendered,
        "artifact_name": artifact.name,
        "artifact_version": artifact.version,
        "artifact_hash": artifact.definition_hash,
        "parameters": _json_safe(resolution.parameters),
        "blocks": _block_manifest(resolution),
        "rendered_sha256": resolution.rendered_sha256,
    }
    validator = Draft202012Validator(collection.content_schema, format_checker=FormatChecker())
    error = next(iter(validator.iter_errors(content)), None)
    if error is not None:
        raise ArtifactRequestError(
            "artifact_snapshot", f"snapshot content violates {collection.name} schema"
        )

    run_id = uuid4()
    output_id = uuid4()
    status = "active" if artifact.lifecycle == "live" else "draft"
    ready = not collection.required_processors
    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        await acquire_entity_locks(conn, workspace, (entity,))
        depth_result = await conn.execute(
            "select id, depth from record where workspace = %s and id = any(%s::uuid[]) for share",
            (workspace, list(resolution.input_ids)),
        )
        depths = {row["id"]: int(row["depth"]) for row in await depth_result.fetchall()}
        if set(resolution.input_ids) != set(depths):
            raise ArtifactRequestError(
                "artifact_stale", "an input record disappeared before commit", status=409
            )
        run_depth = max(depths.values(), default=0)
        from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION

        await insert_canonical_record_tx(
            conn,
            CanonicalRecordWrite(
                id=run_id,
                workspace=workspace,
                collection="_system",
                collection_version=SYSTEM_COLLECTION_VERSION,
                collection_hash=SYSTEM_COLLECTION_HASH,
                entity=entity,
                type="run",
                content=_materialize_run_content(
                    resolution, run_id=run_id, output_id=output_id, settings=settings
                ),
                ready=True,
                depth=run_depth,
                derived_from=tuple(resolution.input_ids),
            ),
            settings,
        )
        snapshot_insert = await insert_canonical_record_tx(
            conn,
            CanonicalRecordWrite(
                id=output_id,
                workspace=workspace,
                collection=collection.name,
                collection_version=collection.version,
                collection_hash=collection.contract_hash,
                entity=entity,
                key=snapshot.key,
                type=snapshot.type,
                status=status,
                content=content,
                ready=ready,
                run_id=run_id,
                # A materialization is a deterministic recipe; it does not add
                # an abstraction level beyond its rendered inputs.
                depth=run_depth,
                derived_from=(run_id,),
            ),
            settings,
        )
        from memseek.projections import on_records_ready_tx

        ready_records: list[dict[str, Any]] = [{"id": run_id}]
        if ready:
            ready_records.append({"id": output_id})
        await on_records_ready_tx(conn, workspace=workspace, records=ready_records, catalog=catalog)
    assert snapshot_insert is not None
    return {
        "run_id": str(run_id),
        "record_id": str(output_id),
        "seq": snapshot_insert.seq,
        "entity": entity,
        "collection": collection.name,
        "key": snapshot.key,
        "status": status,
        "ready": snapshot_insert.ready,
        "rendered_sha256": resolution.rendered_sha256,
        "manifest": render_manifest(resolution),
    }


async def snapshot_artifact(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Render one artifact and persist that exact rendering."""

    resolution = await resolve_named_artifact(
        pool,
        workspace=workspace,
        name=name,
        parameters=parameters,
        catalog=catalog,
        settings=settings,
        require_snapshot=True,
    )
    return await persist_artifact_snapshot(
        pool,
        workspace=workspace,
        resolution=resolution,
        catalog=catalog,
        settings=settings,
    )


async def resolve_named_artifact(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
    require_snapshot: bool = False,
) -> ArtifactResolution:
    """Resolve the active definition of one named artifact and render it."""

    try:
        artifact = catalog.resolve_artifact(name)
    except (KeyError, ValueError) as exc:
        raise ArtifactNotFound(f"unknown artifact {name!r}") from exc
    resolved = resolve_artifact_parameters(artifact, parameters)
    if require_snapshot:
        # Reject an impossible snapshot request before rendering rather than
        # after paying for the reads.
        _snapshot_entity(artifact, resolved)
    return await resolve_artifact_blocks(
        pool,
        workspace=workspace,
        artifact=artifact,
        parameters=resolved,
        catalog=catalog,
        settings=settings,
    )


async def _stale_reasons(
    pool: DatabasePool,
    *,
    workspace: str,
    blocks: dict[str, Any],
    catalog: DefinitionCatalog,
) -> list[str]:
    reasons: set[str] = set()
    for block in blocks.values():
        for ref in block.get("definition_refs", ()):
            kind, ref_name, version = ref.get("kind"), ref.get("name"), ref.get("version")
            current_hash: str | None = None
            try:
                if kind == "collection":
                    current_hash = catalog.resolve_collection(ref_name, version).definition_hash
                elif kind == "view":
                    current_hash = catalog.resolve_view(ref_name, version).definition_hash
            except KeyError, ValueError:
                current_hash = None
            if current_hash is not None and current_hash != ref.get("hash"):
                reasons.add("definition_changed")
        scope = block.get("scope", {})
        collections = scope.get("collections")
        if not isinstance(collections, list) or not collections:
            continue
        recorded = block.get("max_seq")
        if not isinstance(recorded, int):
            continue
        async with pool.connection() as conn:
            entity = scope.get("entity")
            current_max = await _scope_max_seq(
                conn,
                workspace=workspace,
                entity=entity if isinstance(entity, str) else None,
                collections=[str(item) for item in collections],
                status=scope.get("status") if isinstance(scope.get("status"), str) else None,
            )
        if current_max > recorded:
            reasons.add("new_records")
    return sorted(reasons)


async def read_artifact_snapshot(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Read the current active snapshot and its conservative staleness."""

    try:
        artifact = catalog.resolve_artifact(name)
    except (KeyError, ValueError) as exc:
        raise ArtifactNotFound(f"unknown artifact {name!r}") from exc
    resolved = resolve_artifact_parameters(artifact, parameters)
    entity = _snapshot_entity(artifact, resolved)
    snapshot = artifact.snapshot
    assert snapshot is not None  # _snapshot_entity rejects a missing target.
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select distinct on (collection, key)
                   id, seq, content, run_id, enriched_at, occurred_at, created_at
            from record
            where workspace = %s and entity = %s and collection = %s
              and key = %s and status = 'active'
            order by collection, key, seq desc
            """,
            (workspace, entity, snapshot.collection, snapshot.key),
        )
        row = await result.fetchone()
    payload: dict[str, Any] = {
        "artifact": {
            "name": artifact.name,
            "version": artifact.version,
            "hash": artifact.definition_hash,
            "lifecycle": artifact.lifecycle,
        },
        "entity": entity,
        "snapshot": None,
        "stale": None,
        "stale_reasons": [],
    }
    if row is None or row["content"].get("tombstone") is True:
        return payload
    content = row["content"]
    blocks = content.get("blocks") if isinstance(content.get("blocks"), dict) else {}
    reasons = await _stale_reasons(pool, workspace=workspace, blocks=blocks, catalog=catalog)
    if content.get("artifact_hash") not in (None, artifact.definition_hash):
        reasons = sorted({*reasons, "definition_changed"})
    payload["snapshot"] = {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "run_id": str(row["run_id"]) if row["run_id"] is not None else None,
        "ready": row["enriched_at"] is not None,
        "content": content,
        "occurred_at": row["occurred_at"].isoformat(),
        "created_at": row["created_at"].isoformat(),
    }
    payload["stale"] = bool(reasons)
    payload["stale_reasons"] = reasons
    return payload


__all__ = [
    "ArtifactNotFound",
    "ArtifactRequestError",
    "ArtifactResolution",
    "artifact_catalog_payload",
    "package_ref",
    "persist_artifact_snapshot",
    "read_artifact_snapshot",
    "render_artifact",
    "render_manifest",
    "resolve_artifact_blocks",
    "resolve_artifact_parameters",
    "resolve_named_artifact",
    "snapshot_artifact",
]
