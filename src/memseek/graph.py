"""Bounded canonical traversal over catalog-declared structural graphs."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal, LiteralString, cast
from uuid import UUID

from pydantic import Field, field_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions.base import PublicName, StrictModel
from memseek.definitions.models import GraphProjection

if TYPE_CHECKING:
    from memseek.definitions import DefinitionCatalog


type GraphDirection = Literal["out", "in", "both"]
type GraphPredicate = str


class GraphTraversalError(ValueError):
    """A bounded traversal cannot be served by the loaded catalog or settings."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class GraphTraversalRequest(StrictModel):
    """Input shared by graph-kind views and derivation Tasks."""

    seed: str = Field(min_length=1, max_length=128)
    graph: PublicName | None = None
    predicates: tuple[GraphPredicate, ...] = Field(default=(), max_length=100)
    direction: GraphDirection = "out"
    depth: int = Field(default=1, ge=1, le=16)
    limit: int = Field(default=20, ge=1, le=500)

    @field_validator("seed")
    @classmethod
    def normalize_seed(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("seed must not be blank")
        return normalized

    @field_validator("predicates")
    @classmethod
    def validate_predicates(cls, value: tuple[GraphPredicate, ...]) -> tuple[GraphPredicate, ...]:
        normalized = tuple(predicate.strip() for predicate in value)
        if any(not predicate for predicate in normalized):
            raise ValueError("predicates must not contain a blank value")
        if any(len(predicate) > 128 for predicate in normalized):
            raise ValueError("each predicate is capped at 128 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("predicates must be unique")
        return normalized


class GraphOrphansRequest(StrictModel):
    """Bounded request for live nodes with no current structural edge."""

    limit: int = Field(default=50, ge=1, le=500)


def resolve_graph_projection(
    catalog: DefinitionCatalog,
    *,
    graph: str | None = None,
    kind: Literal["graph", "graph_orphans"] = "graph",
) -> GraphProjection:
    """Resolve one graph view without assuming example collection names."""

    if graph is not None:
        try:
            view = catalog.resolve_view(graph)
        except (KeyError, ValueError) as exc:
            raise GraphTraversalError("graph_unavailable", f"unknown graph view {graph!r}") from exc
        if view.kind != kind or view.graph is None:
            raise GraphTraversalError("graph_unavailable", f"view {graph!r} is not a {kind} view")
        return view.graph

    candidates = [
        catalog.views[(name, version)]
        for name, version in catalog.active_views.items()
        if catalog.views[(name, version)].kind == kind
    ]
    if not candidates:
        raise GraphTraversalError(
            "graph_unavailable", f"the active workspace catalog has no {kind} view"
        )
    if len(candidates) > 1:
        names = sorted(view.name for view in candidates)
        raise GraphTraversalError(
            "graph_ambiguous",
            f"the active workspace catalog has multiple {kind} views {names}; select graph",
        )
    projection = candidates[0].graph
    assert projection is not None
    return projection


def _branch_limit(*, path_limit: int, depth: int) -> int:
    """Bound recursive fan-out before PostgreSQL materializes every path."""

    return max(1, math.ceil(path_limit ** (1 / depth)))


def _graph_field_sql(
    catalog: DefinitionCatalog,
    projection: GraphProjection,
    role: Literal["subject", "object", "predicate"],
    *,
    alias: str = "edge",
) -> str:
    """Compile one loader-validated declared field to a fixed SQL expression."""

    collection = catalog.resolve_collection(projection.edges)
    field = collection.fields[getattr(projection, role)]
    root, *parts = field.path.split(".")
    if len(parts) == 1:
        return f"{alias}.{root}->>'{parts[0]}'"
    return f"{alias}.{root}#>>'{{{','.join(parts)}}}'"


def _next_edges_sql(
    direction: GraphDirection,
    *,
    subject_sql: str,
    object_sql: str,
    predicate_sql: str,
) -> str:
    """Build only fixed SQL arms; request values remain bound parameters."""

    arms: list[str] = []
    if direction in {"out", "both"}:
        arms.append(
            f"""
            select edge.id, {object_sql} as to_node
            from record edge
            cross join graph_scope
            where edge.workspace = graph_scope.workspace
              and edge.collection = graph_scope.edge_collection
              and edge.status = 'active'
              and edge.enriched_at is not null
              and not coalesce((edge.content->>'tombstone')::boolean, false)
              and {subject_sql} = walk.node
              and (
                cardinality(graph_scope.predicates) = 0
                or {predicate_sql} = any(graph_scope.predicates)
              )
            """
        )
    if direction in {"in", "both"}:
        arms.append(
            f"""
            select edge.id, {subject_sql} as to_node
            from record edge
            cross join graph_scope
            where edge.workspace = graph_scope.workspace
              and edge.collection = graph_scope.edge_collection
              and edge.status = 'active'
              and edge.enriched_at is not null
              and not coalesce((edge.content->>'tombstone')::boolean, false)
              and {object_sql} = walk.node
              and (
                cardinality(graph_scope.predicates) = 0
                or {predicate_sql} = any(graph_scope.predicates)
              )
            """
        )
    return " union all ".join(arms)


async def traverse_graph(
    pool: DatabasePool,
    *,
    workspace: str,
    request: GraphTraversalRequest,
    catalog: DefinitionCatalog,
    settings: Settings,
    projection: GraphProjection | None = None,
) -> dict[str, Any]:
    """Return a bounded, workspace-isolated walk with canonical edge citations."""

    projection = projection or resolve_graph_projection(catalog, graph=request.graph)
    try:
        catalog.resolve_collection(projection.edges)
    except (KeyError, ValueError) as exc:
        raise GraphTraversalError(
            "graph_unavailable",
            f"the active workspace catalog does not include {projection.edges!r}",
        ) from exc
    subject_sql = _graph_field_sql(catalog, projection, "subject")
    object_sql = _graph_field_sql(catalog, projection, "object")
    predicate_sql = _graph_field_sql(catalog, projection, "predicate")
    next_edges_sql = _next_edges_sql(
        request.direction,
        subject_sql=subject_sql,
        object_sql=object_sql,
        predicate_sql=predicate_sql,
    )
    if request.depth > settings.max_graph_depth:
        raise GraphTraversalError(
            "graph_depth", f"depth exceeds MAX_GRAPH_DEPTH={settings.max_graph_depth}"
        )
    if request.limit > settings.max_graph_paths:
        raise GraphTraversalError(
            "graph_limit", f"limit exceeds MAX_GRAPH_PATHS={settings.max_graph_paths}"
        )

    params: list[Any] = [
        workspace,
        projection.edges,
        list(request.predicates),
        request.seed,
        _branch_limit(path_limit=request.limit, depth=request.depth),
        request.depth,
        request.limit + 1,
    ]
    query = cast(
        LiteralString,
        f"""
        with recursive graph_scope(workspace, edge_collection, predicates) as (
          values (%s::text, %s::text, %s::text[])
        ), start(seed) as (
          select %s::text
        ), walk(seed, node, nodes, edge_ids, depth) as (
          select seed, seed, array[seed]::text[], array[]::uuid[], 0
          from start
          union all
          select walk.seed,
                 next_edge.to_node,
                 array_append(walk.nodes, next_edge.to_node),
                 array_append(walk.edge_ids, next_edge.id),
                 walk.depth + 1
          from walk
          join lateral (
            select id, to_node
            from ({next_edges_sql}) as candidate
            where not to_node = any(walk.nodes)
            order by to_node, id
            limit %s
          ) as next_edge on true
          where walk.depth < %s
        )
        select nodes, edge_ids, depth
        from walk
        where depth > 0
        order by depth, nodes, edge_ids
        limit %s
    """,
    )
    async with pool.connection() as conn:
        result = await conn.execute(query, params)
        path_rows = await result.fetchall()
        citation_order = list(
            dict.fromkeys(
                edge_id for row in path_rows for edge_id in cast(list[UUID], row["edge_ids"])
            )
        )
        citations_by_id: dict[UUID, dict[str, Any]] = {}
        if citation_order:
            citation_query = cast(
                LiteralString,
                f"""
                select edge.id, edge.content,
                       {subject_sql} as subject,
                       {object_sql} as object,
                       {predicate_sql} as predicate
                from record edge
                where edge.workspace = %s
                  and edge.collection = %s
                  and edge.status = 'active'
                  and edge.enriched_at is not null
                  and not coalesce((edge.content->>'tombstone')::boolean, false)
                  and edge.id = any(%s::uuid[])
                """,
            )
            citation_result = await conn.execute(
                citation_query,
                (workspace, projection.edges, citation_order),
            )
            for row in await citation_result.fetchall():
                edge_id = cast(UUID, row["id"])
                content = cast(dict[str, Any], row["content"])
                citations_by_id[edge_id] = {
                    "id": str(edge_id),
                    "text": content["text"],
                    "subject": row["subject"],
                    "object": row["object"],
                    "predicate": row["predicate"],
                    "content": content,
                }

    truncated = len(path_rows) > request.limit
    paths: list[dict[str, Any]] = []
    for row in path_rows[: request.limit]:
        edge_ids = cast(list[UUID], row["edge_ids"])
        if any(edge_id not in citations_by_id for edge_id in edge_ids):
            continue
        paths.append(
            {
                "nodes": list(cast(list[str], row["nodes"])),
                "edge_ids": [str(edge_id) for edge_id in edge_ids],
                "depth": int(row["depth"]),
            }
        )
    used_citations = {
        UUID(edge_id) for path in paths for edge_id in cast(list[str], path["edge_ids"])
    }
    citations = [
        citations_by_id[edge_id] for edge_id in citation_order if edge_id in used_citations
    ]
    return {
        "nodes": sorted({node for path in paths for node in cast(list[str], path["nodes"])}),
        "paths": paths,
        "citations": citations,
        "truncated": truncated,
    }


async def graph_orphans(
    pool: DatabasePool,
    *,
    workspace: str,
    request: GraphOrphansRequest,
    catalog: DefinitionCatalog,
    settings: Settings,
    projection: GraphProjection | None = None,
) -> dict[str, Any]:
    """Return current ready nodes with no live incoming or outgoing edge.

    A directly ingested edge has no node provenance and is live as-is.  When
    an edge does cite the subject node it is live only while that exact node
    record remains current, so a derived edge from an old revision cannot keep
    either endpoint connected forever.
    """

    projection = projection or resolve_graph_projection(catalog, kind="graph_orphans")
    assert projection.nodes is not None
    try:
        catalog.resolve_collection(projection.edges)
        catalog.resolve_collection(projection.nodes)
    except (KeyError, ValueError) as exc:
        raise GraphTraversalError(
            "graph_unavailable",
            "the active workspace catalog does not include the graph's node and edge collections",
        ) from exc
    subject_sql = _graph_field_sql(catalog, projection, "subject")
    object_sql = _graph_field_sql(catalog, projection, "object")
    if request.limit > settings.max_graph_paths:
        raise GraphTraversalError(
            "graph_limit", f"limit exceeds MAX_GRAPH_PATHS={settings.max_graph_paths}"
        )

    query = cast(
        LiteralString,
        f"""
        with current_nodes as (
          select distinct on (node.entity, node.key)
                 node.id, node.entity, node.key, node.content
          from record node
          where node.workspace = %s
            and node.collection = %s
            and node.status = 'active'
            and node.key is not null
            and node.enriched_at is not null
          order by node.entity, node.key, node.seq desc
        ), live_edges as (
          select edge.entity,
                 {subject_sql} as subject,
                 {object_sql} as object
          from record edge
          where edge.workspace = %s
            and edge.collection = %s
            and edge.status = 'active'
            and edge.enriched_at is not null
            and not coalesce((edge.content->>'tombstone')::boolean, false)
            and (
              not exists (
                select 1
                from record source_record
                where source_record.workspace = edge.workspace
                  and source_record.collection = %s
                  and source_record.entity = edge.entity
                  and source_record.key = {subject_sql}
                  and source_record.id = any(edge.derived_from)
              )
              or exists (
                select 1
                from current_nodes source
                where source.entity = edge.entity
                  and source.key = {subject_sql}
                  and not coalesce((source.content->>'tombstone')::boolean, false)
                  and source.id = any(edge.derived_from)
              )
            )
        )
        select node.id, node.entity, node.key, node.content
        from current_nodes node
        where not coalesce((node.content->>'tombstone')::boolean, false)
          and not exists (
            select 1
            from live_edges edge
            where edge.entity = node.entity
              and (edge.subject = node.key or edge.object = node.key)
          )
        order by node.entity, node.key
        limit %s
        """,
    )
    async with pool.connection() as conn:
        result = await conn.execute(
            query,
            (
                workspace,
                projection.nodes,
                workspace,
                projection.edges,
                projection.nodes,
                request.limit + 1,
            ),
        )
        rows = await result.fetchall()
    truncated = len(rows) > request.limit
    orphans = [
        {
            "id": str(cast(UUID, row["id"])),
            "entity": str(row["entity"]),
            "key": str(row["key"]),
            "text": str(cast(dict[str, Any], row["content"])["text"]),
            "content": cast(dict[str, Any], row["content"]),
        }
        for row in rows[: request.limit]
    ]
    return {"orphans": orphans, "truncated": truncated}


__all__ = [
    "GraphOrphansRequest",
    "GraphTraversalError",
    "GraphTraversalRequest",
    "graph_orphans",
    "resolve_graph_projection",
    "traverse_graph",
]
