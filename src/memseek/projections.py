"""Transactional readiness hooks and durable search-projection jobs.

PostgreSQL remains canonical.  Ready transitions only enqueue durable work;
projection workers reload the rows and recompute keyed current state immediately
before calling a backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions.errors import CollectionDefinitionMismatch
from memseek.jobs import complete_job
from memseek.models import ClaimedJob, LeaseLost
from memseek.search.registry import SearchBackend

if TYPE_CHECKING:
    from memseek.definitions import DefinitionCatalog


class ProjectionInvariantError(RuntimeError):
    """Raised when a readiness hook is called for non-canonical state."""


class ProjectionPayloadError(ValueError):
    """Raised when a projection job does not contain a valid durable payload."""


class ProjectionBackendUnavailable(RuntimeError):
    """Raised when no runtime adapter exists for a selected search profile."""


class RecordReference(Protocol):
    """Structural input accepted from the shared record-insertion helper."""

    id: UUID
    collection: str


@dataclass(frozen=True, slots=True)
class ReadyRecord:
    """Canonical identity passed from enrichment or insertion finalization."""

    id: UUID
    collection: str
    entity: str
    key: str | None
    status: str


@dataclass(frozen=True, slots=True)
class ProjectionTarget:
    """The durable minimum needed to refetch or delete an indexed row."""

    id: UUID
    collection: str


@dataclass(frozen=True, slots=True)
class _InputRecord:
    id: UUID
    collection: str | None
    entity: str | None
    key: str | None
    status: str | None
    has_identity: bool


type RecordInput = ReadyRecord | RecordReference | Mapping[str, Any]
type BackendRegistry = Mapping[str, SearchBackend]


def _field(value: RecordInput, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _has_field(value: RecordInput, name: str) -> bool:
    if isinstance(value, Mapping):
        return name in value
    return hasattr(value, name)


def _normalize_inputs(
    records: Sequence[RecordInput], *, require_collection: bool = False
) -> tuple[_InputRecord, ...]:
    normalized: dict[UUID, _InputRecord] = {}
    for value in records:
        raw_id = _field(value, "id")
        raw_collection = _field(value, "collection")
        try:
            record_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectionInvariantError(
                "projection input requires a canonical record UUID"
            ) from exc
        if require_collection and (not isinstance(raw_collection, str) or not raw_collection):
            raise ProjectionInvariantError("projection input requires a collection")
        if raw_collection is not None and (
            not isinstance(raw_collection, str) or not raw_collection
        ):
            raise ProjectionInvariantError("projection input collection is invalid")
        has_identity = all(_has_field(value, name) for name in ("entity", "key", "status"))
        entity = _field(value, "entity") if has_identity else None
        key = _field(value, "key") if has_identity else None
        status = _field(value, "status") if has_identity else None
        if has_identity and (not isinstance(entity, str) or not isinstance(status, str)):
            raise ProjectionInvariantError("projection identity requires entity and status")
        item = _InputRecord(
            id=record_id,
            collection=cast(str | None, raw_collection),
            entity=cast(str | None, entity),
            key=cast(str | None, key),
            status=cast(str | None, status),
            has_identity=has_identity,
        )
        previous = normalized.get(record_id)
        if (
            previous is not None
            and previous.collection is not None
            and item.collection is not None
            and previous.collection != item.collection
        ):
            raise ProjectionInvariantError(f"conflicting collections for record {record_id}")
        normalized[record_id] = item
    return tuple(normalized[record_id] for record_id in sorted(normalized, key=str))


async def _load_identity_rows_tx(
    conn: AsyncConnection[Any], workspace: str, inputs: Sequence[_InputRecord]
) -> dict[UUID, dict[str, Any]]:
    if not inputs:
        return {}
    result = await conn.execute(
        """
        select id, collection, collection_version, collection_hash,
               entity, key, status, enriched_at
        from record
        where workspace = %s and id = any(%s::uuid[])
        """,
        (workspace, [item.id for item in inputs]),
    )
    rows = await result.fetchall()
    return {cast(UUID, row["id"]): row for row in rows}


def _ready_records_from_canonical(
    inputs: Sequence[_InputRecord], canonical: Mapping[UUID, Mapping[str, Any]]
) -> tuple[ReadyRecord, ...]:
    records: list[ReadyRecord] = []
    for item in inputs:
        row = canonical.get(item.id)
        if row is None:
            raise ProjectionInvariantError(
                f"record {item.id} is not owned by the requested workspace"
            )
        if item.collection is not None and row["collection"] != item.collection:
            raise ProjectionInvariantError(f"collection mismatch for record {item.id}")
        records.append(
            ReadyRecord(
                id=item.id,
                collection=str(row["collection"]),
                entity=str(row["entity"]),
                key=cast(str | None, row["key"]),
                status=str(row["status"]),
            )
        )
    return tuple(records)


def _resolve_stored_collection(row: Mapping[str, Any], catalog: DefinitionCatalog) -> None:
    if row["collection"] == "_system":
        return
    try:
        catalog.resolve_stored_collection(
            str(row["collection"]),
            int(row["collection_version"]),
            str(row["collection_hash"]),
        )
    except CollectionDefinitionMismatch as exc:
        raise ProjectionInvariantError(str(exc)) from exc


async def _refresh_targets_tx(
    conn: AsyncConnection[Any], workspace: str, identities: Sequence[ReadyRecord]
) -> tuple[ProjectionTarget, ...]:
    keyed = [
        {
            "source_id": str(item.id),
            "entity": item.entity,
            "collection": item.collection,
            "key": item.key,
            "status": item.status,
        }
        for item in identities
        if item.key is not None
    ]
    if not keyed:
        return ()
    result = await conn.execute(
        """
        with supplied as (
          select source_id, entity, collection, key, status
          from jsonb_to_recordset(%s::jsonb)
            as item(
              source_id uuid, entity text, collection text, key text, status text
            )
        ), identities as (
          select distinct entity, collection, key, status from supplied
        ), current_ready as (
          select distinct on (row.entity, row.collection, row.key, row.status)
                 row.id, row.collection
          from identities identity
          join record row
            on row.workspace = %s
           and row.entity = identity.entity
           and row.collection = identity.collection
           and row.key = identity.key
           and row.status = identity.status
          where row.enriched_at is not null
          order by row.entity, row.collection, row.key, row.status, row.seq desc
        ), previous_ready as (
          select distinct on (row.entity, row.collection, row.key, row.status)
                 row.id, row.collection
          from identities identity
          join record row
            on row.workspace = %s
           and row.entity = identity.entity
           and row.collection = identity.collection
           and row.key = identity.key
           and row.status = identity.status
          where row.enriched_at is not null
            and not exists (
              select 1
              from supplied source
              where source.entity = row.entity
                and source.collection = row.collection
                and source.key = row.key
                and source.status = row.status
                and source.source_id = row.id
            )
          order by row.entity, row.collection, row.key, row.status, row.seq desc
        )
        select distinct id, collection
        from (
          select * from current_ready
          union all
          select * from previous_ready
        ) changed
        order by id
        """,
        (Jsonb(keyed), workspace, workspace),
    )
    rows = await result.fetchall()
    return tuple(
        ProjectionTarget(id=cast(UUID, row["id"]), collection=str(row["collection"]))
        for row in rows
    )


def _merge_targets(*groups: Sequence[ProjectionTarget]) -> tuple[ProjectionTarget, ...]:
    targets: dict[UUID, ProjectionTarget] = {}
    for group in groups:
        for target in group:
            previous = targets.get(target.id)
            if previous is not None and previous.collection != target.collection:
                raise ProjectionInvariantError(f"conflicting collections for record {target.id}")
            targets[target.id] = target
    return tuple(targets[record_id] for record_id in sorted(targets, key=str))


async def _enqueue_projection_tx(
    conn: AsyncConnection[Any],
    *,
    workspace: str,
    kind: str,
    targets: Sequence[ProjectionTarget],
) -> UUID | None:
    if not targets:
        return None
    payload = {
        "records": [{"id": str(target.id), "collection": target.collection} for target in targets]
    }
    result = await conn.execute(
        """
        insert into job (workspace, kind, payload, dedupe_key)
        values (%s, %s, %s, null)
        returning id
        """,
        (workspace, kind, Jsonb(payload)),
    )
    row = await result.fetchone()
    if row is None:
        raise ProjectionInvariantError("projection job insert returned no id")
    return cast(UUID, row["id"])


async def refresh_current_projection_tx(
    conn: AsyncConnection[Any],
    *,
    workspace: str,
    records: Sequence[RecordInput],
) -> None:
    """Enqueue the ready keyed row whose canonical ``is_current`` may have changed.

    Call this after keyed insertion, or with full :class:`ReadyRecord` identities
    after erasure.  The current ready candidate and the latest ready row that
    predates the supplied records are refreshed; the worker recomputes state
    against *all* canonical rows, including a newer unready replacement.
    """

    inputs = _normalize_inputs(records)
    canonical = await _load_identity_rows_tx(conn, workspace, inputs)
    identities: list[ReadyRecord] = []
    for item in inputs:
        row = canonical.get(item.id)
        if row is not None:
            identities.extend(_ready_records_from_canonical((item,), canonical))
        elif item.has_identity and item.collection is not None:
            identities.append(
                ReadyRecord(
                    id=item.id,
                    collection=item.collection,
                    entity=cast(str, item.entity),
                    key=item.key,
                    status=cast(str, item.status),
                )
            )
        else:
            raise ProjectionInvariantError(
                f"record {item.id} is missing and no keyed identity was supplied"
            )
    targets = await _refresh_targets_tx(conn, workspace, identities)
    await _enqueue_projection_tx(conn, workspace=workspace, kind="index_upsert", targets=targets)


async def evaluate_ready_triggers_tx(
    conn: AsyncConnection[Any],
    *,
    workspace: str,
    records: Sequence[ReadyRecord],
    catalog: DefinitionCatalog | None,
) -> None:
    """Evaluate write/accumulator triggers after canonical readiness.

    The import stays local because the trigger evaluator reuses derivation
    schema types and this module is itself used by derive output commits.
    """

    if catalog is None or not records:
        return
    from memseek.triggers import evaluate_ready_triggers_tx as evaluate_triggers

    await evaluate_triggers(
        conn,
        workspace=workspace,
        entities=tuple(record.entity for record in records),
        catalog=catalog,
    )


async def on_records_ready_tx(
    conn: AsyncConnection[Any],
    *,
    workspace: str,
    records: Sequence[RecordInput],
    catalog: DefinitionCatalog | None = None,
) -> None:
    """Atomically enqueue projections and reach the trigger barrier for newly ready rows."""

    inputs = _normalize_inputs(records)
    canonical = await _load_identity_rows_tx(conn, workspace, inputs)
    if catalog is not None:
        for row in canonical.values():
            _resolve_stored_collection(row, catalog)
    ready = _ready_records_from_canonical(inputs, canonical)
    unready = [str(item.id) for item in inputs if canonical[item.id]["enriched_at"] is None]
    if unready:
        raise ProjectionInvariantError(
            f"ready hook cannot run before enrichment: {', '.join(unready)}"
        )
    direct = tuple(ProjectionTarget(item.id, item.collection) for item in ready)
    refreshed = await _refresh_targets_tx(conn, workspace, ready)
    await _enqueue_projection_tx(
        conn,
        workspace=workspace,
        kind="index_upsert",
        targets=_merge_targets(direct, refreshed),
    )
    # This call is deliberately after the readiness check, keeping one
    # integration point for the M5 trigger milestone without an ingest-only
    # trigger path.
    await evaluate_ready_triggers_tx(conn, workspace=workspace, records=ready, catalog=catalog)


def _parse_targets(payload: Mapping[str, Any]) -> tuple[ProjectionTarget, ...]:
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ProjectionPayloadError("projection payload requires a non-empty records list")
    values: list[Mapping[str, Any]] = []
    for item in raw_records:
        if not isinstance(item, Mapping):
            raise ProjectionPayloadError("projection payload records must be objects")
        values.append(cast(Mapping[str, Any], item))
    try:
        normalized = _normalize_inputs(values, require_collection=True)
    except ProjectionInvariantError as exc:
        raise ProjectionPayloadError(str(exc)) from exc
    return tuple(ProjectionTarget(item.id, cast(str, item.collection)) for item in normalized)


async def _assert_live_claim(pool: DatabasePool, claimed: ClaimedJob) -> None:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select exists (
              select 1 from job
              where id = %s
                and locked_by = %s
                and done_at is null
                and dead_at is null
                and lease_until > clock_timestamp()
            ) as owned
            """,
            (claimed.id, claimed.claim_token),
        )
        row = await result.fetchone()
    if not row or not row["owned"]:
        raise LeaseLost(f"job lease lost: {claimed.id}")


