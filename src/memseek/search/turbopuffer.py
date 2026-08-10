"""Small, typed Turbopuffer HTTP adapter.

The canonical search engine remains responsible for scope rechecks and ranking;
this adapter only supplies bounded candidate IDs and durable projection writes.
All namespace names are derived from hashes so tenant and collection strings do
not cross the external-index boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from memseek.config import Settings
from memseek.projections import projected_attribute_name
from memseek.search.registry import CandidateHit, CandidateQuery, SearchCapability

_RETRYABLE = {429, 500, 502, 503, 504}
_SCHEMA: dict[str, Any] = {
    "id": "uuid",
    "vector": {"type": "[1536]f32", "ann": True},
    "text": {
        "type": "string",
        "full_text_search": {
            "tokenizer": "word_v4",
            "language": "english",
            "stemming": True,
            "remove_stopwords": True,
            "case_sensitive": False,
        },
    },
    "has_embedding": "bool",
    "embedding_space": "string",
    "collection": "string",
    "entity": "string",
    "type": "string",
    "status": "string",
    "keyed": "bool",
    "is_current": "bool",
    "tombstone": "bool",
    "depth": "uint",
    "seq": "uint",
    "occurred_at": "datetime",
    "created_at": "datetime",
}


class TurbopufferError(RuntimeError):
    """Bounded, content-free adapter failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def namespace_name(workspace: str, *, collection: str | None = None, layout: str = "shared") -> str:
    """Return the deterministic external namespace for one workspace scope."""

    workspace_hash = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:24]
    if layout == "shared":
        return f"ms_{workspace_hash}"
    if layout == "per_collection" and collection is not None:
        collection_hash = hashlib.sha256(collection.encode("utf-8")).hexdigest()[:24]
        return f"ms_{workspace_hash}__{collection_hash}"
    raise ValueError("per_collection namespaces require a collection")


def workspace_namespace(workspace: str) -> str:
    """Return the deterministic shared-layout namespace."""

    return namespace_name(workspace)


def collection_namespace(workspace: str, collection: str) -> str:
    """Return the deterministic per-collection namespace."""

    return namespace_name(workspace, collection=collection, layout="per_collection")


def _filter_expression(query: CandidateQuery) -> Any:
    scope = query.source.scope
    terms: list[Any] = []
    if scope.collections:
        terms.append(
            [
                "Or",
                *[["collection", "Eq", name] for name in scope.collections],
            ]
        )
    if scope.entities:
        terms.append(["entity", "In", list(scope.entities)])
    if scope.types:
        terms.append(["type", "In", list(scope.types)])
    if scope.status != "all":
        terms.append(["status", "Eq", scope.status])
    if scope.keyed is not None:
        terms.append(["keyed", "Eq", scope.keyed])
    if scope.versions == "current":
        terms.append(["is_current", "Eq", True])
    if scope.occurred_after is not None:
        value = scope.occurred_after
        terms.append(
            ["occurred_at", "Gt", value.isoformat() if isinstance(value, datetime) else value]
        )
    if scope.occurred_before is not None:
        value = scope.occurred_before
        terms.append(
            ["occurred_at", "Lt", value.isoformat() if isinstance(value, datetime) else value]
        )
    if scope.depth_lte is not None:
        terms.append(["depth", "Lte", scope.depth_lte])
    for field, predicates in query.source.where.items():
        versions = query.field_versions.get(field)
        if not versions:
            continue
        collection, version = sorted(versions)[0]
        attribute = projected_attribute_name(collection, version, field)
        for operator, value in predicates.items():
            if isinstance(value, str) and value.startswith("{{"):
                continue
            op = {
                "eq": "Eq",
                "in": "In",
                "gt": "Gt",
                "gte": "Gte",
                "lt": "Lt",
                "lte": "Lte",
                "exists": "Eq",
            }.get(operator)
            if op is not None:
                terms.append([attribute, op, value])
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return ["And", *terms]


