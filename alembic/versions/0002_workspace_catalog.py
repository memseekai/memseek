"""Persist one validated definition package per workspace.

Revision ID: 0002_workspace_catalog
Revises: 0001_initial
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_workspace_catalog"
down_revision: str | None = "0001_initial"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        create table workspace_catalog (
          workspace       text primary key references workspace(id) on delete cascade,
          package_name    text not null check (package_name ~ '^[a-z][a-z0-9._-]{0,63}$'),
          package_version text not null check (
            package_version ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?(\\+[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?$'
          ),
          catalog_hash    text not null check (catalog_hash ~ '^[0-9a-f]{64}$'),
          files           jsonb not null check (jsonb_typeof(files) = 'object'),
          created_at      timestamptz not null default now(),
          updated_at      timestamptz not null default now()
        );
        create index workspace_catalog_hash on workspace_catalog (catalog_hash);
        """
    )


def downgrade() -> None:
    op.execute("drop table workspace_catalog")
