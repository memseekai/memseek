"""Named-view catalog reads and parameterized execution.

Views are versioned, read-only contracts. Search views render a normalized
``SearchSpec``; graph views run bounded structural traversal. Neither advances
a watermark or creates records.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, ViewDefinition, parameters_json_schema
from memseek.definitions.models import parameter_value_matches
from memseek.templates import TemplateError, render_object

from .engine import SearchRequestError, execute_search
from .spec import SearchSpec


class ViewNotFound(Exception):
    def __init__(self, detail: str) -> None:
        self.code = "view_not_found"
        self.detail = detail
        super().__init__(detail)


def _view_collections(view: ViewDefinition, catalog: DefinitionCatalog) -> list[str]:
    if view.kind == "graph":
        assert view.graph is not None
        try:
            catalog.resolve_collection(view.graph.edges)
        except KeyError, ValueError:
            return []
        return [view.graph.edges]
    if view.kind == "graph_orphans":
        assert view.graph is not None
        assert view.graph.nodes is not None
        try:
            catalog.resolve_collection(view.graph.edges)
            catalog.resolve_collection(view.graph.nodes)
        except KeyError, ValueError:
            return []
        return [view.graph.edges, view.graph.nodes]

    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            scoped = value.get("scope")
            if isinstance(scoped, dict):
                for name in scoped.get("collections", ()):
                    if isinstance(name, str):
                        names.add(name)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(view.query)
    return sorted(names & {name for name, _ in catalog.collections})


def _view_profiles(collections: list[str], catalog: DefinitionCatalog) -> list[str]:
    profiles: set[str] = set()
    for name in collections:
        binding = catalog.deployment_bindings.get(name)
        profiles.add(
            binding if binding is not None else catalog.resolve_collection(name).search_profile
        )
    return sorted(profiles)


def view_catalog_payload(catalog: DefinitionCatalog) -> dict[str, Any]:
    """Build the ``GET /views`` listing from loaded definitions."""

    views = []
    for (name, version), view in sorted(catalog.views.items()):
        collections = _view_collections(view, catalog)
        views.append(
            {
                "name": name,
                "version": version,
                "hash": view.definition_hash,
                "active": catalog.active_views.get(name) == version,
                "kind": view.kind,
                "graph": (None if view.graph is None else view.graph.model_dump(mode="json")),
                "parameters": {
                    parameter_name: {
                        "type": parameter.type,
                        "required": parameter.required,
                        "default": parameter.default,
                    }
                    for parameter_name, parameter in view.parameters.items()
                },
                "input_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    **parameters_json_schema(view.parameters),
                },
                "collections": collections,
                "required_capabilities": list(view.required_capabilities),
                "profiles": _view_profiles(collections, catalog),
            }
        )
    return {"views": views}


def resolve_view_parameters(
    view: ViewDefinition,
    supplied: dict[str, Any],
) -> dict[str, Any]:
    """Validate supplied values against the view's typed parameter schema."""

    unknown = sorted(set(supplied) - set(view.parameters))
    if unknown:
        raise SearchRequestError(
            "view_parameter", f"unknown view parameter(s): {', '.join(unknown)}"
        )
    resolved: dict[str, Any] = {}
    for name, parameter in view.parameters.items():
        if name in supplied:
            value = supplied[name]
        elif parameter.default is not None:
            value = parameter.default
        elif parameter.required:
            raise SearchRequestError("view_parameter", f"missing required view parameter {name!r}")
        else:
            continue
        if not parameter_value_matches(parameter, value):
            raise SearchRequestError(
                "view_parameter",
                f"view parameter {name!r} does not match its declared schema",
            )
        resolved[name] = value
    return resolved


async def execute_view(
    pool: DatabasePool,
    *,
    workspace: str,
    name: str,
    parameters: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Render, validate, and execute one named view."""

    try:
        view = catalog.resolve_view(name)
    except (KeyError, ValueError) as exc:
        raise ViewNotFound(f"unknown view {name!r}") from exc
    resolved_parameters = resolve_view_parameters(view, parameters)
    view_metadata = {"name": view.name, "version": view.version, "hash": view.definition_hash}
    if view.kind == "graph":
        from memseek.graph import GraphTraversalError, GraphTraversalRequest, traverse_graph

        try:
            graph_request = GraphTraversalRequest.model_validate(resolved_parameters)
        except ValidationError as exc:
            issue = exc.errors(include_url=False)[0]
            raise SearchRequestError("view_parameter", str(issue["msg"])) from exc
        try:
            graph = await traverse_graph(
                pool,
                workspace=workspace,
                request=graph_request,
                catalog=catalog,
                settings=settings,
                projection=view.graph,
            )
        except GraphTraversalError as exc:
            raise SearchRequestError(exc.code, exc.detail) from exc
        collections = _view_collections(view, catalog)
        return {
            "view": view_metadata,
            "parameters": resolved_parameters,
            # Keeping edge citations in hits preserves the named-view source and
            # artifact contracts without pretending this is ranked search.
            "hits": graph["citations"],
            "input_record_ids": [citation["id"] for citation in graph["citations"]],
            "nodes": graph["nodes"],
            "paths": graph["paths"],
            "citations": graph["citations"],
            "truncated": graph["truncated"],
            "profiles": _view_profiles(collections, catalog),
            "backend": [{"kind": "graph", "name": "postgresql"}],
        }
    if view.kind == "graph_orphans":
        from memseek.graph import GraphOrphansRequest, GraphTraversalError, graph_orphans

        try:
            orphan_request = GraphOrphansRequest.model_validate(resolved_parameters)
        except ValidationError as exc:
            issue = exc.errors(include_url=False)[0]
            raise SearchRequestError("view_parameter", str(issue["msg"])) from exc
        try:
            result = await graph_orphans(
                pool,
                workspace=workspace,
                request=orphan_request,
                catalog=catalog,
                settings=settings,
                projection=view.graph,
            )
        except GraphTraversalError as exc:
            raise SearchRequestError(exc.code, exc.detail) from exc
        collections = _view_collections(view, catalog)
        return {
            "view": view_metadata,
            "parameters": resolved_parameters,
            "hits": result["orphans"],
            "input_record_ids": [orphan["id"] for orphan in result["orphans"]],
            "orphans": result["orphans"],
            "truncated": result["truncated"],
            "profiles": _view_profiles(collections, catalog),
            "backend": [{"kind": "graph_orphans", "name": "postgresql"}],
        }

    assert view.query is not None
    try:
        rendered_query = render_object(view.query, resolved_parameters)
    except TemplateError as exc:
        raise SearchRequestError("view_parameter", str(exc)) from exc
    try:
        spec = SearchSpec.model_validate(rendered_query)
    except ValidationError as exc:
        issue = exc.errors(include_url=False)[0]
        raise SearchRequestError("search_spec", str(issue["msg"])) from exc
    result = await execute_search(
        pool,
        workspace=workspace,
        spec=spec,
        catalog=catalog,
        settings=settings,
        extra_capabilities=view.required_capabilities,
    )
    backend = result["backend"]
    return {
        "view": view_metadata,
        "parameters": resolved_parameters,
        "hits": result["hits"],
        "ranking": result["ranking"],
        "input_record_ids": [hit["id"] for hit in result["hits"]],
        "rendered": result["rendered"],
        "truncated": result["truncated"],
        "profiles": result["profiles"],
        # The view response reports one backend entry per executed source.
        "backend": backend if isinstance(backend, list) else [backend],
    }


__all__ = ["ViewNotFound", "execute_view", "resolve_view_parameters", "view_catalog_payload"]
