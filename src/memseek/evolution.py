"""Operator-facing definition-evolution operations.

Three operations that a workspace needs in order to keep changing its
definitions over a long life:

``migrate_collection_hashes``
    Move stored records onto the narrower record-contract identity.  Needed once
    per workspace written before the contract split; idempotent and resumable
    afterwards.
``prune_definitions``
    Report which definitions nothing references any more, so a catalog can shrink
    with proof rather than hope.
``rebind_cursor``
    Repoint a ``changes`` derivation's cursor after a deliberate source-scope
    change, instead of forcing the pipeline to be renamed.

Every one of them works through ordinary canonical transactions under the
workspace advisory lock, and none of them mutates record content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from memseek.canonical_records import CanonicalRecordWrite, insert_canonical_record_tx
from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.definitions.compat import StoredGroup, plan_stored_groups
from memseek.derive.basis import _watermark, source_contract_hash
from memseek.locks import acquire_entity_locks, acquire_workspace_lock

_REWRITE_BATCH = 5_000


class EvolutionError(ValueError):
    """An invalid or unsafe evolution request."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ContractMigrationResult:
    workspace: str
    rewritten: int
    groups: tuple[dict[str, Any], ...]
    unresolved: tuple[dict[str, Any], ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "rewritten": self.rewritten,
            "groups": [dict(item) for item in self.groups],
            "unresolved": [dict(item) for item in self.unresolved],
            "complete": not self.unresolved,
        }


async def _stored_groups(conn: DatabaseConnection, workspace: str) -> tuple[StoredGroup, ...]:
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


async def migrate_collection_hashes(
    pool: DatabasePool,
    *,
    workspace: str,
    catalog: DefinitionCatalog,
    dry_run: bool = False,
) -> ContractMigrationResult:
    """Rewrite stored collection hashes onto the current record-contract identity.

    Deterministic because the loader can compute both identities from the same
    definition: a stored value that equals a definition's whole-definition hash
    predates the split and is rewritten to that definition's contract hash.  A
    stored value that already equals a contract hash is skipped, which makes
    repeated runs no-ops.  Anything else is reported and left untouched — a
    workspace that drifted for another reason must be fixed before it can be
    migrated.
    """

    async with pool.connection() as conn:
        groups = await _stored_groups(conn, workspace)
    rewrites, blockers = plan_stored_groups(groups, previous=catalog, incoming=catalog)
    planned = tuple(item for item in rewrites if item.reason == "generation_upgrade")
    # A blocker here is never an additive change (previous and incoming are the
    # same catalog), so it always means genuine drift.
    unresolved = tuple(item.as_json() for item in blockers)
    if dry_run:
        return ContractMigrationResult(
            workspace=workspace,
            rewritten=0,
            groups=tuple(item.as_json() for item in planned),
            unresolved=unresolved,
        )

    rewritten = 0
    for rewrite in planned:
        while True:
            async with pool.connection() as conn, conn.transaction():
                await acquire_workspace_lock(conn, workspace)
                result = await conn.execute(
                    """
                    update record
                    set collection_hash = %s
                    where id in (
                      select id from record
                      where workspace = %s and collection = %s and collection_version = %s
                        and collection_hash = %s
                      order by seq
                      limit %s
                    )
                    """,
                    (
                        rewrite.target_hash,
                        workspace,
                        rewrite.collection,
                        rewrite.version,
                        rewrite.stored_hash,
                        _REWRITE_BATCH,
                    ),
                )
                affected = int(result.rowcount or 0)
            rewritten += affected
            if affected < _REWRITE_BATCH:
                break
    return ContractMigrationResult(
        workspace=workspace,
        rewritten=rewritten,
        groups=tuple(item.as_json() for item in planned),
        unresolved=unresolved,
    )


async def workspaces(pool: DatabasePool) -> tuple[str, ...]:
    """Every workspace id, for operations that sweep a whole deployment."""

    async with pool.connection() as conn:
        result = await conn.execute("select id from workspace order by id")
        return tuple(str(row["id"]) for row in await result.fetchall())


@dataclass(frozen=True, slots=True)
class PruneCandidate:
    """One definition and the durable references that would outlive deleting it."""

    family: str
    name: str
    version: int | None
    references: int
    reference_kind: str
    safe_to_delete: bool
    detail: str

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "family": self.family,
            "name": self.name,
            "references": self.references,
            "reference_kind": self.reference_kind,
            "safe_to_delete": self.safe_to_delete,
            "detail": self.detail,
        }
        if self.version is not None:
            payload["version"] = self.version
        return payload


