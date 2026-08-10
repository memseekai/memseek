"""Single-record dereference with optional read-access touch."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.logging import log_event
from memseek.views.shared import record_detail

LOGGER = logging.getLogger(__name__)

_RECORD_COLUMNS = """
    id, seq, collection, collection_version, collection_hash, entity, key,
    type, status, content, embedding_space, scores, annotations,
    annotation_meta, enrichment_meta, enrichment_error, enriched_at, run_id,
    depth, derived_from, dedupe_key, occurred_at, created_at, last_accessed
"""


async def fetch_record(
    pool: DatabasePool,
    *,
    workspace: str,
    record_id: UUID,
    settings: Settings,
) -> dict[str, Any] | None:
    """Return the full canonical row, or ``None`` for missing/cross-workspace IDs.

    The workspace predicate makes a foreign record indistinguishable from a
    missing one.  A touch failure is logged and never fails the read because
    ``last_accessed`` is advisory retention metadata, not response content.
    """

    async with pool.connection() as conn:
        result = await conn.execute(
            f"select {_RECORD_COLUMNS} from record where workspace = %s and id = %s",
            (workspace, record_id),
        )
        row = await result.fetchone()
        if row is None:
            return None
        detail = record_detail(row)
        if settings.touch_on_read:
            try:
                await conn.execute(
                    "update record set last_accessed = now() where id = %s",
                    (record_id,),
                )
            except Exception as exc:
                log_event(
                    LOGGER,
                    "warning",
                    "reads.touch_failed",
                    workspace=workspace,
                    record_id=str(record_id),
                    exception_type=type(exc).__name__,
                )
        return detail


__all__ = ["fetch_record"]
