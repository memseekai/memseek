"""Budgeted context assembly across document, search, recent, and delta.

`/context` is a shipped convenience assembler, not the general prompt-artifact
abstraction.  Sections are packed in spec order with fixed budget shares that
spill forward, and rows are deduplicated by record ID before greedy
whole-record packing.

The endpoint has no author template to own its framing, so the caller declares
it: `fence_tag` and `fence_preamble` wrap the rendering, and their absence
returns bare escaped rows for the caller to compose itself.  Either way the
rows are escaped, so they cannot close the element a caller puts around them.
"""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.definitions.base import PUBLIC_NAME_PATTERN
from memseek.logging import log_event
from memseek.render import (
    FenceDeclaration,
    RenderableRecord,
    fence_overhead_tokens,
    render_record,
    render_rows,
)
from memseek.search.engine import execute_search
from memseek.search.spec import SearchSpec
from memseek.views.delta import DeltaQuery, fetch_delta
from memseek.views.shared import FrozenQueryModel, split_names

LOGGER = logging.getLogger(__name__)

_DOCUMENT_SHARE = 0.30
_SEARCH_SHARE = 0.40
_RECENT_SHARE = 0.20
_DELTA_SHARE = 0.10
_SECTION_CANDIDATE_ROWS = 200
_SEARCH_K = 100


class ContextQuery(FrozenQueryModel):
    """Validated `GET /context` parameters."""

    entity: str = Field(min_length=1, max_length=255)
    task: str = Field(min_length=1)
    budget: int = Field(ge=1)
    consumer: str | None = Field(default=None, min_length=1, max_length=128)
    collections: tuple[str, ...] | None = None
    fence_tag: str | None = Field(default=None, pattern=PUBLIC_NAME_PATTERN)
    fence_preamble: str | None = Field(default=None, min_length=1, max_length=512)

    normalize_collections = field_validator("collections", mode="before")(split_names)

    @property
    def fence(self) -> FenceDeclaration | None:
        """The caller's declaration, or nothing when it asked for bare rows."""

        if self.fence_tag is None:
            return None
        return FenceDeclaration(tag=self.fence_tag, preamble=self.fence_preamble)

    @field_validator("entity")
    @classmethod
    def reject_wildcard(cls, value: str) -> str:
        if value == "*":
            raise ValueError("entity cannot be '*'")
        return value

    @model_validator(mode="after")
    def non_blank_task(self) -> ContextQuery:
        if not self.task.strip():
            raise ValueError("task must be non-blank")
        if self.fence_preamble is not None and self.fence_tag is None:
            raise ValueError("fence_preamble requires fence_tag")
        return self


