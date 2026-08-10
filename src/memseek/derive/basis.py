"""Private source-receipt resolution and verification for pipelines.

Authors select named sources. This Module owns the deeper Implementation:
incremental cursors, bounded snapshot checkpoints, current-read receipts, and
expected active target heads. Those details remain auditable but are never
authoring knobs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, LiteralString, Protocol, cast
from uuid import UUID

from memseek.db import DatabaseConnection
from memseek.derive.errors import DerivationError
from memseek.derive.schema import (
    CurrentSource,
    PipelineDefinition,
    RecordSource,
    StreamSource,
)

type BasisMode = Literal["changes", "corpus", "citation_repair"]


@dataclass(frozen=True, slots=True)
class DerivationRecord:
    """The storage fields a Task may inspect or use as a guard."""

    id: UUID
    seq: int
    collection: str
    collection_version: int
    entity: str
    key: str | None
    type: str
    status: str
    content: Mapping[str, Any]
    scores: Mapping[str, Any]
    occurred_at: datetime
    depth: int


@dataclass(frozen=True, slots=True)
class ExpectedHead:
    """One active keyed target captured before Task execution."""

    collection: str
    key: str
    record_id: UUID | None
    content: Mapping[str, Any] | None = None
    depth: int | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "key": self.key,
            "status": "active",
            "record_id": str(self.record_id) if self.record_id is not None else None,
        }


@dataclass(frozen=True, slots=True)
class EvaluationBasis:
    """Private immutable read receipt persisted with each run."""

    mode: BasisMode
    from_seq: int | None
    through_seq: int
    predecessor_run_id: UUID | None
    predecessor_source_hash: str | None
    input_rows: tuple[DerivationRecord, ...]
    read_rows: Mapping[str, tuple[DerivationRecord, ...]]
    expected_heads: tuple[ExpectedHead, ...]

    @property
    def watermark(self) -> int:
        return self.from_seq or 0

    @property
    def context_rows(self) -> tuple[DerivationRecord, ...]:
        return tuple(row for rows in self.read_rows.values() for row in rows)

    def manifest(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "from_seq": self.from_seq,
            "through_seq": self.through_seq,
            "input_ids": [str(row.id) for row in self.input_rows],
            "reads": {name: [str(row.id) for row in rows] for name, rows in self.read_rows.items()},
            "expected_heads": [head.manifest() for head in self.expected_heads],
            "predecessor_run_id": (
                str(self.predecessor_run_id) if self.predecessor_run_id is not None else None
            ),
            "predecessor_source_hash": self.predecessor_source_hash,
        }


class BasisAdapter(Protocol):
    async def resolve(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
    ) -> EvaluationBasis | None: ...

    async def verify(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
        basis: EvaluationBasis,
    ) -> None: ...


ROW_COLUMNS = """
id, seq, collection, collection_version, entity, key, type, status,
content, scores, occurred_at, depth
"""


def derivation_record(row: Mapping[str, Any]) -> DerivationRecord:
    return DerivationRecord(
        id=cast(UUID, row["id"]),
        seq=int(row["seq"]),
        collection=str(row["collection"]),
        collection_version=int(row["collection_version"]),
        entity=str(row["entity"]),
        key=cast(str | None, row["key"]),
        type=str(row["type"]),
        status=str(row["status"]),
        content=cast(Mapping[str, Any], row["content"]),
        scores=cast(Mapping[str, Any], row["scores"]),
        occurred_at=cast(datetime, row["occurred_at"]),
        depth=int(row["depth"]),
    )


def scope_sql(scope: Any, *, workspace: str, alias: str = "record") -> tuple[list[str], list[Any]]:
    clauses = [f"{alias}.workspace = %s"]
    params: list[Any] = [workspace]
    terms: list[str] = []
    for name, versions in scope.collection_versions.items():
        terms.append(f"({alias}.collection = %s and {alias}.collection_version = any(%s::int[]))")
        params.extend([name, list(versions)])
    unpinned = [name for name in scope.collections if name not in scope.collection_versions]
    if unpinned:
        terms.append(f"{alias}.collection = any(%s::text[])")
        params.append(unpinned)
    clauses.append("(" + " or ".join(terms) + ")")
    if scope.types:
        clauses.append(f"{alias}.type = any(%s::text[])")
        params.append(list(scope.types))
    clauses.append(f"{alias}.status = any(%s::text[])")
    params.append(list(scope.statuses))
    if scope.keyed is True:
        clauses.append(f"{alias}.key is not null")
    elif scope.keyed is False:
        clauses.append(f"{alias}.key is null")
    return clauses, params


async def _watermark(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> tuple[int, UUID | None, str | None]:
    result = await conn.execute(
        """
        select (content->>'high_seq')::bigint as high_seq,
               id, content->>'source_hash' as source_hash
        from record
        where workspace = %s and entity = %s
          and collection = '_system' and type = 'run'
          and content->>'operation' = 'derive'
          and content->>'processor' = %s
          and content->>'status' in ('ok', 'noop')
        order by (content->>'high_seq')::bigint desc, seq desc
        limit 1
        """,
        (workspace, entity, derivation),
    )
    row = await result.fetchone()
    if row is None:
        return 0, None, None
    return int(row["high_seq"] or 0), cast(UUID, row["id"]), cast(str | None, row["source_hash"])


def source_contract_hash(definition: PipelineDefinition) -> str:
    """Hash only the source fields that determine cursor membership."""

    payload = definition.driver.model_dump(
        mode="json", exclude={"max_records", "max_tokens", "allow_empty"}
    )
    for field in ("collections", "types", "statuses"):
        payload[field] = sorted(payload[field])
    payload["collection_versions"] = {
        collection: sorted(versions)
        for collection, versions in payload["collection_versions"].items()
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _current_source_rows(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    source: CurrentSource,
) -> tuple[DerivationRecord, ...] | None:
    clauses, params = scope_sql(source, workspace=workspace)
    clauses.extend(["record.entity = %s", "record.key is not null"])
    params.append(entity)
    if source.keys:
        clauses.append("record.key = any(%s::text[])")
        params.append(list(source.keys))
    query = cast(
        LiteralString,
        f"""
        select distinct on (record.collection, record.key, record.status)
               {ROW_COLUMNS}, record.enriched_at
        from record
        where {" and ".join(clauses)}
        order by record.collection, record.key, record.status, record.seq desc
        limit %s
        """,
    )
    result = await conn.execute(query, [*params, source.max_records + 1])
    rows = await result.fetchall()
    if len(rows) > source.max_records:
        raise DerivationError("budget", "current source exceeds max_records")
    if any(item["enriched_at"] is None for item in rows):
        return None
    return tuple(derivation_record(item) for item in rows)


async def _record_source_rows(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    source: RecordSource,
) -> tuple[DerivationRecord, ...] | None:
    clauses = [
        "record.workspace = %s",
        "record.entity = %s",
        "record.collection = %s",
        "record.collection_version = %s",
        "record.key = %s",
        "record.status = %s",
    ]
    params: list[Any] = [
        workspace,
        entity,
        source.collection,
        source.collection_version,
        source.key,
        source.status,
    ]
    if source.type is not None:
        clauses.append("record.type = %s")
        params.append(source.type)
    query = cast(
        LiteralString,
        f"select {ROW_COLUMNS}, record.enriched_at from record "
        f"where {' and '.join(clauses)} order by seq desc limit 1",
    )
    result = await conn.execute(query, params)
    row = await result.fetchone()
    if row is None:
        return ()
    if row["enriched_at"] is None:
        return None
    return (derivation_record(row),)


async def _read_rows(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
) -> Mapping[str, tuple[DerivationRecord, ...]] | None:
    rows: dict[str, tuple[DerivationRecord, ...]] = {}
    for name, source in definition.sources.items():
        if isinstance(source, CurrentSource):
            selected = await _current_source_rows(
                conn, workspace=workspace, entity=entity, source=source
            )
            if selected is None:
                return None
            rows[name] = selected
        elif isinstance(source, RecordSource):
            selected = await _record_source_rows(
                conn, workspace=workspace, entity=entity, source=source
            )
            if selected is None:
                return None
            rows[name] = selected
    return MappingProxyType(rows)


async def _expected_heads(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    input_rows: tuple[DerivationRecord, ...] = (),
) -> tuple[ExpectedHead, ...]:
    emit = definition.emit
    if emit.keys:
        keys = emit.keys
    elif emit.driver_key:
        if len(input_rows) != 1 or input_rows[0].key is None:
            raise DerivationError("config", "driver_key emission requires one keyed input record")
        keys = (input_rows[0].key,)
    elif not emit.dynamic_keys:
        return ()
    if emit.dynamic_keys:
        result = await conn.execute(
            """
            select distinct on (key) id, key, content, depth
            from record
            where workspace = %s and entity = %s and collection = %s
              and status = 'active' and key is not null
            order by key, seq desc
            """,
            (workspace, entity, emit.collection),
        )
        ordered = await result.fetchall()
        return tuple(
            ExpectedHead(
                collection=emit.collection,
                key=str(row["key"]),
                record_id=cast(UUID, row["id"]),
                content=cast(Mapping[str, Any], row["content"]),
                depth=int(row["depth"]) if row.get("depth") is not None else None,
            )
            for row in ordered
        )
    result = await conn.execute(
        """
        select distinct on (key) id, key, content, depth
        from record
        where workspace = %s and entity = %s and collection = %s
          and status = 'active' and key = any(%s::text[])
        order by key, seq desc
        """,
        (workspace, entity, emit.collection, list(keys)),
    )
    rows = {str(row["key"]): row for row in await result.fetchall()}
    return tuple(
        ExpectedHead(
            collection=emit.collection,
            key=key,
            record_id=cast(UUID | None, rows.get(key, {}).get("id")),
            content=cast(Mapping[str, Any] | None, rows.get(key, {}).get("content")),
            depth=(
                int(rows[key]["depth"])
                if key in rows and rows[key].get("depth") is not None
                else None
            ),
        )
        for key in keys
    )


async def _changes_input(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    source: StreamSource,
    watermark: int,
) -> tuple[DerivationRecord, ...] | None:
    clauses, params = scope_sql(source, workspace=workspace)
    clauses.extend(["record.entity = %s", "record.seq > %s"])
    params.extend([entity, watermark])
    query = cast(
        LiteralString,
        f"""
        select {ROW_COLUMNS}, record.enriched_at
        from record
        where {" and ".join(clauses)}
        order by record.seq
        limit %s
        """,
    )
    result = await conn.execute(query, [*params, source.max_records])
    rows = await result.fetchall()
    prefix: list[DerivationRecord] = []
    for item in rows:
        if item["enriched_at"] is None:
            return tuple(prefix) if prefix else None
        prefix.append(derivation_record(item))
    return tuple(prefix)


async def _snapshot_input(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    source: StreamSource,
) -> tuple[int, int | None, tuple[DerivationRecord, ...] | None]:
    clauses, params = scope_sql(source, workspace=workspace)
    clauses.append("record.entity = %s")
    params.append(entity)
    window = source.window
    # A since/until window narrows both the frozen checkpoint and membership by
    # occurred_at, so "complete" holds over the declared range.
    if window is not None and window.since is not None:
        clauses.append("record.occurred_at >= %s")
        params.append(window.since)
    if window is not None and window.until is not None:
        clauses.append("record.occurred_at <= %s")
        params.append(window.until)
    where = " and ".join(clauses)
    checkpoint_query = cast(
        LiteralString,
        f"select coalesce(max(record.seq), 0) as high_seq from record where {where}",
    )
    checkpoint_result = await conn.execute(checkpoint_query, params)
    checkpoint_row = await checkpoint_result.fetchone()
    through_seq = int(checkpoint_row["high_seq"] if checkpoint_row is not None else 0)
    if through_seq == 0:
        return 0, None, ()
    # A recent window takes the newest N rows at/below the checkpoint (fetched
    # descending, then presented ascending); otherwise the whole corpus is read
    # ascending with the usual max_records ceiling.
    recent = window.recent if window is not None else None
    order = "desc" if recent is not None else "asc"
    limit = recent if recent is not None else source.max_records + 1
    query = cast(
        LiteralString,
        f"""
        select {ROW_COLUMNS}, record.enriched_at
        from record
        where {where} and record.seq <= %s
        order by record.seq {order}
        limit %s
        """,
    )
    result = await conn.execute(query, [*params, through_seq, limit])
    rows = await result.fetchall()
    # The windowed corpus must still fit the declared ceiling.  A recent window
    # only trips this when `recent` itself exceeds `max_records`.
    if len(rows) > source.max_records:
        raise DerivationError(
            "budget",
            "snapshot source exceeds max_records; narrow the window/scope or increase its bound",
        )
    if recent is not None:
        rows = list(reversed(rows))
    if any(row["enriched_at"] is None for row in rows):
        return through_seq, None, None
    records = tuple(derivation_record(item) for item in rows)
    from_seq = records[0].seq if records else None
    return through_seq, from_seq, records


async def _stale_citation_input(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    source: StreamSource,
) -> tuple[DerivationRecord, ...] | None:
    """Return ready current keyed records with a directly stale citation.

    This is deliberately a narrow provenance join, not a general YAML-accessible
    record lookup: the source itself remains a normal scoped collection read.
    A cited keyed parent is stale when a newer ready version with the same
    canonical slot exists, including a ready tombstone.
    """

    clauses, params = scope_sql(source, workspace=workspace)
    clauses.extend(["record.entity = %s", "record.key is not null"])
    params.append(entity)
    query = cast(
        LiteralString,
        f"""
        with current_records as (
          select distinct on (
            record.collection, record.collection_version, record.key, record.status
          )
                 {ROW_COLUMNS}, record.enriched_at, record.derived_from
          from record
          where {" and ".join(clauses)}
          order by record.collection, record.collection_version, record.key,
                   record.status, record.seq desc
        )
        select {ROW_COLUMNS}, enriched_at
        from current_records
        where enriched_at is not null
          and content->>'tombstone' is distinct from 'true'
          and exists (
            select 1
            from unnest(derived_from) as cited(id)
            join record parent
              on parent.id = cited.id and parent.workspace = %s
            where parent.collection <> '_system'
              and parent.key is not null
              and exists (
                select 1
                from record newer
                where newer.workspace = parent.workspace
                  and newer.entity = parent.entity
                  and newer.collection = parent.collection
                  and newer.collection_version = parent.collection_version
                  and newer.key = parent.key
                  and newer.status = parent.status
                  and newer.seq > parent.seq
                  and newer.enriched_at is not null
              )
          )
        order by seq
        limit %s
        """,
    )
    result = await conn.execute(query, [*params, workspace, source.max_records + 1])
    rows = await result.fetchall()
    if len(rows) > source.max_records:
        raise DerivationError("budget", "stale_citations source exceeds max_records")
    return tuple(derivation_record(item) for item in rows)


async def _verify_reads(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    expected: Mapping[str, tuple[DerivationRecord, ...]],
    wm: int,
) -> None:
    current = await _read_rows(conn, workspace=workspace, entity=entity, definition=definition)
    if current is None:
        raise DerivationError("stale", "a current source became unready", wm=wm)
    if {name: tuple(row.id for row in rows) for name, rows in current.items()} != {
        name: tuple(row.id for row in rows) for name, rows in expected.items()
    }:
        raise DerivationError("stale", "a current source changed during Task execution", wm=wm)


async def _verify_heads(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    expected: tuple[ExpectedHead, ...],
    input_rows: tuple[DerivationRecord, ...],
    wm: int,
) -> None:
    current = await _expected_heads(
        conn,
        workspace=workspace,
        entity=entity,
        definition=definition,
        input_rows=input_rows,
    )
    expected_for_verification = expected
    if definition.emit.dynamic_keys:
        # Candidate compilation adds explicit ``None`` heads for brand-new
        # names.  They did not exist when the Task began, so they cannot appear
        # in the pre-commit database snapshot.  Any real concurrent creation
        # still appears in ``current`` and makes this comparison fail.
        expected_for_verification = tuple(head for head in expected if head.record_id is not None)
    if tuple((head.collection, head.key, head.record_id) for head in current) != tuple(
        (head.collection, head.key, head.record_id) for head in expected_for_verification
    ):
        raise DerivationError(
            "stale", "active emission target changed during Task execution", wm=wm
        )


class ChangesBasisAdapter:
    async def resolve(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
    ) -> EvaluationBasis | None:
        wm, predecessor, predecessor_hash = await _watermark(
            conn, workspace=workspace, entity=entity, derivation=definition.name
        )
        if predecessor is not None and predecessor_hash != source_contract_hash(definition):
            raise DerivationError(
                "config",
                "changes source scope differs from the established cursor; use a new "
                "pipeline name or a snapshot pipeline",
                wm=wm,
            )
        inputs = await _changes_input(
            conn,
            workspace=workspace,
            entity=entity,
            source=definition.driver,
            watermark=wm,
        )
        if inputs is None:
            return None
        if not inputs and not definition.driver.allow_empty:
            return EvaluationBasis(
                mode="changes",
                from_seq=wm,
                through_seq=wm,
                predecessor_run_id=predecessor,
                predecessor_source_hash=predecessor_hash,
                input_rows=(),
                read_rows=MappingProxyType({}),
                expected_heads=(),
            )
        reads = await _read_rows(conn, workspace=workspace, entity=entity, definition=definition)
        if reads is None:
            return None
        heads = await _expected_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            input_rows=inputs,
        )
        return EvaluationBasis(
            mode="changes",
            from_seq=wm,
            through_seq=inputs[-1].seq if inputs else wm,
            predecessor_run_id=predecessor,
            predecessor_source_hash=predecessor_hash,
            input_rows=inputs,
            read_rows=reads,
            expected_heads=heads,
        )

    async def verify(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
        basis: EvaluationBasis,
    ) -> None:
        wm, predecessor, predecessor_hash = await _watermark(
            conn, workspace=workspace, entity=entity, derivation=definition.name
        )
        if wm != basis.watermark or predecessor != basis.predecessor_run_id:
            raise DerivationError(
                "stale", "source cursor changed during Task execution", wm=basis.watermark
            )
        if predecessor is not None and predecessor_hash != source_contract_hash(definition):
            raise DerivationError(
                "config", "changes source scope differs from the established cursor", wm=wm
            )
        if not basis.input_rows and not definition.driver.allow_empty:
            return
        await _verify_reads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.read_rows,
            wm=basis.watermark,
        )
        await _verify_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.expected_heads,
            input_rows=basis.input_rows,
            wm=basis.watermark,
        )


class CorpusBasisAdapter:
    async def resolve(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
    ) -> EvaluationBasis | None:
        through_seq, from_seq, inputs = await _snapshot_input(
            conn, workspace=workspace, entity=entity, source=definition.driver
        )
        if inputs is None:
            return None
        if not inputs and not definition.driver.allow_empty:
            return EvaluationBasis(
                mode="corpus",
                from_seq=None,
                through_seq=through_seq,
                predecessor_run_id=None,
                predecessor_source_hash=None,
                input_rows=(),
                read_rows=MappingProxyType({}),
                expected_heads=(),
            )
        reads = await _read_rows(conn, workspace=workspace, entity=entity, definition=definition)
        if reads is None:
            return None
        heads = await _expected_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            input_rows=inputs,
        )
        return EvaluationBasis(
            mode="corpus",
            from_seq=from_seq,
            through_seq=through_seq,
            predecessor_run_id=None,
            predecessor_source_hash=None,
            input_rows=inputs,
            read_rows=reads,
            expected_heads=heads,
        )

    async def verify(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
        basis: EvaluationBasis,
    ) -> None:
        if not basis.input_rows and not definition.driver.allow_empty:
            return
        await _verify_reads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.read_rows,
            wm=0,
        )
        await _verify_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.expected_heads,
            input_rows=basis.input_rows,
            wm=0,
        )


class StaleCitationBasisAdapter:
    """Replay one bounded keyed output when one of its direct citations moved."""

    async def resolve(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
    ) -> EvaluationBasis | None:
        inputs = await _stale_citation_input(
            conn, workspace=workspace, entity=entity, source=definition.driver
        )
        if inputs is None:
            return None
        through_seq = max((item.seq for item in inputs), default=0)
        if not inputs and not definition.driver.allow_empty:
            return EvaluationBasis(
                mode="citation_repair",
                from_seq=None,
                through_seq=through_seq,
                predecessor_run_id=None,
                predecessor_source_hash=None,
                input_rows=(),
                read_rows=MappingProxyType({}),
                expected_heads=(),
            )
        reads = await _read_rows(conn, workspace=workspace, entity=entity, definition=definition)
        if reads is None:
            return None
        heads = await _expected_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            input_rows=inputs,
        )
        return EvaluationBasis(
            mode="citation_repair",
            from_seq=inputs[0].seq if inputs else None,
            through_seq=through_seq,
            predecessor_run_id=None,
            predecessor_source_hash=None,
            input_rows=inputs,
            read_rows=reads,
            expected_heads=heads,
        )

    async def verify(
        self,
        conn: DatabaseConnection,
        *,
        workspace: str,
        entity: str,
        definition: PipelineDefinition,
        basis: EvaluationBasis,
    ) -> None:
        if not basis.input_rows and not definition.driver.allow_empty:
            return
        current = await _stale_citation_input(
            conn, workspace=workspace, entity=entity, source=definition.driver
        )
        if current is None:
            raise DerivationError("stale", "a stale_citations source became unready")
        if tuple(item.id for item in current) != tuple(item.id for item in basis.input_rows):
            raise DerivationError(
                "stale", "stale citation membership changed during Task execution"
            )
        await _verify_reads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.read_rows,
            wm=0,
        )
        await _verify_heads(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            expected=basis.expected_heads,
            input_rows=basis.input_rows,
            wm=0,
        )


_ADAPTERS: dict[BasisMode, BasisAdapter] = {
    "changes": ChangesBasisAdapter(),
    "corpus": CorpusBasisAdapter(),
    "citation_repair": StaleCitationBasisAdapter(),
}


def adapter_for(kind: str) -> BasisAdapter:
    """Map the friendly source kind to its private receipt Adapter."""

    if kind == "snapshot":
        return _ADAPTERS["corpus"]
    if kind == "stale_citations":
        return _ADAPTERS["citation_repair"]
    return _ADAPTERS[cast(BasisMode, kind)]


__all__ = [
    "ROW_COLUMNS",
    "BasisAdapter",
    "BasisMode",
    "ChangesBasisAdapter",
    "CorpusBasisAdapter",
    "DerivationRecord",
    "EvaluationBasis",
    "ExpectedHead",
    "StaleCitationBasisAdapter",
    "adapter_for",
    "derivation_record",
    "scope_sql",
    "source_contract_hash",
]
