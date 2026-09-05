"""Bounded invocation memory over the canonical append-only run journal.

Briefs and receipts are navigation aids. They always retain their source event
range and citations; callers that need factual authority open the original
event or the MemSeek records named by its citations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from memseek.db import DatabaseConnection, DatabasePool

PressureAction = Literal["keep", "pointerize", "compact", "pause"]


@dataclass(frozen=True, slots=True)
class ContextPressure:
    used_tokens: int
    max_input_tokens: int
    reserve_output_tokens: int
    pointerize: float = 0.70
    compact: float = 0.82
    pause: float = 0.92

    @property
    def ratio(self) -> float:
        usable = self.max_input_tokens - self.reserve_output_tokens
        return self.used_tokens / max(usable, 1)

    @property
    def action(self) -> PressureAction:
        ratio = self.ratio
        if ratio >= self.pause:
            return "pause"
        if ratio >= self.compact:
            return "compact"
        if ratio >= self.pointerize:
            return "pointerize"
        return "keep"


async def persist_completion_memory_tx(
    conn: DatabaseConnection,
    *,
    workspace: str,
    invocation_id: UUID,
    event_id: UUID,
    end_ordinal: int,
    task: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Persist one range-indexed receipt and refreshed Working Brief."""

    previous = await conn.execute(
        "select coalesce(max(end_ordinal), 0) as value from invocation_memory_node "
        "where invocation_id = %s and kind = 'episode_receipt'",
        (invocation_id,),
    )
    row = await previous.fetchone()
    assert row is not None
    start = int(row["value"]) + 1
    citations = [str(value) for value in result.get("citation_ids", ())]
    receipt = {
        "kind": "episode_receipt",
        "range": {"start": start, "end": end_ordinal},
        "task_kind": task.get("kind"),
        "output_sha256": result.get("receipt", {}).get("output_sha256"),
        "citations": citations,
        "steps": result.get("steps", 0),
    }
    await conn.execute(
        """
        insert into invocation_memory_node
          (id, workspace, invocation_id, kind, start_ordinal, end_ordinal,
           level, content, source_event_ids)
        values (gen_random_uuid(), %s, %s, 'episode_receipt', %s, %s, 0, %s, %s)
        """,
        (workspace, invocation_id, start, end_ordinal, Jsonb(receipt), [event_id]),
    )
    brief = {
        "kind": "working_brief",
        "objective": task.get("prompt") or task.get("kind"),
        "latest_result": result.get("value"),
        "citations": citations,
        "open_loops": [],
        "source_range": {"start": 1, "end": end_ordinal},
    }
    await conn.execute(
        """
        insert into invocation_memory_node
          (id, workspace, invocation_id, kind, start_ordinal, end_ordinal,
           level, content, source_event_ids)
        values (gen_random_uuid(), %s, %s, 'working_brief', 1, %s, 0, %s, %s)
        """,
        (workspace, invocation_id, end_ordinal, Jsonb(brief), [event_id]),
    )


async def read_memory_nodes(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    kind: Literal["working_brief", "episode_receipt"] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("memory node limit must be between 1 and 200")
    async with pool.connection() as conn:
        if kind is None:
            found = await conn.execute(
                """
                select id, kind, start_ordinal, end_ordinal, level, content,
                       source_event_ids, created_at
                from invocation_memory_node
                where workspace = %s and invocation_id = %s
                order by created_at desc
                limit %s
                """,
                (workspace, invocation_id, limit),
            )
        else:
            found = await conn.execute(
                """
                select id, kind, start_ordinal, end_ordinal, level, content,
                       source_event_ids, created_at
                from invocation_memory_node
                where workspace = %s and invocation_id = %s and kind = %s
                order by created_at desc
                limit %s
                """,
                (workspace, invocation_id, kind, limit),
            )
        rows = await found.fetchall()
    return {
        "nodes": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "start_ordinal": int(row["start_ordinal"]),
                "end_ordinal": int(row["end_ordinal"]),
                "level": int(row["level"]),
                "content": row["content"],
                "source_event_ids": [str(value) for value in row["source_event_ids"]],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


async def recall_invocation(
    pool: DatabasePool,
    *,
    workspace: str,
    invocation_id: UUID,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Search receipts and original journal events without widening session scope."""

    normalized = query.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("recall query must contain 1 to 512 characters")
    if not 1 <= limit <= 50:
        raise ValueError("recall limit must be between 1 and 50")
    pattern = f"%{normalized.replace('%', r'\%').replace('_', r'\_')}%"
    async with pool.connection() as conn:
        exists = await conn.execute(
            "select 1 from invocation where id = %s and workspace = %s",
            (invocation_id, workspace),
        )
        if await exists.fetchone() is None:
            return {"hits": [], "not_found": True}
        events = await conn.execute(
            """
            select id, ordinal, kind, payload, payload_sha256, created_at
            from invocation_event
            where invocation_id = %s and workspace = %s
              and (kind ilike %s escape '\\' or payload::text ilike %s escape '\\')
            order by ordinal desc
            limit %s
            """,
            (invocation_id, workspace, pattern, pattern, limit),
        )
        event_rows = await events.fetchall()
        nodes = await conn.execute(
            """
            select id, kind, start_ordinal, end_ordinal, content, created_at
            from invocation_memory_node
            where invocation_id = %s and workspace = %s
              and content::text ilike %s escape '\\'
            order by created_at desc
            limit %s
            """,
            (invocation_id, workspace, pattern, limit),
        )
        node_rows = await nodes.fetchall()
    hits = [
        {
            "kind": "event",
            "id": str(row["id"]),
            "ordinal": int(row["ordinal"]),
            "event_kind": row["kind"],
            "payload": row["payload"],
            "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in event_rows
    ]
    hits.extend(
        {
            "kind": "memory_node",
            "id": str(row["id"]),
            "memory_kind": row["kind"],
            "start_ordinal": int(row["start_ordinal"]),
            "end_ordinal": int(row["end_ordinal"]),
            "content": row["content"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in node_rows
    )
    return {"hits": hits[:limit], "not_found": False}


__all__ = [
    "ContextPressure",
    "persist_completion_memory_tx",
    "read_memory_nodes",
    "recall_invocation",
]
