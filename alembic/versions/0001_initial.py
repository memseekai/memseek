"""Create the normative Memseek schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-15
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op
from memseek.migrations import verify_normative_migration

revision: str = "0001_initial"
down_revision: None = None
branch_labels: None = None
depends_on: None = None


def _migration_directory() -> Path:
    configured = context.config.attributes.get("normative_migrations_path")
    if configured is not None:
        return Path(str(configured))
    return Path(context.config.get_main_option("normative_migrations_path"))


def upgrade() -> None:
    """Execute the immutable SQL supplied by specification v3.2."""

    path = verify_normative_migration(_migration_directory())
    migration_sql = path.read_text(encoding="utf-8")
    if context.is_offline_mode():
        op.execute(migration_sql)
    else:
        op.get_bind().exec_driver_sql(
            migration_sql,
            execution_options={"no_parameters": True},
        )


def downgrade() -> None:
    """Remove Memseek-owned objects while retaining the shared vector extension."""

    op.drop_table("job")
    op.drop_table("cursor")
    op.drop_table("record")
    op.drop_table("workspace")
