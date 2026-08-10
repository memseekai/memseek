"""Register one lightweight correlation handle per externally used artifact render.

An artifact use is operational metadata, not canonical history: it holds
identities, hashes, and the resolved learning target, and it expires.  It
deliberately has no column able to hold rendered content, prompt text, model
output, tool calls, or trace spans.

Revision ID: 0006_artifact_use
Revises: 0005_retention_purge
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_artifact_use"
down_revision: str | None = "0005_retention_purge"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        create table artifact_use (
          id               uuid primary key default gen_random_uuid(),
          workspace        text not null references workspace(id) on delete cascade,
          artifact_name    text not null check (artifact_name ~ '^[a-z][a-z0-9._-]{0,63}$'),
          artifact_version int not null check (artifact_version >= 1),
          definition_hash  text not null check (definition_hash ~ '^[0-9a-f]{64}$'),
          render_sha256    text not null check (render_sha256 ~ '^[0-9a-f]{64}$'),
          learning_target  jsonb
                           check (learning_target is null
                                  or jsonb_typeof(learning_target) = 'object'),
          snapshot_id      uuid references record(id) on delete set null,
          created_at       timestamptz not null default now(),
          -- An expiry already in the past is a valid state: shortening retention
          -- must be able to retire handles that were registered under a longer
          -- window, so this deliberately has no ordering constraint.
          expires_at       timestamptz not null
        );
        create index artifact_use_expiry on artifact_use (expires_at);
        create index artifact_use_artifact
          on artifact_use (workspace, artifact_name, artifact_version, created_at desc);
        create index artifact_use_snapshot
          on artifact_use (snapshot_id) where snapshot_id is not null;
        """
    )


def downgrade() -> None:
    op.execute("drop table artifact_use")
