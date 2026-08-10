"""M5 trigger evaluation, coalescing, and persisted cron scan primitives.

PostgreSQL remains the source of trigger truth.  Ready-transition evaluation is
performed inside the caller's mutation transaction, so a trigger can never see
an unready row or enqueue work for a record whose readiness transaction rolls
back.  Jobs are coalescing mailboxes: reason keys are monotonic booleans and
the earliest permitted ``run_after`` wins.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    StandaloneTrigger,
    WriteCondition,
)
from memseek.logging import log_event
from memseek.models import ClaimedJob, LeaseLost
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


async def _watermark(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> int:
    result = await conn.execute(
        """
        select coalesce(max((content->>'high_seq')::bigint), 0) as watermark
        from record
        where workspace = %s and entity = %s
          and collection = '_system' and type = 'run'
          and content->>'operation' = 'derive'
          and coalesce(content->>'processor', content->>'derivation') = %s
          and content->>'status' in ('ok', 'noop')
        """,
        (workspace, entity, derivation),
    )
    row = await result.fetchone()
    return int(row["watermark"] or 0) if row is not None else 0


async def _last_success(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    derivation: str,
) -> datetime | None:
    result = await conn.execute(
        """
        select content->>'completed_at' as completed_at
        from record
        where workspace = %s and entity = %s
          and collection = '_system' and type = 'run'
          and content->>'operation' = 'derive'
          and coalesce(content->>'processor', content->>'derivation') = %s
          and content->>'status' in ('ok', 'noop')
        order by (content->>'high_seq')::bigint desc, seq desc
        limit 1
        """,
        (workspace, entity, derivation),
    )
    row = await result.fetchone()
    value = row["completed_at"] if row is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


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
) -> tuple[UUID, bool, datetime]:
    """Insert/coalesce one derive mailbox entry in the caller's transaction.

    ``coalesce`` decides how a conflicting active job's ``run_after`` merges
    with this stimulus: ``earliest`` keeps the sooner time, while ``extend``
    pushes the shared deadline later — the settle semantics used by quiet and
    debounced stimuli.
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
    payload = {reason: True}
    result = await conn.execute(
        """
        insert into job (workspace, kind, derivation, entity, run_after, payload)
        values (%s, 'derive', %s, %s, %s, %s)
        on conflict (workspace, derivation, entity)
          where kind = 'derive' and done_at is null and dead_at is null
        do update set payload = job.payload || excluded.payload,
                      run_after = case
                        when %s then greatest(job.run_after, excluded.run_after)
                        else least(job.run_after, excluded.run_after)
                      end
        returning id, run_after, (xmax = 0) as inserted
        """,
        (workspace, derivation, entity, when, Jsonb(payload), coalesce == "extend"),
    )
    row = await result.fetchone()
    if row is None:
        raise RuntimeError("derive trigger enqueue returned no row")
    return cast(UUID, row["id"]), not bool(row["inserted"]), cast(datetime, row["run_after"])


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
    last = await _last_success(
        conn,
        workspace=workspace,
        entity=entity,
        derivation=trigger.processor,
    )
    if last is None:
        return current
    return max(current, last + timedelta(seconds=trigger.cooldown_s))


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
    catalog: DefinitionCatalog,
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
            versions = _field_versions(catalog, definition.driver, metric.annotation)
            if not versions:
                return False
            expression, expression_params = field_value_expression(versions)
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
    floor = await _last_success(
        conn,
        workspace=workspace,
        entity=entity,
        derivation=trigger.processor,
    ) or datetime(1970, 1, 1, tzinfo=UTC)
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


async def evaluate_entity_triggers_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    catalog: DefinitionCatalog,
) -> int:
    """Evaluate every ready-transition trigger condition for one entity.

    Arrival conditions (write, threshold, changed, retraction, census,
    lifecycle) enqueue at the cooldown due time, pushed later by
    ``debounce_s``. Quiet enqueues a settle deadline that extends while
    matching records keep arriving. At enqueues the earliest unhandled record
    deadline.
    """

    fired = 0
    for name in sorted(catalog.triggers):
        trigger = catalog.triggers[name]
        definition = catalog.derivations.get(trigger.processor)
        if definition is None:
            continue
        watermark = await _watermark(
            conn,
            workspace=workspace,
            entity=entity,
            derivation=definition.name,
        )
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
            catalog=catalog,
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
            )
            if trigger.at is not None
            else None
        )
        if not arrivals and not quiet_matched and deadline is None:
            continue
        now = await _db_now(conn)
        due = await _cooldown_due(
            conn, workspace=workspace, entity=entity, trigger=trigger, now=now
        )
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
) -> int:
    """Evaluate all ready-transition trigger conditions for changed entities."""

    total = 0
    for entity in sorted(set(entities)):
        total += await evaluate_entity_triggers_tx(
            conn,
            workspace=workspace,
            entity=entity,
            catalog=catalog,
        )
    return total