def _vector(value: Any, dimension: int) -> tuple[list[float], bool]:
    if value is None:
        return [1.0, *([0.0] * (dimension - 1))], False
    if isinstance(value, str):
        stripped = value.strip().removeprefix("[").removesuffix("]")
        vector = [float(part) for part in stripped.split(",") if part]
    elif isinstance(value, Sequence):
        vector = [float(part) for part in value]
    else:
        raise ProjectionInvariantError("canonical embedding has an unsupported representation")
    if len(vector) != dimension:
        raise ProjectionInvariantError(
            f"canonical embedding has dimension {len(vector)}, expected {dimension}"
        )
    return vector, True


def _dotted_value(row: Mapping[str, Any], path: str) -> Any:
    root, *parts = path.split(".")
    value: Any = row.get(root)
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def projected_attribute_name(collection: str, version: int, field: str) -> str:
    """Return the stable private attribute used for a projected collection field."""

    identity = f"{collection}-{version}-{field}".encode()
    return f"f_{hashlib.sha256(identity).hexdigest()[:16]}"


def _timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ProjectionInvariantError("canonical projection timestamp is invalid")
    return value.isoformat()


def _project_row(row: Mapping[str, Any], catalog: DefinitionCatalog) -> dict[str, Any]:
    _resolve_stored_collection(row, catalog)
    vector, has_embedding = _vector(row["embedding_text"], catalog.models.embedding.dimensions)
    content = cast(Mapping[str, Any], row["content"])
    projected: dict[str, Any] = {
        "id": str(row["id"]),
        "workspace": str(row["workspace"]),
        "vector": vector,
        "text": str(content["text"]),
        "has_embedding": has_embedding,
        "embedding_space": str(row["embedding_space"] or ""),
        "collection": str(row["collection"]),
        "collection_version": int(row["collection_version"]),
        "collection_hash": str(row["collection_hash"]),
        "entity": str(row["entity"]),
        "type": str(row["type"]),
        "status": str(row["status"]),
        "keyed": row["key"] is not None,
        "is_current": bool(row["is_current"]),
        "tombstone": bool(content.get("tombstone", False)),
        "depth": int(row["depth"]),
        "seq": int(row["seq"]),
        "occurred_at": _timestamp(row["occurred_at"]),
        "created_at": _timestamp(row["created_at"]),
    }
    if row["collection"] != "_system":
        collection = catalog.resolve_stored_collection(
            str(row["collection"]),
            int(row["collection_version"]),
            str(row["collection_hash"]),
        )
        for name, declaration in collection.fields.items():
            if declaration.project:
                # Prefer the newest annotation a supersession chain offers, so an
                # external index matches what canonical reads return.
                value = next(
                    (
                        candidate
                        for candidate in (
                            _dotted_value(row, dotted)
                            for dotted in (declaration.path, *declaration.fallback_paths)
                        )
                        if candidate is not None
                    ),
                    None,
                )
                projected[projected_attribute_name(collection.name, collection.version, name)] = (
                    value
                )
    return projected


