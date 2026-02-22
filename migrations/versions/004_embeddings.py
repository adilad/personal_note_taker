"""004 — segment_embeddings table for semantic search via NumPy cosine similarity.

Embeddings are stored as raw float32 BLOBs and searched in-memory with NumPy.
This works on all platforms without requiring SQLite extension loading.

Revision ID: 004
Revises: 003
Create Date: 2026-02-21
"""
from __future__ import annotations

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_embeddings (
            segment_id INTEGER PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
            embedding  BLOB NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_seg_emb_sid ON segment_embeddings(segment_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_seg_emb_sid")
    op.execute("DROP TABLE IF EXISTS segment_embeddings")
