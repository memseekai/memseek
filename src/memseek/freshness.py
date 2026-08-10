"""Read-triggered derivation freshness and stale-while-revalidate enqueueing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, LiteralString

from memseek.db import DatabaseConnection
from memseek.definitions import DefinitionCatalog
from memseek.derive.schema import PipelineDefinition

type JobState = Literal["enqueued", "queued", "running", "dead"]


@dataclass(frozen=True, slots=True)
class DerivationFreshness:
    """One ``freshness`` entry for a read-triggered derivation."""

    derivation: str
    last_run_at: str | None
    watermark: int
    dirty: bool
    pending_unready: bool
    job: JobState | None
    error_kind: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "derivation": self.derivation,
            "last_run_at": self.last_run_at,
            "watermark": self.watermark,
            "dirty": self.dirty,
            "pending_unready": self.pending_unready,
            "job": self.job,
            "error_kind": self.error_kind,
        }


def read_triggered_derivations(catalog: DefinitionCatalog) -> tuple[PipelineDefinition, ...]:
    """Return the derivations reachable from a ``read`` trigger, sorted by name."""

    names = sorted(
        {trigger.processor for trigger in catalog.triggers.values() if trigger.read}
        & set(catalog.derivations)
    )
    return tuple(catalog.derivations[name] for name in names)


def _parse_run_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


async def _watermark(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> tuple[int, str | None]:
    result = await conn.execute(
        """
        select (content->>'high_seq')::bigint as watermark,
               content->>'completed_at' as completed_at
        from record
        where workspace = %s
          and entity = %s
          and collection = '_system'
          and type = 'run'
          and content->>'operation' = 'derive'
          and coalesce(content->>'processor', content->>'derivation') = %s
          and content->>'status' in ('ok', 'noop')
        order by (content->>'high_seq')::bigint desc, seq desc
        limit 1
        """,
        (workspace, entity, derivation),
    )
    row = await result.fetchone()
    if row is None:
        return 0, None
    completed_at = row["completed_at"]
    return int(row["watermark"]), completed_at if isinstance(completed_at, str) else None


async def _first_pending_input(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    watermark: int,
) -> tuple[bool, bool]:
    """Probe the oldest input-scope row above the watermark.

    Readiness deliberately does not filter the probe: an unready first match
    is exactly the spec's ``pending_unready`` barrier signal.
    """

    scope = definition.driver
    clauses: list[LiteralString] = ["workspace = %s", "entity = %s", "seq > %s"]
    params: list[Any] = [workspace, entity, watermark]
    unpinned = [name for name in scope.collections if name not in scope.collection_versions]
    collection_terms: list[LiteralString] = []
    if unpinned:
        collection_terms.append("collection = any(%s::text[])")
        params.append(unpinned)
    for name, versions in scope.collection_versions.items():
        collection_terms.append("(collection = %s and collection_version = any(%s::int[]))")
        params.extend([name, list(versions)])
    clauses.append("(" + " or ".join(collection_terms) + ")")
    if scope.types:
        clauses.append("type = any(%s::text[])")
        params.append(list(scope.types))
    clauses.append("status = any(%s::text[])")
    params.append(list(scope.statuses))
    if scope.keyed is False:
        clauses.append("key is null")
    elif scope.keyed is True:
        clauses.append("key is not null")
    result = await conn.execute(
        f"""
        select enriched_at is not null as ready
        from record
        where {" and ".join(clauses)}
        order by seq
        limit 1
        """,
        params,
    )
    row = await result.fetchone()
    if row is None:
        return False, False
    return True, not row["ready"]


async def _job_state(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
    last_run_at: str | None,
) -> tuple[JobState | None, str | None]:
    """Map job rows onto the freshness ``job`` states.

    A dead-lettered derive job blocks the lane until a later successful or
    noop run supersedes it, even when a newer active job already exists.
    """

    result = await conn.execute(
        """
        select dead_at,
               last_error_kind,
               (locked_by is not null and lease_until > now()) as leased,
               run_after <= now() as due
        from job
        where workspace = %s and kind = 'derive' and derivation = %s and entity = %s
          and done_at is null
        order by created_at desc, id
        """,
        (workspace, derivation, entity),
    )
    rows = await result.fetchall()
    last_run = _parse_run_time(last_run_at)
    for row in rows:
        if row["dead_at"] is not None:
            if last_run is not None and last_run >= row["dead_at"]:
                continue
            return "dead", row["last_error_kind"]
    for row in rows:
        if row["dead_at"] is not None:
            continue
        if row["leased"]:
            return "running", None
        return ("enqueued", None) if row["due"] else ("queued", None)
    return None, None


async def compute_freshness(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    catalog: DefinitionCatalog,
) -> tuple[DerivationFreshness, ...]:
    """Compute one freshness entry per read-triggered derivation."""

    entries: list[DerivationFreshness] = []
    for definition in read_triggered_derivations(catalog):
        watermark, last_run_at = await _watermark(
            conn,
            workspace=workspace,
            entity=entity,
            derivation=definition.name,
        )
        dirty, pending_unready = await _first_pending_input(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            watermark=watermark,
        )
        job, error_kind = await _job_state(
            conn,
            workspace=workspace,
            entity=entity,
            derivation=definition.name,
            last_run_at=last_run_at,
        )
        entries.append(
            DerivationFreshness(
                derivation=definition.name,
                last_run_at=last_run_at,
                watermark=watermark,
                dirty=dirty,
                pending_unready=pending_unready,
                job=job,
                error_kind=error_kind,
            )
        )
    return tuple(entries)


async def request_revalidation(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    catalog: DefinitionCatalog | None = None,
    freshness: tuple[DerivationFreshness, ...],
    max_staleness: float | None,
) -> tuple[DerivationFreshness, ...]:
    """Coalesce stale read-trigger requests into derive jobs.

    The current document is assembled before this function is called, so the
    request remains a stale-while-revalidate read.  Only the durable mailbox is
    changed; no provider or derivation work runs on the request path.
    """

    from memseek.triggers import _cooldown_due, enqueue_derive_tx

    if catalog is None:
        return freshness

    now = datetime.now(UTC)
    updated: list[DerivationFreshness] = []
    for entry in freshness:
        stale = False
        if max_staleness is not None:
            completed = _parse_run_time(entry.last_run_at)
            stale = completed is None or (now - completed).total_seconds() >= max_staleness
        should_enqueue = entry.dirty or stale
        if not should_enqueue:
            updated.append(entry)
            continue
        triggers = tuple(
            trigger
            for trigger in catalog.triggers.values()
            if trigger.processor == entry.derivation and trigger.read
        )
        if not triggers:
            updated.append(entry)
            continue
        # One derive mailbox serves all read triggers for the processor.  Keep
        # each reason so a later successful run can re-evaluate any stimulus
        # that was still cooling down.
        coalesced = False
        for trigger in sorted(triggers, key=lambda value: value.name):
            due = await _cooldown_due(
                conn,
                workspace=workspace,
                entity=entity,
                trigger=trigger,
            )
            _job_id, was_coalesced, _actual_due = await enqueue_derive_tx(
                conn,
                workspace=workspace,
                derivation=entry.derivation,
                entity=entity,
                reason=f"trigger:{trigger.name}:read",
                run_after=due,
            )
            coalesced = coalesced or was_coalesced
        if entry.job == "running":
            state = "running"
        else:
            state = "queued" if coalesced or entry.job == "queued" else "enqueued"
        updated.append(replace(entry, job=state, error_kind=None))
    return tuple(updated)


__all__ = [
    "DerivationFreshness",
    "compute_freshness",
    "read_triggered_derivations",
    "request_revalidation",
]
