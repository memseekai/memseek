"""Reject updates to the columns that define a record's lineage.

Revision ID: 0011_record_lineage_immutable
Revises: 0010_computer_invocations
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision: str = "0011_record_lineage_immutable"
down_revision: str | None = "0010_computer_invocations"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Make id, workspace, seq, and derived_from write-once.

    The insert boundary proves each parent exists, shares the workspace, and is
    older than its child.  That proof covers the whole lineage only while these
    columns never change after insert.
    """

    op.execute(
        """
        create function record_lineage_immutable() returns trigger
        language plpgsql as $$
        begin
          raise exception 'record lineage columns are immutable'
            using errcode = 'integrity_constraint_violation';
        end
        $$
        """
    )
    op.execute(
        """
        create trigger record_lineage_immutable
          before update of id, workspace, seq, derived_from on record
          for each row
          when (
            new.id is distinct from old.id
            or new.workspace is distinct from old.workspace
            or new.seq is distinct from old.seq
            or new.derived_from is distinct from old.derived_from
          )
          execute function record_lineage_immutable()
        """
    )


def downgrade() -> None:
    op.execute("drop trigger record_lineage_immutable on record")
    op.execute("drop function record_lineage_immutable()")