@dataclass(frozen=True, slots=True)
class PruneReport:
    workspace: str
    candidates: tuple[PruneCandidate, ...] = field(default=())

    @property
    def deletable(self) -> tuple[PruneCandidate, ...]:
        return tuple(item for item in self.candidates if item.safe_to_delete)

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "candidates": [item.as_json() for item in self.candidates],
            "safe_to_delete": [
                f"{item.family}:{item.name}"
                + (f"@{item.version}" if item.version is not None else "")
                for item in self.deletable
            ],
        }


async def prune_definitions(
    pool: DatabasePool,
    *,
    workspace: str,
    catalog: DefinitionCatalog,
) -> PruneReport:
    """Report which inactive definitions nothing in the workspace still references.

    Only definitions that are *not* the active choice are reported: an active
    collection version, view, or artifact is in use by definition.  Everything
    else is counted against real rows, annotations, and runs, so a deletion can be
    justified rather than guessed.
    """

    candidates: list[PruneCandidate] = []
    async with pool.connection() as conn:
        row_counts: dict[tuple[str, int], int] = {}
        result = await conn.execute(
            """
            select collection, collection_version, count(*) as rows
            from record
            where workspace = %s and collection <> '_system'
            group by collection, collection_version
            """,
            (workspace,),
        )
        for row in await result.fetchall():
            row_counts[(str(row["collection"]), int(row["collection_version"]))] = int(row["rows"])

        annotation_counts: dict[str, int] = {}
        result = await conn.execute(
            """
            select name, count(*) as rows
            from record
            cross join lateral (
              select jsonb_object_keys(record.annotations) as name
            ) annotation
            where record.workspace = %s
            group by name
            """,
            (workspace,),
        )
        for row in await result.fetchall():
            annotation_counts[str(row["name"])] = int(row["rows"])

        derivation_counts: dict[str, int] = {}
        result = await conn.execute(
            """
            select content ->> 'processor' as derivation, count(*) as runs
            from record
            where workspace = %s and collection = '_system' and type = 'run'
              and content ->> 'operation' = 'derive'
            group by content ->> 'processor'
            """,
            (workspace,),
        )
        for row in await result.fetchall():
            derivation_counts[str(row["derivation"])] = int(row["runs"])

        artifact_counts: dict[tuple[str, int], int] = {}
        result = await conn.execute(
            """
            select artifact_name, artifact_version, count(*) as uses
            from artifact_use
            where workspace = %s
            group by artifact_name, artifact_version
            """,
            (workspace,),
        )
        for row in await result.fetchall():
            artifact_counts[(str(row["artifact_name"]), int(row["artifact_version"]))] = int(
                row["uses"]
            )

    for (name, version), collection in sorted(catalog.collections.items()):
        if catalog.active_collections.get(name) == version:
            continue
        rows = row_counts.get((name, version), 0)
        candidates.append(
            PruneCandidate(
                family="collection",
                name=name,
                version=version,
                references=rows,
                reference_kind="records",
                safe_to_delete=rows == 0,
                detail=(
                    "no record was written under this contract"
                    if rows == 0
                    else f"{rows} record(s) are bound to this contract"
                ),
            )
        )
        del collection

    bound_processors = {
        processor
        for collection in catalog.collections.values()
        for processor in (*collection.required_processors, *collection.optional_processors)
    }
    for name in sorted(catalog.processors):
        if name in bound_processors:
            continue
        annotated = annotation_counts.get(name, 0)
        candidates.append(
            PruneCandidate(
                family="processor",
                name=name,
                version=None,
                references=annotated,
                reference_kind="annotations",
                safe_to_delete=annotated == 0,
                detail=(
                    "no collection binds it and no annotation survives"
                    if annotated == 0
                    else f"{annotated} record(s) still hold an annotation under this name"
                ),
            )
        )

    for name in sorted(catalog.derivations):
        runs = derivation_counts.get(name, 0)
        if runs:
            candidates.append(
                PruneCandidate(
                    family="derivation",
                    name=name,
                    version=None,
                    references=runs,
                    reference_kind="runs",
                    safe_to_delete=False,
                    detail=f"{runs} run record(s) cite this derivation",
                )
            )

    for (name, version), _artifact in sorted(catalog.artifacts.items()):
        if catalog.active_artifacts.get(name) == version:
            continue
        uses = artifact_counts.get((name, version), 0)
        candidates.append(
            PruneCandidate(
                family="artifact",
                name=name,
                version=version,
                references=uses,
                reference_kind="artifact_uses",
                safe_to_delete=uses == 0,
                detail=(
                    "no use handle references this version"
                    if uses == 0
                    else f"{uses} use handle(s) reference this version"
                ),
            )
        )

    return PruneReport(workspace=workspace, candidates=tuple(candidates))


