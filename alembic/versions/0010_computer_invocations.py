"""Add Computer sessions and durable invocation journals.

Revision ID: 0010_computer_invocations
Revises: 0009_general_graph_indexes
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_computer_invocations"
down_revision: str | None = "0009_general_graph_indexes"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        create table computer_session (
          id                uuid primary key,
          workspace         text not null references workspace(id),
          entity            text not null,
          computer_ref      text not null,
          computer_hash     text not null check (computer_hash ~ '^[0-9a-f]{64}$'),
          provider          text not null,
          definition_refs   jsonb not null check (jsonb_typeof(definition_refs) = 'object'),
          parent_session_id uuid references computer_session(id),
          provider_handle   text,
          status            text not null default 'active'
                            check (status in ('active', 'expired', 'destroyed')),
          expires_at        timestamptz,
          created_at        timestamptz not null default now(),
          updated_at        timestamptz not null default now()
        );
        create index computer_session_entity
          on computer_session (workspace, entity, created_at desc);

        create table invocation (
          id                uuid primary key,
          workspace         text not null references workspace(id),
          entity            text not null,
          session_id        uuid not null references computer_session(id),
          executor_kind     text not null check (executor_kind in ('program', 'agent')),
          executor_ref      text not null,
          context_policy_ref text,
          definition_refs   jsonb not null check (jsonb_typeof(definition_refs) = 'object'),
          task              jsonb not null check (jsonb_typeof(task) = 'object'),
          status            text not null default 'queued' check (
                              status in ('queued', 'running', 'awaiting_input', 'succeeded',
                                         'failed', 'cancelled', 'context_exhausted')
                            ),
          idempotency_key   text,
          result            jsonb,
          error_kind        text,
          error             text,
          started_at        timestamptz,
          completed_at      timestamptz,
          created_at        timestamptz not null default now(),
          updated_at        timestamptz not null default now(),
          check (result is null or jsonb_typeof(result) = 'object')
        );
        create unique index invocation_idempotency
          on invocation (workspace, idempotency_key) where idempotency_key is not null;
        create index invocation_entity
          on invocation (workspace, entity, created_at desc);
        create index invocation_session
          on invocation (session_id, created_at desc);

        create table invocation_event (
          invocation_id uuid not null references invocation(id) on delete cascade,
          ordinal       bigint not null check (ordinal > 0),
          id            uuid not null unique,
          workspace     text not null references workspace(id),
          kind          text not null,
          payload       jsonb not null check (jsonb_typeof(payload) = 'object'),
          payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
          created_at    timestamptz not null default now(),
          primary key (invocation_id, ordinal)
        );
        create index invocation_event_workspace
          on invocation_event (workspace, invocation_id, ordinal);

        create table invocation_memory_node (
          id             uuid primary key,
          workspace      text not null references workspace(id),
          invocation_id  uuid not null references invocation(id) on delete cascade,
          kind           text not null check (kind in ('working_brief', 'episode_receipt')),
          start_ordinal  bigint not null check (start_ordinal > 0),
          end_ordinal    bigint not null check (end_ordinal >= start_ordinal),
          level          int not null default 0 check (level >= 0),
          content        jsonb not null check (jsonb_typeof(content) = 'object'),
          source_event_ids uuid[] not null default '{}',
          created_at     timestamptz not null default now()
        );
        create index invocation_memory_range
          on invocation_memory_node (invocation_id, start_ordinal, end_ordinal, level);

        create table invocation_artifact (
          id             uuid primary key,
          workspace      text not null references workspace(id),
          invocation_id  uuid not null references invocation(id) on delete cascade,
          path           text not null,
          sha256         text not null check (sha256 ~ '^[0-9a-f]{64}$'),
          size_bytes     bigint not null check (size_bytes >= 0),
          mime_type      text,
          storage_uri    text,
          source_event_id uuid references invocation_event(id),
          preserved      boolean not null default false,
          created_at     timestamptz not null default now(),
          unique (invocation_id, path, sha256)
        );

        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check check (
          kind in ('derive', 'cron_scan', 'retention_purge', 'annotation_backfill',
                   'invocation', 'index_upsert', 'index_delete')
        );
        alter table job drop constraint job_shape_check;
        alter table job add constraint job_shape_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind in ('retention_purge', 'annotation_backfill', 'index_upsert', 'index_delete')
            and derivation is null and entity is null) or
          (kind = 'invocation' and derivation is null and entity is not null)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        delete from job where kind = 'invocation';
        alter table job drop constraint job_shape_check;
        alter table job add constraint job_shape_check check (
          (kind = 'derive' and derivation is not null and entity is not null) or
          (kind = 'cron_scan' and derivation is not null and entity is null) or
          (kind in ('retention_purge', 'annotation_backfill', 'index_upsert', 'index_delete')
            and derivation is null and entity is null)
        );
        alter table job drop constraint job_kind_check;
        alter table job add constraint job_kind_check check (
          kind in ('derive', 'cron_scan', 'retention_purge', 'annotation_backfill',
                   'index_upsert', 'index_delete')
        );
        drop table invocation_artifact;
        drop table invocation_memory_node;
        drop table invocation_event;
        drop table invocation;
        drop table computer_session;
        """
    )
