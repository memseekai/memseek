"""Per-stimulus derive deadlines, a durable cron checkpoint, and queue indexes.

Revision ID: 0013_scheduling_state
Revises: 0012_history_lookup_indexes
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_scheduling_state"
down_revision: str | None = "0012_history_lookup_indexes"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Give the scheduler its own state instead of inferring it from job rows.

    ``job.stimuli`` maps each coalesced derive reason to its own deadline, so
    a settle-style stimulus extends only its own deadline and ``run_after``
    stays the earliest one (spec 11.4).  ``job.unready_since`` bounds how long a
    not-ready job may hold its mailbox.  ``cron_checkpoint`` survives job-table
    retention, which the checkpoint previously could not.
    """

    op.execute(
        """
        alter table job
          add column stimuli jsonb not null default '{}'
            check (jsonb_typeof(stimuli) = 'object'),
          add column unready_since timestamptz
        """
    )
    op.execute(
        """
        create table cron_checkpoint (
          workspace    text not null references workspace(id) on delete cascade,
          trigger      text not null,
          scheduled_at timestamptz not null,
          primary key (workspace, trigger)
        )
        """
    )
    op.execute(
        """
        insert into cron_checkpoint (workspace, trigger, scheduled_at)
        select workspace, payload->>'trigger', max((payload->>'scheduled_at')::timestamptz)
        from job
        where kind = 'cron_scan'
          and payload ? 'trigger'
          and payload ? 'scheduled_at'
        group by workspace, payload->>'trigger'
        """
    )
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists job_cron_checkpoint")
        op.execute(
            """
            create index concurrently if not exists job_claim
              on job (kind, run_after, created_at, id)
              where done_at is null and dead_at is null
            """
        )
        op.execute("drop index concurrently if exists job_ready")
        op.execute(
            """
            create index concurrently if not exists job_lease_expiry
              on job (lease_until)
              where done_at is null and dead_at is null and lease_until is not null
            """
        )
        op.execute(
            """
            create index concurrently if not exists job_finished_at
              on job (coalesce(done_at, dead_at))
              where done_at is not null or dead_at is not null
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists job_finished_at")
        op.execute("drop index concurrently if exists job_lease_expiry")
        op.execute(
            """
            create index concurrently if not exists job_ready
              on job (run_after, created_at)
              where done_at is null and dead_at is null
            """
        )
        op.execute("drop index concurrently if exists job_claim")
        op.execute(
            """
            create index concurrently if not exists job_cron_checkpoint
              on job (workspace, derivation, (payload->>'trigger'), (payload->>'scheduled_at'))
              where kind = 'cron_scan'
            """
        )
    op.execute("drop table cron_checkpoint")
    op.execute("alter table job drop column unready_since, drop column stimuli")
