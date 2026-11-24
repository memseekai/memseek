"""M5 trigger evaluation, coalescing, and checkpointed cron scheduling.

PostgreSQL remains the source of trigger truth.  Ready-transition evaluation is
performed inside the caller's mutation transaction, so a trigger can never see
an unready row or enqueue work for a record whose readiness transaction rolls
back.  Jobs are coalescing mailboxes: reason keys are monotonic booleans, each
reason keeps its own deadline, and the earliest one is the job's ``run_after``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, LiteralString, cast
from uuid import UUID

from croniter import croniter
from psycopg.types.json import Jsonb

from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.definitions.models import DeclaredField
from memseek.derive.schema import (
    PipelineDefinition,
    RecordScope,
    RecordSource,
    StandaloneTrigger,
    ViewSource,
    WriteCondition,
)
from memseek.locks import advisory_lock_key
from memseek.logging import log_event
from memseek.search.scope import field_value_expression

LOGGER = logging.getLogger(__name__)

type TriggerCondition = Literal[
    "write",
    "threshold",
    "read",
    "cron",
    "quiet",
    "at",
    "changed",
    "census",
    "lifecycle",
    "retraction",
]

TRIGGER_REASON_CONDITIONS = frozenset(
    {
        "write",
        "threshold",
        "accumulator",
        "read",
        "cron",
        "quiet",
        "at",
        "changed",
        "census",
        "lifecycle",
        "retraction",
    }
)

_TOMBSTONE_SQL = "coalesce((row.content->>'tombstone')::boolean, false)"


def _scope_clauses(
    scope: RecordScope,
    *,
    workspace: str,
    entity: str,
    watermark: int,
) -> tuple[list[str], list[Any]]:
    clauses = [
        "row.workspace = %s",
        "row.entity = %s",
        "row.seq > %s",
        "row.enriched_at is not null",
    ]
    params: list[Any] = [workspace, entity, watermark]
    terms: list[str] = []
    for name, versions in scope.collection_versions.items():
        terms.append("(row.collection = %s and row.collection_version = any(%s::int[]))")
        params.extend([name, list(versions)])
    unpinned = [name for name in scope.collections if name not in scope.collection_versions]
    if unpinned:
        terms.append("row.collection = any(%s::text[])")
        params.append(unpinned)
    clauses.append("(" + " or ".join(terms) + ")")
    if scope.types:
        clauses.append("row.type = any(%s::text[])")
        params.append(list(scope.types))
    clauses.append("row.status = any(%s::text[])")
    params.append(list(scope.statuses))
    keyed = getattr(scope, "keyed", "any")
    if keyed is True:
        clauses.append("row.key is not null")
    elif keyed is False:
        clauses.append("row.key is null")
    return clauses, params


@dataclass(frozen=True, slots=True)
class _RunState:
    """What the latest successful run left behind for one processor and entity."""

    watermark: int
    last_success: datetime | None


async def _run_state(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> _RunState:
    result = await conn.execute(
        """
        select (content->>'high_seq')::bigint as watermark,
               content->>'completed_at' as completed_at
        from record
        where workspace = %s and entity = %s
          and collection = '_system' and type = 'run'
          and content->>'operation' = 'derive'
          and content->>'processor' = %s
          and content->>'status' in ('ok', 'noop')
        order by seq desc
        limit 1
        """,
        (workspace, entity, derivation),
    )
    row = await result.fetchone()
    if row is None:
        return _RunState(watermark=0, last_success=None)
    return _RunState(
        watermark=int(row["watermark"] or 0),
        last_success=_parse_completed_at(row["completed_at"]),
    )


def _parse_completed_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


async def _watermark(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> int:
    state = await _run_state(conn, workspace=workspace, entity=entity, derivation=derivation)
    return state.watermark


def _field_versions(
    catalog: DefinitionCatalog,
    scope: RecordScope,
    name: str,
) -> dict[tuple[str, int], DeclaredField]:
    versions: dict[tuple[str, int], DeclaredField] = {}
    for key, collection in catalog.collections.items():
        collection_name, version = key
        if collection_name not in scope.collections:
            continue
        allowed = scope.collection_versions.get(collection_name)
        if allowed is not None and version not in allowed:
            continue
        field = collection.fields.get(name)
        if field is not None:
            versions[key] = field
    return versions


def _json_array_expression(
    versions: Mapping[tuple[str, int], DeclaredField],
) -> tuple[str, list[Any]]:
    arms: list[str] = []
    params: list[Any] = []
    for (collection, version), field in sorted(versions.items()):
        root, *parts = field.path.split(".")
        column = "row.content" if root == "content" else "row.annotations"
        arms.append(
            f"when row.collection = %s and row.collection_version = %s then {column} #> %s::text[]"
        )
        params.extend([collection, version, parts])
    return "(case " + " ".join(arms) + " else null end)", params


def _predicate_clauses(
    where: Mapping[str, Mapping[str, Any]],
    *,
    catalog: DefinitionCatalog,
    scope: RecordScope,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for name, predicates in where.items():
        versions = _field_versions(catalog, scope, name)
        if not versions:
            clauses.append("false")
            continue
        declaration = next(iter(versions.values()))
        value_sql, value_params = field_value_expression(versions)
        array_sql, array_params = _json_array_expression(versions)
        for operator, operand in predicates.items():
            if operator == "exists":
                clauses.append(f"{value_sql} is {'not ' if operand else ''}null")
                params.extend(value_params)
            elif operator == "eq" and declaration.is_array:
                clauses.append(f"{array_sql} = %s::jsonb")
                params.extend([*array_params, Jsonb(operand)])
            elif operator == "contains_any":
                array_cast = {
                    "number": "numeric",
                    "integer": "numeric",
                    "datetime": "timestamptz",
                    "boolean": "boolean",
                    "string": "text",
                }[declaration.scalar_type]
                clauses.append(
                    "exists (select 1 from jsonb_array_elements_text("
                    f"{array_sql}) as item(value) where item.value::{array_cast} = "
                    f"any(%s::{array_cast}[]))"
                )
                params.extend([*array_params, list(operand)])
            elif operator == "contains_all":
                array_cast = {
                    "number": "numeric",
                    "integer": "numeric",
                    "datetime": "timestamptz",
                    "boolean": "boolean",
                    "string": "text",
                }[declaration.scalar_type]
                clauses.append(
                    f"not exists (select 1 from unnest(%s::{array_cast}[]) as wanted(value) "
                    "where not exists (select 1 from jsonb_array_elements_text("
                    f"{array_sql}) as item(value) where item.value::{array_cast} = wanted.value))"
                )
                params.extend([list(operand), *array_params])
            else:
                pushed = _scalar_predicate(value_sql, value_params, operator, operand, declaration)
                if pushed is None:
                    clauses.append("false")
                else:
                    expression, expression_params = pushed
                    clauses.append(expression)
                    params.extend(expression_params)
    return clauses, params


def _scalar_predicate(
    value_sql: str,
    value_params: Sequence[Any],
    operator: str,
    operand: Any,
    declaration: DeclaredField,
) -> tuple[str, list[Any]] | None:
    if declaration.is_array:
        return None
    suffix = {
        "number": "::numeric",
        "integer": "::numeric",
        "datetime": "::timestamptz",
        "boolean": "::boolean",
        "string": "",
    }[declaration.scalar_type]
    if operator == "eq":
        return f"{value_sql} = %s{suffix}", [*value_params, operand]
    if operator == "in":
        cast_type = {
            "number": "numeric[]",
            "integer": "numeric[]",
            "datetime": "timestamptz[]",
            "boolean": "boolean[]",
            "string": "text[]",
        }[declaration.scalar_type]
        return f"{value_sql} = any(%s::{cast_type})", [*value_params, list(operand)]
    if operator in {"gt", "gte", "lt", "lte"}:
        comparison = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
        return f"{value_sql} {comparison} %s{suffix}", [*value_params, operand]
    return None


async def _db_now(conn: DatabaseConnection) -> datetime:
    result = await conn.execute("select clock_timestamp() as now")
    row = await result.fetchone()
    if row is None or not isinstance(row["now"], datetime):
        raise RuntimeError("database clock returned no timestamp")
    return row["now"].astimezone(UTC)


async def enqueue_derive_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    derivation: str,
    entity: str,
    reason: str,
    run_after: datetime | None = None,
    coalesce: Literal["earliest", "extend"] = "earliest",
    max_wait_s: int | None = None,
) -> tuple[UUID, bool, datetime]:
    """Insert/coalesce one derive mailbox entry in the caller's transaction.

    ``coalesce`` decides how this reason's own deadline merges with an
    earlier stimulus of the same reason: ``earliest`` keeps the sooner time,
    while ``extend`` pushes it later, the settle semantics used by quiet and
    debounced stimuli. Other reasons' deadlines are untouched. ``max_wait_s``
    stops an ``extend`` reason from moving past its first sighting plus that
    many seconds, so a stream that never pauses still runs.
    """

    parts = reason.split(":")
    if (
        len(parts) != 3
        or parts[0] != "trigger"
        or not parts[1]
        or parts[2] not in TRIGGER_REASON_CONDITIONS
    ):
        raise ValueError("trigger reason must use the trigger:<name>:<condition> form")
    when = run_after if run_after is not None else await _db_now(conn)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("run_after must include a timezone")
    return await coalesce_derive_stimulus_tx(
        conn,
        workspace=workspace,
        derivation=derivation,
        entity=entity,
        reason=reason,
        due=when,
        coalesce=coalesce,
        max_wait_s=max_wait_s,
    )


async def coalesce_derive_stimulus_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    derivation: str,
    entity: str,
    reason: str,
    due: datetime,
    coalesce: Literal["earliest", "extend"],
    max_wait_s: int | None = None,
) -> tuple[UUID, bool, datetime]:
    """Merge one reason into the entity's active derive job (spec 11.4).

    ``job.stimuli`` holds each reason's own ``due`` and first-seen ``since``.
    An ``earliest`` reason keeps its sooner due; an ``extend`` reason pushes
    only its own, never past ``since + max_wait_s`` and never earlier than it
    already was. ``run_after`` follows the earliest due, so a settle window
    never delays a reason that is already allowed to run. Extending an
    existing reason keeps a later ``run_after`` the queue set itself (retry
    backoff, a not-ready release). A job written before ``stimuli`` existed is
    read as if each payload reason were due at its ``run_after``.
    """

    result = await conn.execute(
        """
        insert into job as job (workspace, kind, derivation, entity, run_after, payload, stimuli)
        values (
          %(workspace)s, 'derive', %(derivation)s, %(entity)s, %(due)s,
          jsonb_build_object(%(reason)s::text, true),
          jsonb_build_object(
            %(reason)s::text,
            jsonb_build_object('due', %(due)s::timestamptz, 'since', clock_timestamp())
          )
        )
        on conflict (workspace, derivation, entity)
          where kind = 'derive' and done_at is null and dead_at is null
        do update set
          payload = job.payload || excluded.payload,
          (stimuli, run_after) = (
            select merged.stimuli,
                   case
                     when %(extend)s and prior.due is not null
                       then greatest(
                         job.run_after,
                         (select min((value->>'due')::timestamptz)
                          from jsonb_each(merged.stimuli))
                       )
                     else least(job.run_after, excluded.run_after)
                   end
            from (
              select case when job.stimuli = '{}'::jsonb then (
                       select coalesce(jsonb_object_agg(
                         reason_key,
                         jsonb_build_object('due', job.run_after, 'since', job.created_at)
                       ), '{}'::jsonb)
                       from jsonb_object_keys(job.payload) as reason_key
                     ) else job.stimuli end as stimuli
            ) existing
            cross join lateral (
              select (existing.stimuli #>> array[%(reason)s::text, 'due'])::timestamptz as due,
                     existing.stimuli #> array[%(reason)s::text, 'since'] as since
            ) prior
            cross join lateral (
              select existing.stimuli || jsonb_build_object(
                %(reason)s::text,
                jsonb_build_object(
                  'due',
                  case
                    when prior.due is null then excluded.run_after
                    when %(extend)s then greatest(
                      prior.due,
                      least(
                        excluded.run_after,
                        (prior.since #>> '{}')::timestamptz
                          + make_interval(secs => %(max_wait_s)s::int)
                      )
                    )
                    else least(prior.due, excluded.run_after)
                  end,
                  'since',
                  coalesce(prior.since, excluded.stimuli #> array[%(reason)s::text, 'since'])
                )
              ) as stimuli
            ) merged
          )
        returning id, run_after, (xmax = 0) as inserted
        """,
        {
            "workspace": workspace,
            "derivation": derivation,
            "entity": entity,
            "due": due,
            "reason": reason,
            "extend": coalesce == "extend",
            "max_wait_s": max_wait_s,
        },
    )
    row = await result.fetchone()
    if row is None:
        raise RuntimeError("derive enqueue returned no row")
    return cast(UUID, row["id"]), not bool(row["inserted"]), cast(datetime, row["run_after"])


def _cooldown_until(now: datetime, cooldown_s: int, last_success: datetime | None) -> datetime:
    if cooldown_s <= 0 or last_success is None:
        return now
    return max(now, last_success + timedelta(seconds=cooldown_s))


async def _cooldown_due(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    trigger: StandaloneTrigger,
    now: datetime | None = None,
) -> datetime:
    current = now if now is not None else await _db_now(conn)
    if trigger.cooldown_s <= 0:
        return current
    state = await _run_state(
        conn,
        workspace=workspace,
        entity=entity,
        derivation=trigger.processor,
    )
    return _cooldown_until(current, trigger.cooldown_s, state.last_success)


async def _scope_matches(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    scope: WriteCondition,
    catalog: DefinitionCatalog,
    watermark: int,
    processor: str,
    extra_clauses: Sequence[str] = (),
) -> bool:
    """True when a ready record matching ``scope`` exists above ``watermark``."""

    clauses, params = _scope_clauses(
        scope,
        workspace=workspace,
        entity=entity,
        watermark=watermark,
    )
    clauses.extend(extra_clauses)
    if scope.ignore_own_outputs:
        clauses.append(
            """
            not exists (
              select 1
              from record producer_run
              where producer_run.workspace = row.workspace
                and producer_run.id = row.run_id
                and producer_run.collection = '_system'
                and producer_run.content->>'processor' = %s
            )
            """
        )
        params.append(processor)
    where_clauses, where_params = _predicate_clauses(
        scope.where,
        catalog=catalog,
        scope=scope,
    )
    clauses.extend(where_clauses)
    params.extend(where_params)
    result = await conn.execute(
        cast(
            LiteralString,
            f"select exists(select 1 from record row where {' and '.join(clauses)}) as matched",
        ),
        params,
    )
    row = await result.fetchone()
    return bool(row and row["matched"])


async def _driver_dirty(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    watermark: int,
) -> bool:
    clauses, params = _scope_clauses(
        definition.driver,
        workspace=workspace,
        entity=entity,
        watermark=watermark,
    )
    result = await conn.execute(
        cast(
            LiteralString,
            f"select exists(select 1 from record row where {' and '.join(clauses)}) as matched",
        ),
        params,
    )
    row = await result.fetchone()
    return bool(row and row["matched"])


async def _accumulator_matches(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    trigger: StandaloneTrigger,
    watermark: int,
) -> bool:
    assert trigger.accumulator is not None
    clauses, params = _scope_clauses(
        definition.driver,
        workspace=workspace,
        entity=entity,
        watermark=watermark,
    )
    metric = trigger.accumulator.metric
    if metric == "count":
        aggregate = "count(*)"
    elif isinstance(metric, str):
        aggregate = "coalesce(sum(coalesce((row.scores->>%s)::double precision, 0)), 0)"
        params.insert(0, metric)
    elif metric.aggregate == "count":
        aggregate = "count(*)"
    else:
        if metric.scorer is not None:
            expression = "((row.scores->>%s)::double precision)"
            expression_params: list[Any] = [metric.scorer]
        else:
            assert metric.annotation is not None
            assert metric.path is not None
            expression = "(row.annotations #>> %s::text[])"
            expression_params = [[metric.annotation, *metric.path.split(".")]]
        if metric.aggregate == "sum":
            aggregate = f"coalesce(sum(coalesce({expression}::double precision, 0)), 0)"
        elif metric.aggregate == "distinct_count":
            aggregate = f"count(distinct {expression})"
        else:
            aggregate = f"{metric.aggregate}({expression}::double precision)"
        params = [*expression_params, *params]
    result = await conn.execute(
        cast(
            LiteralString,
            f"select count(*) as matched_rows, {aggregate} as metric "
            f"from record row where {' and '.join(clauses)}",
        ),
        params,
    )
    row = await result.fetchone()
    if row is None or int(row["matched_rows"] or 0) == 0 or row["metric"] is None:
        return False
    value = float(row["metric"])
    if trigger.accumulator.comparison == "lte":
        return value <= trigger.accumulator.threshold
    return value >= trigger.accumulator.threshold


async def _changed_matches(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    trigger: StandaloneTrigger,
    catalog: DefinitionCatalog,
    watermark: int,
) -> bool:
    """True when a keyed head above the watermark was added, changed, or removed."""

    changed = trigger.changed
    assert changed is not None
    clauses, params = _scope_clauses(
        changed,
        workspace=workspace,
        entity=entity,
        watermark=watermark,
    )
    clauses.append("row.key is not null")
    if changed.keys:
        clauses.append("row.key = any(%s::text[])")
        params.append(list(changed.keys))
    where_clauses, where_params = _predicate_clauses(
        changed.where,
        catalog=catalog,
        scope=changed,
    )
    clauses.extend(where_clauses)
    params.extend(where_params)
    prev_tombstone = "coalesce((prev.content->>'tombstone')::boolean, false)"
    transition = f"""
        (case
          when {_TOMBSTONE_SQL} then
            case when prev.content is null or {prev_tombstone} then 'unchanged'
                 else 'removed' end
          when prev.content is null or {prev_tombstone} then 'added'
          when prev.content is distinct from row.content then 'changed'
          else 'unchanged'
        end)
    """
    query = f"""
        select exists(
          select 1
          from record row
          left join lateral (
            select prev.content
            from record prev
            where prev.workspace = row.workspace and prev.entity = row.entity
              and prev.collection = row.collection and prev.key = row.key
              and prev.seq < row.seq and prev.status = any(%s::text[])
            order by prev.seq desc
            limit 1
          ) prev on true
          where {" and ".join(clauses)}
            and {transition} = any(%s::text[])
        ) as matched
    """
    result = await conn.execute(
        cast(LiteralString, query),
        [list(changed.statuses), *params, list(changed.transitions)],
    )
    row = await result.fetchone()
    return bool(row and row["matched"])


async def _census_matches(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    trigger: StandaloneTrigger,
    catalog: DefinitionCatalog,
    watermark: int,
) -> bool:
    """True when new driver data arrived and the current census meets the floor.

    The dirtiness guard keys the census to fresh driving-source data, so a
    standing census can never re-enqueue itself after its own run.
    """

    census = trigger.census
    assert census is not None
    if not await _driver_dirty(
        conn,
        workspace=workspace,
        entity=entity,
        definition=definition,
        watermark=watermark,
    ):
        return False
    clauses, params = _scope_clauses(
        census,
        workspace=workspace,
        entity=entity,
        watermark=0,
    )
    where_clauses, where_params = _predicate_clauses(
        census.where,
        catalog=catalog,
        scope=census,
    )
    outer = [f"{_TOMBSTONE_SQL} = false", *where_clauses]
    query = f"""
        select count(*) as census from (
          select distinct on (row.collection, coalesce(row.key, row.seq::text)) row.*
          from record row
          where {" and ".join(clauses)}
          order by row.collection, coalesce(row.key, row.seq::text), row.seq desc
        ) row
        where {" and ".join(outer)}
    """
    result = await conn.execute(cast(LiteralString, query), [*params, *where_params])
    row = await result.fetchone()
    return bool(row and int(row["census"] or 0) >= census.threshold)


async def _lifecycle_matches(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    definition: PipelineDefinition,
    trigger: StandaloneTrigger,
    watermark: int,
) -> bool:
    lifecycle = trigger.lifecycle
    assert lifecycle is not None
    if not await _driver_dirty(
        conn,
        workspace=workspace,
        entity=entity,
        definition=definition,
        watermark=watermark,
    ):
        return False
    if lifecycle.first_record and watermark == 0:
        return True
    if lifecycle.total_records is None:
        return False
    clauses, params = _scope_clauses(
        definition.driver,
        workspace=workspace,
        entity=entity,
        watermark=0,
    )
    result = await conn.execute(
        cast(
            LiteralString,
            f"select count(*) as total from record row where {' and '.join(clauses)}",
        ),
        params,
    )
    row = await result.fetchone()
    return bool(row and int(row["total"] or 0) >= lifecycle.total_records)


async def _at_deadline(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    trigger: StandaloneTrigger,
    catalog: DefinitionCatalog,
    last_success: datetime | None,
) -> datetime | None:
    """The earliest unhandled record deadline, or None when nothing is pending.

    A deadline is handled once a successful run completes at or after it, so
    consecutive runs walk forward through future-dated records without a
    watermark dependency.
    """

    at = trigger.at
    assert at is not None
    versions = _field_versions(catalog, at, at.field)
    if not versions:
        return None
    value_sql, value_params = field_value_expression(versions)
    clauses, params = _scope_clauses(
        at,
        workspace=workspace,
        entity=entity,
        watermark=0,
    )
    where_clauses, where_params = _predicate_clauses(
        at.where,
        catalog=catalog,
        scope=at,
    )
    clauses.extend(where_clauses)
    params.extend(where_params)
    floor = last_success or datetime(1970, 1, 1, tzinfo=UTC)
    query = f"""
        select min(candidate.deadline) as deadline from (
          select ({value_sql} + make_interval(secs => %s)) as deadline
          from record row
          where {" and ".join(clauses)} and {value_sql} is not null
        ) candidate
        where candidate.deadline > %s
    """
    result = await conn.execute(
        cast(LiteralString, query),
        [*value_params, at.offset_s, *params, *value_params, floor],
    )
    row = await result.fetchone()
    deadline = row["deadline"] if row is not None else None
    if not isinstance(deadline, datetime):
        return None
    return deadline.astimezone(UTC)


def _trigger_collections(
    trigger: StandaloneTrigger, definition: PipelineDefinition
) -> set[str] | None:
    """Collections whose readiness can change what this trigger enqueues.

    That is every scope the trigger observes plus everything its processor
    reads: a source row turning ready can make a released not-ready job
    runnable, and re-coalescing its pending reason is what pulls it back in.
    ``None`` means a view source hides what is read, so nothing is skipped.
    """

    names = {
        collection for scope in trigger.observed_scopes.values() for collection in scope.collections
    }
    for source in definition.sources.values():
        if isinstance(source, ViewSource):
            return None
        if isinstance(source, RecordSource):
            names.add(source.collection)
        else:
            names.update(source.collections)
    return names


async def evaluate_entity_triggers_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    catalog: DefinitionCatalog,
    collections: Collection[str] | None = None,
) -> int:
    """Evaluate every ready-transition trigger condition for one entity.

    Arrival conditions (write, threshold, changed, retraction, census,
    lifecycle) enqueue at the cooldown due time, pushed later by
    ``debounce_s``. Quiet enqueues a settle deadline that extends while
    matching records keep arriving. At enqueues the earliest unhandled record
    deadline.

    ``collections`` names the collections whose records just became ready.
    When given, triggers whose scopes cannot see any of them are skipped;
    ``None`` evaluates every trigger, which a watermark advance needs.
    """

    fired = 0
    run_states: dict[str, _RunState] = {}
    for name in sorted(catalog.triggers):
        trigger = catalog.triggers[name]
        definition = catalog.derivations.get(trigger.processor)
        if definition is None:
            continue
        if collections is not None:
            relevant = _trigger_collections(trigger, definition)
            if relevant is not None and relevant.isdisjoint(collections):
                continue
        state = run_states.get(definition.name)
        if state is None:
            state = await _run_state(
                conn, workspace=workspace, entity=entity, derivation=definition.name
            )
            run_states[definition.name] = state
        watermark = state.watermark
        arrivals: list[TriggerCondition] = []
        if trigger.write is not None and await _scope_matches(
            conn,
            workspace=workspace,
            entity=entity,
            scope=trigger.write,
            catalog=catalog,
            watermark=watermark,
            processor=trigger.processor,
        ):
            arrivals.append("write")
        if trigger.accumulator is not None and await _accumulator_matches(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            trigger=trigger,
            watermark=watermark,
        ):
            arrivals.append("threshold")
        if trigger.changed is not None and await _changed_matches(
            conn,
            workspace=workspace,
            entity=entity,
            trigger=trigger,
            catalog=catalog,
            watermark=watermark,
        ):
            arrivals.append("changed")
        if trigger.retraction is not None and await _scope_matches(
            conn,
            workspace=workspace,
            entity=entity,
            scope=trigger.retraction,
            catalog=catalog,
            watermark=watermark,
            processor=trigger.processor,
            extra_clauses=("row.key is not null", f"{_TOMBSTONE_SQL} = true"),
        ):
            arrivals.append("retraction")
        if trigger.census is not None and await _census_matches(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            trigger=trigger,
            catalog=catalog,
            watermark=watermark,
        ):
            arrivals.append("census")
        if trigger.lifecycle is not None and await _lifecycle_matches(
            conn,
            workspace=workspace,
            entity=entity,
            definition=definition,
            trigger=trigger,
            watermark=watermark,
        ):
            arrivals.append("lifecycle")
        quiet_matched = trigger.quiet is not None and await _scope_matches(
            conn,
            workspace=workspace,
            entity=entity,
            scope=trigger.quiet,
            catalog=catalog,
            watermark=watermark,
            processor=trigger.processor,
        )
        deadline = (
            await _at_deadline(
                conn,
                workspace=workspace,
                entity=entity,
                trigger=trigger,
                catalog=catalog,
                last_success=state.last_success,
            )
            if trigger.at is not None
            else None
        )
        if not arrivals and not quiet_matched and deadline is None:
            continue
        # A fresh clock read per firing trigger, not one per call: triggers fire
        # in name order, and the distinct run_after values make their jobs claim
        # in that order instead of tying and claiming in random id order.
        now = await _db_now(conn)
        due = _cooldown_until(now, trigger.cooldown_s, state.last_success)
        for condition in arrivals:
            run_after = due
            coalesce: Literal["earliest", "extend"] = "earliest"
            if trigger.debounce_s > 0:
                run_after = max(due, now + timedelta(seconds=trigger.debounce_s))
                coalesce = "extend"
            await enqueue_derive_tx(
                conn,
                workspace=workspace,
                derivation=definition.name,
                entity=entity,
                reason=f"trigger:{name}:{condition}",
                run_after=run_after,
                coalesce=coalesce,
                max_wait_s=trigger.max_wait_s,
            )
            fired += 1
        if quiet_matched:
            assert trigger.quiet is not None
            await enqueue_derive_tx(
                conn,
                workspace=workspace,
                derivation=definition.name,
                entity=entity,
                reason=f"trigger:{name}:quiet",
                run_after=max(due, now + timedelta(seconds=trigger.quiet.after_s)),
                coalesce="extend",
                max_wait_s=trigger.max_wait_s,
            )
            fired += 1
        if deadline is not None:
            await enqueue_derive_tx(
                conn,
                workspace=workspace,
                derivation=definition.name,
                entity=entity,
                reason=f"trigger:{name}:at",
                run_after=max(due, deadline),
            )
            fired += 1
    return fired


async def evaluate_ready_triggers_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entities: Sequence[str],
    catalog: DefinitionCatalog,
    collections: Mapping[str, Collection[str]] | None = None,
) -> int:
    """Evaluate ready-transition trigger conditions for changed entities.

    ``collections`` maps an entity to the collections that changed for it;
    see ``evaluate_entity_triggers_tx``.
    """

    total = 0
    for entity in sorted(set(entities)):
        total += await evaluate_entity_triggers_tx(
            conn,
            workspace=workspace,
            entity=entity,
            catalog=catalog,
            collections=None if collections is None else collections.get(entity, ()),
        )
    return total


_CRON_SCHEDULER_LOCK = advisory_lock_key("cron-scheduler")


@dataclass(frozen=True, slots=True)
class _CronSchedule:
    """One cron-driven job source and the checkpoint row that paces it."""

    checkpoint: str
    expr: str
    kind: Literal["cron_scan", "retention_purge"]
    derivation: str | None
    dedupe_prefix: str
    payload: Mapping[str, Any]


def _cron_schedules(catalog: DefinitionCatalog) -> list[_CronSchedule]:
    schedules = [
        _CronSchedule(
            checkpoint=trigger.name,
            expr=trigger.cron.expr,
            kind="cron_scan",
            derivation=trigger.processor,
            dedupe_prefix=f"cron:{trigger.name}:{trigger.processor}",
            payload={"trigger": trigger.name, "entities": trigger.cron.entities, "cursor": None},
        )
        for trigger in sorted(catalog.triggers.values(), key=lambda value: value.name)
        if trigger.cron is not None
    ]
    for _key, package in sorted(catalog.packages.items()):
        for retention in package.retentions:
            name = f"retention:{package.name}@{package.version}:{retention.name}"
            schedules.append(
                _CronSchedule(
                    checkpoint=name,
                    expr=retention.cron,
                    kind="retention_purge",
                    derivation=None,
                    dedupe_prefix=name,
                    payload={
                        "package_name": package.name,
                        "package_version": package.version,
                        "retention": retention.name,
                    },
                )
            )
    return schedules


def _latest_bucket(expr: str, current: datetime) -> datetime:
    minute = current.replace(second=0, microsecond=0)
    if croniter.match(expr, minute):
        return minute.astimezone(UTC)
    return croniter(expr, current).get_prev(datetime).astimezone(UTC)


async def schedule_cron_jobs(
    pool: DatabasePool,
    *,
    catalog: DefinitionCatalog,
    catalog_for_workspace: Callable[[str], Awaitable[DefinitionCatalog]] | None = None,
    now: datetime | None = None,
) -> int:
    """Insert the latest due cron-scan and retention job for every workspace.

    ``cron_checkpoint`` holds the last scheduled bucket per schedule.  A pass
    schedules only the latest bucket, and only when it is newer than the
    checkpoint: missed ticks collapse into it, because a cron reason is a
    boolean on a coalescing mailbox and replaying N scans adds nothing.  The
    job and its checkpoint commit together, one transaction per workspace.
    A session advisory lock keeps concurrent workers from sweeping at once;
    a worker that loses it skips the pass.  ``now`` defaults to the database
    clock.
    """

    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must include a timezone")
    if (
        not catalog.triggers
        and not any(package.retentions for package in catalog.packages.values())
        and catalog_for_workspace is None
    ):
        return 0
    async with pool.connection() as conn:
        result = await conn.execute(
            "select pg_try_advisory_lock(%s) as acquired, clock_timestamp() as now",
            (_CRON_SCHEDULER_LOCK,),
        )
        row = await result.fetchone()
        await conn.commit()
        if row is None or not row["acquired"]:
            return 0
        try:
            current = now or row["now"]
            return await _schedule_workspaces(
                conn,
                catalog=catalog,
                catalog_for_workspace=catalog_for_workspace,
                current=current,
            )
        finally:
            await conn.execute("select pg_advisory_unlock(%s)", (_CRON_SCHEDULER_LOCK,))
            await conn.commit()


async def _schedule_workspaces(
    conn: DatabaseConnection,
    *,
    catalog: DefinitionCatalog,
    catalog_for_workspace: Callable[[str], Awaitable[DefinitionCatalog]] | None,
    current: datetime,
) -> int:
    result = await conn.execute("select id from workspace order by id")
    workspaces = [str(row["id"]) for row in await result.fetchall()]
    await conn.commit()
    latest: dict[str, datetime] = {}
    inserted = 0
    for workspace in workspaces:
        if catalog_for_workspace is None:
            selected_catalog = catalog
        else:
            try:
                selected_catalog = await catalog_for_workspace(workspace)
            except Exception as exc:
                # Scheduling sweeps every workspace on the deployment, so one
                # whose stored overlay no longer resolves must cost only its
                # own cron work.
                log_event(
                    LOGGER,
                    "error",
                    "cron.workspace_skipped",
                    workspace=workspace,
                    exception_type=type(exc).__name__,
                )
                continue
        schedules = _cron_schedules(selected_catalog)
        if not schedules:
            continue
        async with conn.transaction():
            for schedule in schedules:
                if schedule.expr not in latest:
                    latest[schedule.expr] = _latest_bucket(schedule.expr, current)
                due = latest[schedule.expr]
                stamp = due.isoformat().replace("+00:00", "Z")
                result = await conn.execute(
                    """
                    with advanced as (
                      insert into cron_checkpoint (workspace, trigger, scheduled_at)
                      values (%(workspace)s, %(checkpoint)s, %(due)s)
                      on conflict (workspace, trigger) do update
                        set scheduled_at = excluded.scheduled_at
                        where cron_checkpoint.scheduled_at < excluded.scheduled_at
                      returning 1
                    )
                    insert into job (workspace, kind, derivation, dedupe_key, payload)
                    select %(workspace)s, %(kind)s, %(derivation)s, %(dedupe)s, %(payload)s
                    from advanced
                    on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
                    returning id
                    """,
                    {
                        "workspace": workspace,
                        "checkpoint": schedule.checkpoint,
                        "due": due,
                        "kind": schedule.kind,
                        "derivation": schedule.derivation,
                        "dedupe": f"{schedule.dedupe_prefix}:{stamp}",
                        "payload": Jsonb({**schedule.payload, "scheduled_at": stamp}),
                    },
                )
                if await result.fetchone() is not None:
                    inserted += 1
    return inserted


__all__ = [
    "enqueue_derive_tx",
    "evaluate_entity_triggers_tx",
    "evaluate_ready_triggers_tx",
    "schedule_cron_jobs",
]
