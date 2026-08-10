"""Index structural graph hops for every edge collection.

Revision ID: 0009_general_graph_indexes
Revises: 0008_embedding_spaces
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_general_graph_indexes"
down_revision: str | None = "0008_embedding_spaces"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Replace the example-specific partial indexes with collection-aware ones."""

    op.execute("drop index record_graph_edges_in")
    op.execute("drop index record_graph_edges_out")
    op.execute(
        """
        create index record_graph_edges_out
          on record (workspace, collection, (content->>'subject'), (content->>'object'), id)
          where status = 'active'
            and enriched_at is not null
            and not coalesce((content->>'tombstone')::boolean, false)
        """
    )
    op.execute(
        """
        create index record_graph_edges_in
          on record (workspace, collection, (content->>'object'), (content->>'subject'), id)
          where status = 'active'
            and enriched_at is not null
            and not coalesce((content->>'tombstone')::boolean, false)
        """
    )


def downgrade() -> None:
    """Restore the original indexes for the conventional ``edges`` collection."""

    op.execute("drop index record_graph_edges_in")
    op.execute("drop index record_graph_edges_out")
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
