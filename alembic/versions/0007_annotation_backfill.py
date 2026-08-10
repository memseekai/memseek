"""Register the annotation-backfill lane and its durable progress handle.

A backfill applies one processor to records that already exist.  It is a
long-running, resumable, cancellable operation with real cost, so it gets a
durable row rather than living only in a job payload: an author needs to see
progress, stop it, and know afterwards that it finished.

The unique partial index makes one live backfill per (collection version,
processor) target an invariant rather than a convention, so two operators cannot
race the same work.

Revision ID: 0007_annotation_backfill
Revises: 0006_artifact_use
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_annotation_backfill"
down_revision: str | None = "0006_artifact_use"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        create table backfill (
          id                 uuid primary key default gen_random_uuid(),
          workspace          text not null references workspace(id) on delete cascade,
          collection         text not null check (collection ~ '^[a-z][a-z0-9._-]{0,63}$'),
          collection_version int not null check (collection_version >= 1),
          processor          text not null check (processor ~ '^[a-z][a-z0-9_]{0,31}$'),
          -- The resumption point: every record at or below this sequence has been
          -- considered exactly once.
          cursor_seq         bigint not null default 0 check (cursor_seq >= 0),
          scanned            int not null default 0 check (scanned >= 0),
          annotated          int not null default 0 check (annotated >= 0),
          max_rows           int check (max_rows is null or max_rows > 0),
          state              text not null default 'queued'
                             check (state in ('queued', 'running', 'done', 'cancelled', 'failed')),
          last_error         text,
          created_at         timestamptz not null default now(),
          updated_at         timestamptz not null default now(),
          completed_at       timestamptz,
          check ((state in ('done', 'cancelled', 'failed')) = (completed_at is not null))
        );

        create index backfill_workspace on backfill (workspace, created_at desc);
        create unique index backfill_live
          on backfill (workspace, collection, collection_version, processor)
          where state in ('queued', 'running');

        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check
          check (kind in ('derive', 'cron_scan', 'retention_purge', 'annotation_backfill',
                          'index_upsert', 'index_delete'));

        alter table job drop constraint job_shape_check;
        alter table job add constraint job_shape_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind = 'retention_purge' and derivation is null and entity is null) or
          (kind = 'annotation_backfill' and derivation is null and entity is null) or
          (kind in ('index_upsert', 'index_delete') and derivation is null and entity is null)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        delete from job where kind = 'annotation_backfill';
        drop table backfill;

        alter table job drop constraint job_shape_check;
        alter table job add constraint job_shape_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind = 'retention_purge' and derivation is null and entity is null) or
          (kind in ('index_upsert', 'index_delete') and derivation is null and entity is null)
        );

        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check
          check (kind in ('derive', 'cron_scan', 'retention_purge',
                          'index_upsert', 'index_delete'));
        """
    )
