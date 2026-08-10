"""Promotion and rollback: activate one complete reviewed snapshot.

Promotion copies the outputs of one prior run as new `status=active` rows
behind an `operation=promote` run.  Nothing is mutated: rollback is the same
operation with an older source run, and the keyed history keeps every
version.  Promotion approves; it never evaluates quality.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from memseek import __version__
from memseek.canonical_records import CanonicalRecordWrite, insert_canonical_record_tx
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.locks import acquire_entity_locks, acquire_workspace_lock


class PromotionError(ValueError):
    """A promotion request is invalid or conflicts with current state."""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(detail)


_SOURCE_OPERATIONS = frozenset({"derive", "materialize", "promote"})

_COPY_COLUMNS = """
    id, seq, collection, collection_version, collection_hash, entity, key, type,
    status, content, embedding_space, scores, annotations, annotation_meta, run_id,
    enrichment_meta, enrichment_error, enriched_at, depth, occurred_at
"""


def _expected_heads(content: dict[str, Any]) -> dict[tuple[str, str], UUID | None] | None:
    """Parse the active-head preconditions from a Candidate Set run."""

    candidate = content.get("candidate_set")
    basis = content.get("basis")
    if not isinstance(candidate, dict) or not isinstance(basis, dict):
        return None
    raw_heads = basis.get("expected_heads")
    if not isinstance(raw_heads, list):
        raise PromotionError("promotion_source", "candidate basis has invalid expected heads")
    heads: dict[tuple[str, str], UUID | None] = {}
    for item in raw_heads:
        if not isinstance(item, dict):
            raise PromotionError("promotion_source", "candidate basis has invalid expected heads")
        collection = item.get("collection")
        key = item.get("key")
        raw_id = item.get("record_id")
        if not isinstance(collection, str) or not isinstance(key, str):
            raise PromotionError("promotion_source", "candidate basis has invalid head identity")
        try:
            record_id = UUID(str(raw_id)) if raw_id is not None else None
        except (TypeError, ValueError, AttributeError) as exc:
            raise PromotionError(
                "promotion_source", "candidate basis has invalid head record ID"
            ) from exc
        heads[(collection, key)] = record_id
    return heads


def _promotion_run_content(
    *,
    run_id: UUID,
    entity: str,
    artifact: str | None,
    source_run_id: UUID,
    source_output_ids: list[UUID],
    superseded_ids: list[UUID],
    output_ids: list[UUID],
    started_at: datetime,
    settings: Settings,
) -> dict[str, Any]:
    completed = datetime.now(UTC)
    return {
        "text": (
            f"promote {len(output_ids)} row(s) from run {source_run_id}"
            + (f" for artifact {artifact}" if artifact else "")
        ),
        "schema_version": 1,
        "engine_version": f"{__version__}+{settings.memseek_build_sha}",
        "operation": "promote",
        "status": "ok",
        "run_id": str(run_id),
        "entity": entity,
        "artifact": artifact,
        "source_run_id": str(source_run_id),
        "source_output_ids": [str(item) for item in source_output_ids],
        "superseded_ids": [str(item) for item in superseded_ids],
        "output_ids": [str(item) for item in output_ids],
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "ms": max(0, int((completed - started_at).total_seconds() * 1_000)),
        "error_kind": None,
        "error": None,
    }


async def promote_run(
    pool: DatabasePool,
    *,
    workspace: str,
    entity: str,
    source_run_id: UUID,
    artifact: str | None,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Activate the complete output snapshot of one prior run, all-or-none."""

    started_at = datetime.now(UTC)
    artifact_definition = None
    if artifact is not None:
        try:
            artifact_definition = catalog.resolve_artifact(artifact)
        except (KeyError, ValueError) as exc:
            raise PromotionError("artifact_not_found", f"unknown artifact {artifact!r}") from exc
        if artifact_definition.lifecycle != "reviewed":
            raise PromotionError(
                "promotion_source", f"artifact {artifact!r} is not lifecycle=reviewed"
            )

    async with pool.connection() as conn, conn.transaction():
        await acquire_workspace_lock(conn, workspace)
        await acquire_entity_locks(conn, workspace, (entity,))
        run_result = await conn.execute(
            """
            select id, entity, content from record
            where workspace = %s and id = %s and collection = '_system' and type = 'run'
            """,
            (workspace, source_run_id),
        )
        run_row = await run_result.fetchone()
        if run_row is None:
            raise PromotionError("run_not_found", "source run does not exist", status=404)
        if run_row["entity"] != entity:
            raise PromotionError("promotion_source", "source run belongs to a different entity")
        run_content: dict[str, Any] = run_row["content"]
        source_operation = run_content.get("operation")
        if source_operation not in _SOURCE_OPERATIONS:
            raise PromotionError(
                "promotion_source", "source must be a derive, materialize, or promote run"
            )
        raw_output_ids = run_content.get("output_ids")
        if not isinstance(raw_output_ids, list) or not raw_output_ids:
            raise PromotionError("promotion_source", "source run has no output rows")
        source_output_ids = [UUID(str(item)) for item in raw_output_ids]

        if artifact_definition is not None:
            if source_operation != "derive":
                raise PromotionError(
                    "promotion_source",
                    "a reviewed artifact can promote only a derive candidate run",
                )
            if run_content.get("processor") != artifact_definition.candidate_processor:
                raise PromotionError(
                    "promotion_source",
                    f"artifact {artifact!r} accepts candidates only from "
                    f"{artifact_definition.candidate_processor!r}",
                )

        # Select source rows strictly from the run record's own output list.
        source_result = await conn.execute(
            f"select {_COPY_COLUMNS} from record"
            " where workspace = %s and id = any(%s::uuid[]) for share",
            (workspace, source_output_ids),
        )
        sources_by_id = {row["id"]: dict(row) for row in await source_result.fetchall()}
        missing = [item for item in source_output_ids if item not in sources_by_id]
        if missing:
            raise PromotionError(
                "promotion_source",
                f"{len(missing)} source output row(s) no longer exist",
                status=409,
            )
        sources = [sources_by_id[item] for item in source_output_ids]
        for row in sources:
            if row["run_id"] != source_run_id:
                raise PromotionError(
                    "promotion_source", "source output does not belong to the source run"
                )
            if row["key"] is None:
                raise PromotionError("promotion_source", "every source row must be keyed")
            if row["enriched_at"] is None:
                raise PromotionError(
                    "promotion_source", "every source row must be ready", status=409
                )
            if row["entity"] != entity:
                raise PromotionError("promotion_source", "source rows span another entity")

        candidate_manifest = run_content.get("candidate_set")
        if source_operation == "derive":
            if not isinstance(candidate_manifest, dict):
                raise PromotionError(
                    "promotion_source", "derive promotion requires a Candidate Set manifest"
                )
            if candidate_manifest.get("status") != "draft" or any(
                row["status"] != "draft" for row in sources
            ):
                raise PromotionError(
                    "promotion_source", "derive promotion requires a draft Candidate Set"
                )
            covered_keys = candidate_manifest.get("covered_keys")
            if not isinstance(covered_keys, list) or set(covered_keys) != {
                cast(str, row["key"]) for row in sources
            }:
                raise PromotionError(
                    "promotion_source", "candidate manifest does not match source output keys"
                )
            if candidate_manifest.get("coverage") != "complete":
                raise PromotionError(
                    "promotion_source", "derive promotion requires a complete Candidate Set"
                )

        if artifact_definition is not None:
            keys = {cast(str, row["key"]) for row in sources}
            expected_keys = set(artifact_definition.complete_keys)
            if keys != expected_keys:
                missing_keys = sorted(expected_keys - keys)
                extra_keys = sorted(keys - expected_keys)
                detail = []
                if missing_keys:
                    detail.append(f"missing keys: {', '.join(missing_keys)}")
                if extra_keys:
                    detail.append(f"unexpected keys: {', '.join(extra_keys)}")
                raise PromotionError(
                    "promotion_incomplete",
                    "source outputs do not satisfy the artifact snapshot contract; "
                    + "; ".join(detail),
                )
            candidate_processor = catalog.derivations[
                cast(str, artifact_definition.candidate_processor)
            ]
            expected_emit = candidate_processor.emit
            if any(
                row["collection"] != expected_emit.collection
                or row["collection_version"] != expected_emit.collection_version
                or row["type"] != expected_emit.type
                for row in sources
            ):
                raise PromotionError(
                    "promotion_source",
                    "source outputs do not match the artifact candidate destination",
                )

        # Current active head per source collection/key pair.
        pairs = sorted({(cast(str, row["collection"]), cast(str, row["key"])) for row in sources})
        current_result = await conn.execute(
            """
            select distinct on (collection, key) id, collection, key, run_id, derived_from
            from record
            where workspace = %s and entity = %s and status = 'active'
              and (collection, key) in (
                select unnest(%s::text[]), unnest(%s::text[])
              )
            order by collection, key, seq desc
            """,
            (
                workspace,
                entity,
                [pair[0] for pair in pairs],
                [pair[1] for pair in pairs],
            ),
        )
        current_by_pair = {
            (row["collection"], row["key"]): dict(row) for row in await current_result.fetchall()
        }

        def _already_promoted(source_row: dict[str, Any]) -> bool:
            head = current_by_pair.get((source_row["collection"], source_row["key"]))
            if head is None:
                return False
            return source_row["id"] in tuple(head["derived_from"])

        if all(_already_promoted(row) for row in sources):
            head_run_ids = {
                current_by_pair[(row["collection"], cast(str, row["key"]))]["run_id"]
                for row in sources
            }
            existing = head_run_ids.pop() if len(head_run_ids) == 1 else None
            return {
                "promotion_run_id": str(existing) if existing is not None else None,
                "promoted": 0,
                "skipped": len(sources),
                "output_ids": [],
            }

        expected_heads = _expected_heads(run_content)
        if expected_heads is not None:
            for row in sources:
                pair = (cast(str, row["collection"]), cast(str, row["key"]))
                if pair not in expected_heads:
                    raise PromotionError(
                        "promotion_source",
                        f"candidate basis has no active-head precondition for {pair[0]}/{pair[1]}",
                    )
                current = current_by_pair.get(pair)
                current_id = cast(UUID | None, current["id"] if current is not None else None)
                if current_id != expected_heads[pair]:
                    raise PromotionError(
                        "promotion_stale",
                        f"active head changed after candidate generation: {pair[0]}/{pair[1]}",
                        status=409,
                    )

        run_id = uuid4()
        copies = [(row, uuid4()) for row in sources]
        superseded_ids = sorted(
            {
                cast(UUID, head["id"])
                for row in sources
                if (head := current_by_pair.get((row["collection"], cast(str, row["key"]))))
                is not None
            },
            key=str,
        )
        run_depth = max(int(row["depth"]) for row in sources)
        await insert_canonical_record_tx(
            conn,
            CanonicalRecordWrite(
                id=run_id,
                workspace=workspace,
                collection="_system",
                collection_version=_system_collection_version(),
                collection_hash=_system_collection_hash(),
                entity=entity,
                type="run",
                content=_promotion_run_content(
                    run_id=run_id,
                    entity=entity,
                    artifact=artifact,
                    source_run_id=source_run_id,
                    source_output_ids=source_output_ids,
                    superseded_ids=superseded_ids,
                    output_ids=[copy_id for _, copy_id in copies],
                    started_at=started_at,
                    settings=settings,
                ),
                ready=True,
                depth=run_depth,
                # The promotion run's semantic parents are only the source rows.
                derived_from=tuple(row["id"] for row in sources),
            ),
            settings,
        )
        for row, copy_id in copies:
            await insert_canonical_record_tx(
                conn,
                CanonicalRecordWrite(
                    id=copy_id,
                    workspace=workspace,
                    collection=row["collection"],
                    collection_version=int(row["collection_version"]),
                    collection_hash=row["collection_hash"],
                    entity=entity,
                    key=row["key"],
                    type=row["type"],
                    status="active",
                    content=row["content"],
                    scores=row["scores"],
                    annotations=row["annotations"],
                    annotation_meta=row["annotation_meta"],
                    enrichment_meta=row["enrichment_meta"],
                    enrichment_error=row["enrichment_error"],
                    ready=True,
                    run_id=run_id,
                    depth=int(row["depth"]),
                    derived_from=(run_id, cast(UUID, row["id"])),
                    occurred_at=row["occurred_at"],
                ),
                settings,
            )
            # The canonical boundary owns row invariants; the embedding vector
            # is enrichment output and is copied verbatim from the source row.
            await conn.execute(
                """
                update record
                set embedding = src.embedding, embedding_space = src.embedding_space
                from record src
                where record.id = %s and src.id = %s
                """,
                (copy_id, row["id"]),
            )
        from memseek.projections import on_records_ready_tx

        await on_records_ready_tx(
            conn,
            workspace=workspace,
            records=[{"id": run_id}, *({"id": copy_id} for _, copy_id in copies)],
            catalog=catalog,
        )
    return {
        "promotion_run_id": str(run_id),
        "promoted": len(copies),
        "skipped": 0,
        "output_ids": [str(copy_id) for _, copy_id in copies],
    }


def _system_collection_version() -> int:
    from memseek.enrichment import SYSTEM_COLLECTION_VERSION

    return SYSTEM_COLLECTION_VERSION


def _system_collection_hash() -> str:
    from memseek.enrichment import SYSTEM_COLLECTION_HASH

    return SYSTEM_COLLECTION_HASH


__all__ = ["PromotionError", "promote_run"]
