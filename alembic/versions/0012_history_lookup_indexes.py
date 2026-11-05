"""Index the derive cursor and the job-history lookups.

Revision ID: 0012_history_lookup_indexes
Revises: 0011_record_lineage_immutable
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision: str = "0012_history_lookup_indexes"
down_revision: str | None = "0011_record_lineage_immutable"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Serve the cursor, cron checkpoint, and job-state reads from indexes.

    ``record_run_lookup`` keyed runs by ``content->>'derivation'``, which no run
    writes, so every cursor read scanned the entity's run history.
    """

    op.execute("drop index record_run_lookup")
    op.execute(
        """
        create index record_derive_cursor
          on record (workspace, entity, (content->>'processor'), seq desc)
          where collection = '_system'
            and type = 'run'
            and content->>'operation' = 'derive'
            and content->>'status' in ('ok', 'noop')
        """
    )
    op.execute(
        """
        create index job_cron_checkpoint
          on job (workspace, derivation, (payload->>'trigger'), (payload->>'scheduled_at'))
          where kind = 'cron_scan'
        """
    )
    op.execute(
        """
        create index job_derive_unfinished
          on job (workspace, derivation, entity, created_at desc, id)
          where kind = 'derive' and done_at is null
        """
    )


def downgrade() -> None:
    op.execute("drop index job_derive_unfinished")
    op.execute("drop index job_cron_checkpoint")
    op.execute("drop index record_derive_cursor")
    op.execute(
        """
        create index record_run_lookup
          on record (workspace, entity, ((content->>'derivation')), seq desc)
          where type = 'run'
        """
    )