def _query_body(
    query: CandidateQuery, qvec: list[float] | None, *, consistency: str = "strong"
) -> dict[str, Any]:
    source = query.source
    filters = _filter_expression(query)
    common: dict[str, Any] = {
        "filters": filters,
        "limit": source.candidates,
        "include_attributes": [],
    }
    if source.mode == "vector":
        if qvec is None:
            raise TurbopufferError("search_backend", "vector search requires a query vector")
        common["filters"] = _and_filter(filters, ["has_embedding", "Eq", True])
        common["rank_by"] = ["vector", "ANN", qvec]
    elif source.mode == "text":
        common["rank_by"] = ["text", "BM25", query.query or ""]
    elif source.mode == "recent":
        common["rank_by"] = ["occurred_at", "desc"]
    elif source.mode == "structured":
        order = source.order_by[0]
        versions = query.field_versions.get(order.field)
        if versions:
            collection, version = sorted(versions)[0]
            field = projected_attribute_name(collection, version, order.field)
        else:
            field = order.field
        common["rank_by"] = [field, order.direction]
    else:
        if qvec is None:
            raise TurbopufferError("search_backend", "hybrid search requires a query vector")
        common = {
            "queries": [
                {
                    **common,
                    "rank_by": ["vector", "ANN", qvec],
                    "filters": _and_filter(filters, ["has_embedding", "Eq", True]),
                },
                {**common, "rank_by": ["text", "BM25", query.query or ""]},
                {**common, "rank_by": ["occurred_at", "desc"]},
            ],
            "consistency": {"level": consistency},
        }
    return common


def _and_filter(left: Any, right: Any) -> Any:
    if left is None:
        return right
    return ["And", left, right]


