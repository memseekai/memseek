"""Stage embeddings for a second space so the embedding model can be changed.

``record.embedding`` holds exactly one vector per record, in the deployment's
active space.  That makes changing the embedding model all-or-nothing and
irreversible: there is nowhere to put the new vectors while the old ones still
serve reads, and nowhere to keep the old ones once they are replaced.

This table is that somewhere.  It holds embeddings for spaces that are not the
active one, which is what allows a model migration to be prepared in the
background, verified for coverage, cut over in bounded batches, and rolled back
afterwards.  It never holds record text.

Revision ID: 0008_embedding_spaces
Revises: 0007_annotation_backfill
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision: str = "0008_embedding_spaces"
down_revision: str | None = "0007_annotation_backfill"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        create table record_embedding (
          record_id  uuid not null references record(id) on delete cascade,
          space      text not null check (space ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
          embedding  vector(1536) not null,
          -- The provider:model identity that produced this vector, so a staged
          -- space can be audited before anything is cut over to it.
          resolved   text not null,
          created_at timestamptz not null default now(),
          primary key (record_id, space)
        );

        create index record_embedding_by_space on record_embedding (space);
        create index record_embedding_vec
          on record_embedding using hnsw (embedding vector_cosine_ops);
        """
    )


def downgrade() -> None:
    op.execute("drop table record_embedding")
