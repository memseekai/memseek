"""Add the trusted scheduled tombstone-retention job lane.

Revision ID: 0005_retention_purge
Revises: 0004_graph_edges_indexes
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_retention_purge"
down_revision: str | None = "0004_graph_edges_indexes"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check
          check (kind in ('derive', 'cron_scan', 'retention_purge', 'index_upsert', 'index_delete'));

        alter table job drop constraint job_check;
        alter table job add constraint job_shape_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind = 'retention_purge' and derivation is null and entity is null) or
          (kind in ('index_upsert', 'index_delete') and derivation is null and entity is null)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        delete from job where kind = 'retention_purge';
        alter table job drop constraint job_shape_check;
        alter table job add constraint job_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind in ('index_upsert', 'index_delete') and derivation is null and entity is null)
        );

        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check
          check (kind in ('derive', 'cron_scan', 'index_upsert', 'index_delete'));
        """
    )