class ContextRequestError(ValueError):
    """A context request exceeds a configured bound."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _tokens(value: str) -> int:
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


def _renderable(row: dict[str, Any]) -> RenderableRecord:
    return RenderableRecord(
        id=row["id"],
        occurred_at=row["occurred_at"],
        collection=row["collection"],
        type=row["type"],
        content=row["content"],
        key=row["key"],
        scores=row["scores"],
    )


_ROW_COLUMNS = "id, collection, key, type, content, scores, occurred_at, seq"


async def _document_candidates(
    conn: Any, *, workspace: str, query: ContextQuery, settings: Settings
) -> list[UUID]:
    clauses = ["workspace = %s", "entity = %s", "status = 'active'", "key is not null"]
    params: list[Any] = [workspace, query.entity]
    if query.collections:
        clauses.append("collection = any(%s)")
        params.append(list(query.collections))
    params.extend([settings.context_doc_order_score, _SECTION_CANDIDATE_ROWS])
    result = await conn.execute(
        f"""
        select id from (
          select distinct on (collection, key) id, content, scores, seq
          from record
          where {" and ".join(clauses)} and collection not like '\\_%%'
          order by collection, key, seq desc
        ) current
        where coalesce((content->>'tombstone')::boolean, false) is not true
        order by (scores->>%s)::float desc nulls last, seq desc
        limit %s
        """,
        params,
    )
    return [row["id"] for row in await result.fetchall()]


async def _recent_candidates(conn: Any, *, workspace: str, query: ContextQuery) -> list[UUID]:
    clauses = ["workspace = %s", "entity = %s", "status = 'active'"]
    params: list[Any] = [workspace, query.entity]
    if query.collections:
        clauses.append("collection = any(%s)")
        params.append(list(query.collections))
    params.append(_SECTION_CANDIDATE_ROWS)
    result = await conn.execute(
        f"""
        select id from record
        where {" and ".join(clauses)} and collection not like '\\_%%'
          and coalesce((content->>'tombstone')::boolean, false) is not true
        order by occurred_at desc, seq desc
        limit %s
        """,
        params,
    )
    return [row["id"] for row in await result.fetchall()]


async def _search_candidates(
    pool: DatabasePool,
    *,
    workspace: str,
    query: ContextQuery,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> list[UUID]:
    spec = SearchSpec.model_validate(
        {
            "q": query.task,
            "mode": "hybrid",
            "scope": {
                "entities": [query.entity],
                "collections": list(query.collections or ()),
            },
            "k": _SEARCH_K,
        }
    )
    result = await execute_search(
        pool, workspace=workspace, spec=spec, catalog=catalog, settings=settings
    )
    return [UUID(hit["id"]) for hit in result["hits"]]


async def _delta_candidates(
    pool: DatabasePool,
    *,
    workspace: str,
    query: ContextQuery,
    settings: Settings,
) -> tuple[list[UUID], dict[str, Any]]:
    assert query.consumer is not None
    delta_query = DeltaQuery(
        consumer=query.consumer,
        entity=query.entity,
        collections=query.collections,
        status="active",
        include_system=False,
    )
    delta = await fetch_delta(pool, workspace=workspace, query=delta_query, settings=settings)
    ids = [UUID(row["id"]) for row in delta["records"] if not row["tombstone"]]
    meta = {"next_cursor": delta["next_cursor"], "scope_hash": delta["scope_hash"]}
    return ids, meta


async def build_context(
    pool: DatabasePool,
    *,
    workspace: str,
    query: ContextQuery,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Assemble one budgeted, deduplicated context rendering."""

    if len(query.task) > settings.max_query_chars:
        raise ContextRequestError("request_schema", "task exceeds MAX_QUERY_CHARS")
    if query.budget > settings.model_context_tokens:
        raise ContextRequestError("request_schema", "budget exceeds MODEL_CONTEXT_TOKENS")

    async with pool.connection() as conn:
        document_ids = await _document_candidates(
            conn, workspace=workspace, query=query, settings=settings
        )
        recent_ids = await _recent_candidates(conn, workspace=workspace, query=query)
    search_ids = await _search_candidates(
        pool, workspace=workspace, query=query, catalog=catalog, settings=settings
    )
    delta_meta: dict[str, Any] | None = None
    delta_ids: list[UUID] = []
    if query.consumer is not None:
        delta_ids, delta_meta = await _delta_candidates(
            pool, workspace=workspace, query=query, settings=settings
        )

    sections: list[tuple[str, list[UUID], float]] = [
        ("document", document_ids, _DOCUMENT_SHARE),
        ("search", search_ids, _SEARCH_SHARE),
        (
            "recent",
            recent_ids,
            _RECENT_SHARE if query.consumer is not None else _RECENT_SHARE + _DELTA_SHARE,
        ),
    ]
    if query.consumer is not None:
        sections.append(("delta", delta_ids, _DELTA_SHARE))

    unique_ids = list(dict.fromkeys(id_ for _, ids, _ in sections for id_ in ids))
    rows_by_id: dict[UUID, dict[str, Any]] = {}
    if unique_ids:
        async with pool.connection() as conn:
            result = await conn.execute(
                f"select {_ROW_COLUMNS} from record where workspace = %s and id = any(%s::uuid[])",
                (workspace, unique_ids),
            )
            rows_by_id = {row["id"]: row for row in await result.fetchall()}

    fence = query.fence
    available = max(0, query.budget - fence_overhead_tokens(fence, _tokens))
    packed_rows: list[str] = []
    seen: set[UUID] = set()
    components: dict[str, Any] = {}
    truncated = False
    carry = 0.0
    emitted_ids: list[UUID] = []
    for name, ids, share in sections:
        section_budget = available * share + carry
        used = 0
        section_ids: list[str] = []
        section_tokens = 0
        for id_ in ids:
            if id_ in seen:
                continue
            row = rows_by_id.get(id_)
            if row is None:
                continue
            line = render_record(_renderable(row), profile="compact", catalog=catalog)
            cost = _tokens(line)
            if used + cost > section_budget:
                truncated = True
                continue
            used += cost
            seen.add(id_)
            packed_rows.append(line)
            emitted_ids.append(id_)
            section_ids.append(str(id_))
            section_tokens += cost
        carry = section_budget - used
        components[name] = {"ids": section_ids, "tokens": section_tokens}
    if delta_meta is not None:
        components["delta"].update(delta_meta)

    rendered = render_rows(packed_rows, fence=fence)
    if emitted_ids and settings.touch_on_read:
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "update record set last_accessed = now() where workspace = %s and id = any(%s::uuid[])",
                    (workspace, emitted_ids),
                )
        except Exception as exc:
            log_event(
                LOGGER,
                "warning",
                "reads.touch_failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
    return {
        "entity": query.entity,
        "task": query.task,
        "budget": query.budget,
        "components": components,
        "input_record_ids": [str(id_) for id_ in emitted_ids],
        "rendered": rendered,
        "tokens": _tokens(rendered),
        "truncated": truncated,
    }


__all__ = ["ContextQuery", "ContextRequestError", "build_context"]
