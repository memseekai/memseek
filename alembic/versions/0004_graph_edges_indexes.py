"""Index bounded structural graph hops.

Revision ID: 0004_graph_edges_indexes
Revises: 0003_public_relations
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_graph_edges_indexes"
down_revision: str | None = "0003_public_relations"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Make each subject/object graph hop an indexed partial lookup."""

    op.execute(
        """
        create index record_graph_edges_out
          on record (workspace, (content->>'subject'), (content->>'object'), id)
          where collection = 'edges'
            and status = 'active'
            and enriched_at is not null
            and not coalesce((content->>'tombstone')::boolean, false)
        """
    )
    op.execute(
        """
        create index record_graph_edges_in
          on record (workspace, (content->>'object'), (content->>'subject'), id)
          where collection = 'edges'
            and status = 'active'
            and enriched_at is not null
            and not coalesce((content->>'tombstone')::boolean, false)
        """
    )


def downgrade() -> None:
    op.execute("drop index record_graph_edges_in")
    op.execute("drop index record_graph_edges_out")