def _extract_rows(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    values: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(payload.get("rows"), list):
        values.extend(("remote", row) for row in payload["rows"] if isinstance(row, Mapping))
    results = payload.get("results")
    if isinstance(results, list):
        for index, result in enumerate(results):
            if isinstance(result, Mapping) and isinstance(result.get("rows"), list):
                rows = cast(Mapping[str, Any], result).get("rows")
                if isinstance(rows, list):
                    values.extend(
                        (str(index), cast(Mapping[str, Any], row))
                        for row in rows
                        if isinstance(row, Mapping)
                    )
    return values


class TurbopufferSearchBackend:
    """Turbopuffer candidate and projection implementation."""

    NAME: ClassVar[str] = "turbopuffer"
    CAPS: ClassVar[frozenset[SearchCapability]] = frozenset(
        {"vector", "text", "recent", "structured"}
    )

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _base_url(self, settings: Settings) -> str:
        return (
            settings.turbopuffer_base_url.rstrip("/")
            if settings.turbopuffer_base_url
            else f"https://{settings.turbopuffer_region}.turbopuffer.com"
        )

    async def _request(
        self,
        settings: Settings,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        if not settings.turbopuffer_api_key:
            raise TurbopufferError(
                "search_backend_unavailable", "Turbopuffer credentials are not configured"
            )
        url = f"{self._base_url(settings)}{path}"
        headers = {"Authorization": f"Bearer {settings.turbopuffer_api_key}"}
        for attempt in range(2):
            try:
                response = await self._client.request(method, url, headers=headers, json=json)
            except httpx.RequestError as exc:
                if attempt:
                    raise TurbopufferError(
                        "search_backend_unavailable", "Turbopuffer transport failed"
                    ) from exc
                await asyncio.sleep(2)
                continue
            if response.status_code in _RETRYABLE and attempt == 0:
                retry_after = response.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 2.0
                except ValueError:
                    delay = 2.0
                await asyncio.sleep(max(0.0, min(delay, 30.0)))
                continue
            if response.status_code == 202:
                deadline = asyncio.get_running_loop().time() + 30.0
                delay = 0.5
                while response.status_code == 202 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(delay)
                    try:
                        response = await self._client.request(
                            method,
                            url,
                            headers=headers,
                            json=json,
                        )
                    except httpx.RequestError as exc:
                        raise TurbopufferError(
                            "search_backend_unavailable", "Turbopuffer transport failed"
                        ) from exc
                    delay = min(delay * 2, 4.0)
                if response.status_code == 202:
                    raise TurbopufferError(
                        "search_index_building", "Turbopuffer index is still building"
                    )
            if response.status_code == 404:
                return response
            if response.is_error:
                raise TurbopufferError(
                    "search_backend", f"Turbopuffer returned HTTP {response.status_code}"
                )
            return response
        raise TurbopufferError("search_backend_unavailable", "Turbopuffer request failed")

    async def _namespaces(
        self,
        settings: Settings,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
    ) -> list[tuple[str, str | None]]:
        layout = query.layout or settings.turbopuffer_layout
        if layout == "shared":
            return [(namespace_name(workspace), None)]
        collections = list(query.source.scope.collections)
        if not collections:
            result = await conn.execute(
                "select distinct collection from record where workspace = %s order by collection",
                (workspace,),
            )
            collections = [str(row["collection"]) for row in await result.fetchall()]
        if len(collections) > settings.max_collection_fanout:
            raise TurbopufferError(
                "search_fanout",
                "per_collection search exceeds MAX_COLLECTION_FANOUT; narrow collections",
            )
        return [
            (namespace_name(workspace, collection=collection, layout="per_collection"), collection)
            for collection in collections
        ]

    async def candidates(
        self,
        cfg: Settings,
        conn: Any,
        workspace: str,
        query: CandidateQuery,
        qvec: list[float] | None,
    ) -> list[CandidateHit]:
        hits: list[CandidateHit] = []
        for namespace, _collection in await self._namespaces(cfg, conn, workspace, query):
            response = await self._request(
                cfg,
                "POST",
                f"/v2/namespaces/{quote(namespace, safe='')}/query",
                json=_query_body(query, qvec, consistency=cfg.turbopuffer_consistency),
            )
            if response.status_code == 404:
                continue
            for channel, row in _extract_rows(response.json()):
                try:
                    record_id = UUID(str(row.get("id")))
                except TypeError, ValueError, AttributeError:
                    continue
                distance = row.get("$dist")
                hits.append(
                    CandidateHit(
                        id=record_id,
                        channel=channel,
                        backend_score=float(distance)
                        if isinstance(distance, (float, int))
                        else None,
                    )
                )
        return hits

    async def upsert(self, cfg: Settings, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            collection = str(row["collection"])
            workspace = str(row.get("workspace", ""))
            if not workspace:
                raise TurbopufferError("projection_payload", "projection row is missing workspace")
            namespace = namespace_name(
                workspace,
                collection=collection if cfg.turbopuffer_layout == "per_collection" else None,
                layout=cfg.turbopuffer_layout,
            )
            grouped[namespace].append(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"workspace", "collection_version", "collection_hash"}
                }
            )
        for namespace, items in grouped.items():
            await self._request(
                cfg,
                "POST",
                f"/v2/namespaces/{quote(namespace, safe='')}",
                json={
                    "upsert_rows": items,
                    "distance_metric": "cosine_distance",
                    "schema": _SCHEMA,
                },
            )

    async def delete(self, cfg: Settings, workspace: str, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            collection = str(row["collection"])
            namespace = namespace_name(
                workspace,
                collection=collection if cfg.turbopuffer_layout == "per_collection" else None,
                layout=cfg.turbopuffer_layout,
            )
            grouped[namespace].append(str(row["id"]))
        for namespace, ids in grouped.items():
            response = await self._request(
                cfg,
                "POST",
                f"/v2/namespaces/{quote(namespace, safe='')}",
                json={"delete_rows": ids},
            )
            if response.status_code == 404:
                continue


__all__ = [
    "TurbopufferError",
    "TurbopufferSearchBackend",
    "collection_namespace",
    "namespace_name",
    "workspace_namespace",
]
