"""Shared SQL fragments for candidate generation and canonical reload.

Both the PostgreSQL candidate backend and the core engine build predicates
from one place so scope pushdown can never drift from the canonical recheck.
Every fragment references the canonical table through the ``row`` alias, and
declared field paths travel as parameters (`#>> %s::text[]`), never as
interpolated SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, LiteralString

from memseek.definitions.models import DeclaredField

from .spec import SearchSource

# (collection name, collection version) -> the declaration stored under that
# exact immutable version.  A field keeps one portable name while its path
# may differ per version.
type FieldVersions = dict[tuple[str, int], DeclaredField]

_CAST_BY_TYPE: dict[str, LiteralString] = {
    "number": "::numeric",
    "integer": "::numeric",
    "datetime": "::timestamptz",
    "boolean": "::boolean",
    "string": "",
}


def scope_conditions(
    source: SearchSource,
    workspace: str,
) -> tuple[list[LiteralString], list[Any]]:
    """Return WHERE fragments enforcing the complete canonical scope."""

    scope = source.scope
    clauses: list[LiteralString] = [
        "row.workspace = %s",
        "row.enriched_at is not null",
        "not coalesce((row.content->>'tombstone')::boolean, false)",
    ]
    params: list[Any] = [workspace]
    if scope.collections:
        unpinned = [name for name in scope.collections if name not in scope.collection_versions]
        terms: list[LiteralString] = []
        if unpinned:
            terms.append("row.collection = any(%s::text[])")
            params.append(unpinned)
        for name, versions in scope.collection_versions.items():
            terms.append("(row.collection = %s and row.collection_version = any(%s::int[]))")
            params.extend([name, list(versions)])
        clauses.append("(" + " or ".join(terms) + ")")
    else:
        clauses.append("row.collection <> '_system'")
    if scope.entities:
        clauses.append("row.entity = any(%s::text[])")
        params.append(list(scope.entities))
    if scope.types:
        clauses.append("row.type = any(%s::text[])")
        params.append(list(scope.types))
    if scope.status != "all":
        clauses.append("row.status = %s")
        params.append(scope.status)
    if scope.keyed is True:
        clauses.append("row.key is not null")
    elif scope.keyed is False:
        clauses.append("row.key is null")
    if isinstance(scope.occurred_after, datetime):
        clauses.append("row.occurred_at > %s")
        params.append(scope.occurred_after)
    if isinstance(scope.occurred_before, datetime):
        clauses.append("row.occurred_at < %s")
        params.append(scope.occurred_before)
    if scope.depth_lte is not None:
        clauses.append("row.depth <= %s")
        params.append(scope.depth_lte)
    if scope.versions == "current":
        clauses.append(
            """
            (row.key is null or not exists (
              select 1 from record newer
              where newer.workspace = row.workspace
                and newer.entity = row.entity
                and newer.collection = row.collection
                and newer.key = row.key
                and newer.status = row.status
                and newer.seq > row.seq
            ))
            """
        )
    return clauses, params


def field_annotation_names(declaration: DeclaredField) -> tuple[str, ...]:
    """Return every annotation this field can read, newest first.

    One name normally; more when the owning processor declares ``supersedes``.  A
    field is guaranteed to resolve as long as *any* name in the chain is required,
    which is what callers check before allowing a filter or sort over it.
    """

    names: list[str] = []
    for dotted in (declaration.path, *declaration.fallback_paths):
        root, *parts = dotted.split(".")
        if root == "annotations" and parts:
            names.append(parts[0])
    return tuple(names)


type FieldRoot = Literal["content", "annotations"]

_READ_BY_ROOT: dict[FieldRoot, LiteralString] = {
    "content": "row.content #>> %s::text[]",
    "annotations": "row.annotations #>> %s::text[]",
}


def declared_field_paths(
    declaration: DeclaredField,
) -> tuple[tuple[FieldRoot, list[str]], ...]:
    """Return the (root, JSON path) pairs to read for one declared field.

    More than one only when a processor declares ``supersedes``: the newest
    annotation is preferred and older ones answer for rows that predate it.
    """

    candidates: list[tuple[FieldRoot, list[str]]] = []
    for dotted in (declaration.path, *declaration.fallback_paths):
        root, *parts = dotted.split(".")
        candidates.append(("content" if root == "content" else "annotations", parts))
    return tuple(candidates)


def field_value_expression(
    versions: FieldVersions,
    *,
    cast: bool = True,
) -> tuple[LiteralString, list[Any]]:
    """Build one typed value expression dispatching on the stored version."""

    arms: list[LiteralString] = []
    params: list[Any] = []
    scalar_type = "string"
    for (collection, version), declaration in sorted(versions.items()):
        reads: list[LiteralString] = []
        read_params: list[Any] = []
        for root, parts in declared_field_paths(declaration):
            reads.append(_READ_BY_ROOT[root])
            read_params.append(parts)
        value: LiteralString = reads[0] if len(reads) == 1 else "coalesce(" + ", ".join(reads) + ")"
        arms.append(f"when row.collection = %s and row.collection_version = %s then {value}")
        params.extend([collection, version, *read_params])
        scalar_type = declaration.scalar_type
    expression: LiteralString = "(case " + " ".join(arms) + " else null end)"
    suffix = _CAST_BY_TYPE[scalar_type] if cast else ""
    return f"({expression}{suffix})", params


def pushdown_predicate(
    versions: FieldVersions,
    operator: str,
    operand: Any,
) -> tuple[LiteralString, list[Any]] | None:
    """Translate one scalar predicate into SQL, or ``None`` when not pushable.

    Array predicates (`contains_any`, `contains_all`, array `eq`) are only
    evaluated by the canonical core recheck; skipping their pushdown merely
    widens the candidate set.
    """

    declaration = next(iter(versions.values()))
    if declaration.is_array:
        return None
    value_sql, params = field_value_expression(versions)
    if operator == "exists":
        return (f"{value_sql} is not null" if operand else f"{value_sql} is null"), params
    if operator == "eq":
        return f"{value_sql} = %s{_CAST_BY_TYPE[declaration.scalar_type]}", [*params, operand]
    if operator == "in":
        if declaration.scalar_type in {"number", "integer"}:
            array_cast: LiteralString = "::numeric[]"
        elif declaration.scalar_type == "datetime":
            array_cast = "::timestamptz[]"
        elif declaration.scalar_type == "boolean":
            array_cast = "::boolean[]"
        else:
            array_cast = "::text[]"
        return f"{value_sql} = any(%s{array_cast})", [*params, list(operand)]
    if operator in {"gt", "gte", "lt", "lte"}:
        if operator == "gt":
            comparison: LiteralString = ">"
        elif operator == "gte":
            comparison = ">="
        elif operator == "lt":
            comparison = "<"
        else:
            comparison = "<="
        cast_suffix = _CAST_BY_TYPE[declaration.scalar_type]
        return f"{value_sql} {comparison} %s{cast_suffix}", [*params, operand]
    return None


__all__ = [
    "FieldVersions",
    "field_value_expression",
    "pushdown_predicate",
    "scope_conditions",
]