async def schedule_cron_jobs(
    pool: DatabasePool,
    *,
    catalog: DefinitionCatalog,
    catalog_for_workspace: Callable[[str], Awaitable[DefinitionCatalog]] | None = None,
    now: datetime | None = None,
    max_catchup: int = 100,
) -> int:
    """Insert due deduplicated cron-scan jobs for every workspace.

    The last persisted schedule bucket is the restart checkpoint.  A brand-new
    schedule intentionally starts at its latest due bucket; an existing one
    catches up at most ``max_catchup`` buckets per scheduler pass.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if max_catchup <= 0:
        raise ValueError("max_catchup must be positive")
    if (
        not catalog.triggers
        and not any(package.retentions for package in catalog.packages.values())
        and catalog_for_workspace is None
    ):
        return 0
    inserted = 0
    async with pool.connection() as conn, conn.transaction():
        workspaces = await conn.execute("select id from workspace order by id")
        workspace_rows = await workspaces.fetchall()
        for workspace_row in workspace_rows:
            workspace = str(workspace_row["id"])
            if catalog_for_workspace is None:
                selected_catalog = catalog
            else:
                try:
                    selected_catalog = await catalog_for_workspace(workspace)
                except Exception as exc:
                    # Scheduling sweeps every workspace on the deployment, so one
                    # whose stored overlay no longer resolves must cost only its
                    # own cron work.  Aborting here would roll the whole
                    # transaction back and leave every other workspace unscheduled.
                    log_event(
                        LOGGER,
                        "error",
                        "cron.workspace_skipped",
                        workspace=workspace,
                        exception_type=type(exc).__name__,
                    )
                    continue
            cron_triggers = tuple(
                trigger
                for trigger in selected_catalog.triggers.values()
                if trigger.cron is not None
            )
            for trigger in sorted(cron_triggers, key=lambda value: value.name):
                assert trigger.cron is not None
                iterator = croniter(trigger.cron.expr, current)
                minute = current.replace(second=0, microsecond=0)
                latest = (
                    minute
                    if croniter.match(trigger.cron.expr, minute)
                    else iterator.get_prev(datetime)
                ).astimezone(UTC)
                checkpoint_result = await conn.execute(
                    """
                    select max(payload->>'scheduled_at') as scheduled_at
                    from job
                    where workspace = %s and kind = 'cron_scan'
                      and derivation = %s and payload->>'trigger' = %s
                    """,
                    (workspace, trigger.processor, trigger.name),
                )
                checkpoint_row = await checkpoint_result.fetchone()
                checkpoint = checkpoint_row["scheduled_at"] if checkpoint_row else None
                if not isinstance(checkpoint, str):
                    buckets = [latest]
                else:
                    try:
                        previous = datetime.fromisoformat(checkpoint.replace("Z", "+00:00"))
                    except ValueError:
                        previous = latest
                    buckets = []
                    cursor = previous.astimezone(UTC)
                    for _ in range(max_catchup):
                        candidate = croniter(trigger.cron.expr, cursor).get_next(datetime)
                        candidate = candidate.astimezone(UTC)
                        if candidate > latest:
                            break
                        buckets.append(candidate)
                        cursor = candidate
                for due in buckets[:max_catchup]:
                    stamp = due.isoformat().replace("+00:00", "Z")
                    dedupe = f"cron:{trigger.processor}:{stamp}"
                    result = await conn.execute(
                        """
                        insert into job (workspace, kind, derivation, dedupe_key, payload)
                        values (%s, 'cron_scan', %s, %s, %s)
                        on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
                        returning id
                        """,
                        (
                            workspace,
                            trigger.processor,
                            dedupe,
                            Jsonb(
                                {
                                    "trigger": trigger.name,
                                    "scheduled_at": stamp,
                                    "entities": trigger.cron.entities,
                                    "cursor": None,
                                }
                            ),
                        ),
                    )
                    if await result.fetchone() is not None:
                        inserted += 1

            retention_policies = tuple(
                (package, retention)
                for _key, package in sorted(selected_catalog.packages.items())
                for retention in package.retentions
            )
            for package, retention in retention_policies:
                iterator = croniter(retention.cron, current)
                minute = current.replace(second=0, microsecond=0)
                latest = (
                    minute
                    if croniter.match(retention.cron, minute)
                    else iterator.get_prev(datetime)
                ).astimezone(UTC)
                # Retention has no missed-time semantics: an expired page is
                # still eligible at the latest tick. Deliberately avoid
                # catch-up buckets so downtime cannot turn a 25-page batch
                # into a large destructive backlog on one worker pass.
                for due in (latest,):
                    stamp = due.isoformat().replace("+00:00", "Z")
                    dedupe = f"retention:{package.name}@{package.version}:{retention.name}:{stamp}"
                    result = await conn.execute(
                        """
                        insert into job (workspace, kind, dedupe_key, payload)
                        values (%s, 'retention_purge', %s, %s)
                        on conflict (workspace, dedupe_key) where dedupe_key is not null do nothing
                        returning id
                        """,
                        (
                            workspace,
                            dedupe,
                            Jsonb(
                                {
                                    "package_name": package.name,
                                    "package_version": package.version,
                                    "retention": retention.name,
                                    "scheduled_at": stamp,
                                }
                            ),
                        ),
                    )
                    if await result.fetchone() is not None:
                        inserted += 1
    return inserted


async def claim_owned_tx(conn: DatabaseConnection, claimed: ClaimedJob) -> None:
    result = await conn.execute(
        """
        select 1 as owned from job
        where id = %s and locked_by = %s and done_at is null and dead_at is null
          and lease_until > clock_timestamp()
        for update
        """,
        (claimed.id, claimed.claim_token),
    )
    if await result.fetchone() is None:
        raise LeaseLost(f"job lease lost: {claimed.id}")


__all__ = [
    "claim_owned_tx",
    "enqueue_derive_tx",
    "evaluate_entity_triggers_tx",
    "evaluate_ready_triggers_tx",
    "schedule_cron_jobs",
]
