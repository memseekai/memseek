"""Retire the removed built-in contradiction job lane.

Revision ID: 0003_public_relations
Revises: 0002_workspace_catalog
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_public_relations"
down_revision: str | None = "0002_workspace_catalog"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # Keep historical run/relation records intact. Only obsolete active jobs
    # would otherwise be claimed and released forever after their synthesized
    # processor definition disappears.
    op.execute(
        """
        update job
        set dead_at = clock_timestamp(),
            lease_until = null,
            locked_by = null,
            last_error_kind = 'superseded',
            last_error = 'replaced by YAML derivation contradiction'
        where kind = 'derive'
          and derivation = 'contradiction_relation'
          and done_at is null
          and dead_at is null
        """
    )


def downgrade() -> None:
    # The removed synthesized processor cannot safely resume old jobs, and a
    # downgrade must not guess which rows were active before this migration.
    pass
