"""Small immutable runtime value objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

JobKind = Literal[
    "derive",
    "cron_scan",
    "retention_purge",
    "annotation_backfill",
    "index_upsert",
    "index_delete",
]


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A snapshot of a job owned by one unique lease token."""

    id: UUID
    workspace: str
    kind: JobKind
    derivation: str | None
    entity: str | None
    payload: Mapping[str, Any]
    dedupe_key: str | None
    run_after: datetime
    attempts: int
    lease_until: datetime
    claim_token: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobTransition:
    """The durable result of releasing a claimed job."""

    dead: bool
    attempts: int
    run_after: datetime | None
    dead_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkspaceCredential:
    """A one-time workspace credential returned only by creation."""

    workspace: str
    api_key: str


class LeaseLost(RuntimeError):
    """Raised when a job token no longer owns a live lease."""