type RebindPolicy = Literal["reset", "carry"]


@dataclass(frozen=True, slots=True)
class RebindResult:
    workspace: str
    derivation: str
    entity: str
    policy: RebindPolicy
    previous_watermark: int
    watermark: int
    previous_source_hash: str | None
    source_hash: str

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "derivation": self.derivation,
            "entity": self.entity,
            "policy": self.policy,
            "previous_watermark": self.previous_watermark,
            "watermark": self.watermark,
            "previous_source_hash": self.previous_source_hash,
            "source_hash": self.source_hash,
        }


async def rebind_cursor(
    pool: DatabasePool,
    *,
    workspace: str,
    derivation: str,
    entity: str,
    policy: RebindPolicy,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> RebindResult:
    """Repoint a ``changes`` cursor after a deliberate source-scope change.

    A ``changes`` pipeline refuses to run when its source scope no longer matches
    the scope its cursor was established under, because silently skipping or
    double-counting rows would be worse than stopping.  This is how an operator
    says which they meant:

    ``reset``
        Restart from zero — correct when the widened scope must be fully re-read.
    ``carry``
        Keep the watermark and adopt the new source hash — correct when the
        widening only admits rows that have yet to arrive.

    Both write a ``_system/cursor_rebind`` audit naming the old and new hashes.
    """

    definition = catalog.derivations.get(derivation)
    if definition is None:
        raise EvolutionError("unknown_derivation", f"unknown derivation {derivation!r}")
    if definition.driver.kind != "changes":
        raise EvolutionError(
            "not_a_changes_source",
            "only a changes source keeps a cursor that can be rebound",
        )
    target_hash = source_contract_hash(definition)

    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        await acquire_entity_locks(conn, workspace, (entity,))
        previous_watermark, predecessor, previous_hash = await _watermark(
            conn, workspace=workspace, entity=entity, derivation=derivation
        )
        if predecessor is None:
            raise EvolutionError(
                "no_cursor",
                f"{derivation!r} has no established cursor for entity {entity!r}",
            )
        watermark = 0 if policy == "reset" else previous_watermark

        from memseek.enrichment import SYSTEM_COLLECTION_HASH, SYSTEM_COLLECTION_VERSION

        await insert_canonical_record_tx(
            conn,
            CanonicalRecordWrite(
                workspace=workspace,
                collection="_system",
                collection_version=SYSTEM_COLLECTION_VERSION,
                collection_hash=SYSTEM_COLLECTION_HASH,
                entity=entity,
                type="run",
                content=_rebind_audit(
                    derivation=derivation,
                    policy=policy,
                    previous_watermark=previous_watermark,
                    watermark=watermark,
                    predecessor=predecessor,
                    previous_hash=previous_hash,
                    source_hash=target_hash,
                    build_sha=settings.memseek_build_sha,
                ),
                ready=True,
                derived_from=(predecessor,),
            ),
            settings,
        )
    return RebindResult(
        workspace=workspace,
        derivation=derivation,
        entity=entity,
        policy=policy,
        previous_watermark=previous_watermark,
        watermark=watermark,
        previous_source_hash=previous_hash,
        source_hash=target_hash,
    )


def _rebind_audit(
    *,
    derivation: str,
    policy: str,
    previous_watermark: int,
    watermark: int,
    predecessor: UUID,
    previous_hash: str | None,
    source_hash: str,
    build_sha: str,
) -> dict[str, Any]:
    # The audit is itself the newest successful derive run, so the runner's
    # cursor read adopts its high_seq and source_hash.
    return {
        "text": (
            f"cursor rebind: {derivation} {policy} from seq {previous_watermark} to seq {watermark}"
        ),
        "operation": "derive",
        "processor": derivation,
        "status": "ok",
        "kind": "cursor_rebind",
        "policy": policy,
        "wm_before": previous_watermark,
        "high_seq": watermark,
        "predecessor_run_id": str(predecessor),
        "previous_source_hash": previous_hash,
        "source_hash": source_hash,
        "build_sha": build_sha,
    }


__all__ = [
    "ContractMigrationResult",
    "EvolutionError",
    "PruneCandidate",
    "PruneReport",
    "RebindResult",
    "migrate_collection_hashes",
    "prune_definitions",
    "rebind_cursor",
    "workspaces",
]