async def _load_projection_rows(
    pool: DatabasePool,
    *,
    workspace: str,
    targets: Sequence[ProjectionTarget],
    catalog: DefinitionCatalog,
) -> dict[UUID, dict[str, Any]]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            select row.id, row.workspace, row.seq, row.collection, row.collection_version,
                   row.collection_hash,
                   row.entity, row.key, row.type, row.status, row.content,
                   row.embedding::text as embedding_text, row.embedding_space,
                   row.annotations, row.depth, row.occurred_at, row.created_at,
                   case
                     when row.key is null then true
                     else not exists (
                       select 1
                       from record newer
                       where newer.workspace = row.workspace
                         and newer.entity = row.entity
                         and newer.collection = row.collection
                         and newer.key = row.key
                         and newer.status = row.status
                         and newer.seq > row.seq
                     )
                   end as is_current
            from record row
            where row.workspace = %s
              and row.id = any(%s::uuid[])
              and row.enriched_at is not null
            """,
            (workspace, [target.id for target in targets]),
        )
        rows = await result.fetchall()
    return {cast(UUID, row["id"]): _project_row(row, catalog) for row in rows}


def _profile_for_collection(
    settings: Settings, catalog: DefinitionCatalog, collection: str
) -> tuple[str, str]:
    profile_name = catalog.deployment_bindings.get(collection)
    if profile_name is not None:
        profile = catalog.resolve_search_profile(profile_name)
        return profile_name, profile.backend
    # System records have no public collection definition.  Their projection
    # still uses the deployment's private backend and remains canonically
    # filtered by collection/type.
    if collection == "_system":
        return "_system", settings.search_backend
    raise ProjectionInvariantError(f"collection {collection!r} has no search profile binding")


def _backend_for_profile(
    *,
    profile_name: str,
    backend_name: str,
    backends: BackendRegistry | None,
) -> SearchBackend:
    if backends is not None:
        backend = backends.get(profile_name) or backends.get(backend_name)
        if backend is not None:
            return backend
    if backend_name == "pg":
        from memseek.search.pg import PostgresSearchBackend

        return PostgresSearchBackend()
    if backend_name == "turbopuffer":
        from memseek.search.turbopuffer import TurbopufferSearchBackend

        return TurbopufferSearchBackend()
    raise ProjectionBackendUnavailable(
        f"search profile {profile_name!r} requires unavailable backend {backend_name!r}"
    )


async def execute_projection_job(
    pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    claimed: ClaimedJob,
    *,
    backends: BackendRegistry | None = None,
) -> None:
    """Refetch canonical truth and perform one live claimed projection job.

    Backend failures intentionally propagate so the normal durable retry policy
    can release or dead-letter the still-claimed job.
    """

    if claimed.kind not in {"index_upsert", "index_delete"}:
        raise ProjectionPayloadError(f"unsupported projection job kind {claimed.kind!r}")
    targets = _parse_targets(claimed.payload)
    await _assert_live_claim(pool, claimed)
    rows = (
        await _load_projection_rows(
            pool,
            workspace=claimed.workspace,
            targets=targets,
            catalog=catalog,
        )
        if claimed.kind == "index_upsert"
        else {}
    )

    grouped_upserts: dict[str, list[dict[str, Any]]] = {}
    grouped_deletes: dict[str, list[dict[str, Any]]] = {}
    profile_backends: dict[str, str] = {}
    for target in targets:
        profile_name, backend_name = _profile_for_collection(settings, catalog, target.collection)
        profile_backends[profile_name] = backend_name
        row = rows.get(target.id)
        if row is not None:
            grouped_upserts.setdefault(profile_name, []).append(row)
        else:
            grouped_deletes.setdefault(profile_name, []).append(
                {"id": str(target.id), "collection": target.collection}
            )

    for profile_name in sorted(profile_backends):
        backend = _backend_for_profile(
            profile_name=profile_name,
            backend_name=profile_backends[profile_name],
            backends=backends,
        )
        try:
            upserts = grouped_upserts.get(profile_name)
            if upserts:
                await backend.upsert(settings, upserts)
            deletes = grouped_deletes.get(profile_name)
            if deletes:
                await backend.delete(settings, claimed.workspace, deletes)
        finally:
            if backends is None:
                close = getattr(backend, "aclose", None)
                if close is not None:
                    await close()


async def handle_projection_job(
    pool: DatabasePool,
    settings: Settings,
    catalog: DefinitionCatalog,
    claimed: ClaimedJob,
    *,
    backends: BackendRegistry | None = None,
) -> None:
    """Execute and claim-token-fence completion of one projection job."""

    await execute_projection_job(pool, settings, catalog, claimed, backends=backends)
    await complete_job(pool, claimed)
