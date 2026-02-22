"""Add FTS5 virtual table for full-text search.

Revision ID: 002
Revises: 001
Create Date: 2026-02-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Check if FTS5 table already exists
    tables = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='segments_fts'")
    ).fetchall()

    if not tables:
        bind.execute(sa.text(
            """
            CREATE VIRTUAL TABLE segments_fts USING fts5(
                transcript,
                summary,
                keywords,
                content='segments',
                content_rowid='id'
            )
            """
        ))
        # Populate from existing data
        bind.execute(sa.text(
            """
            INSERT INTO segments_fts(rowid, transcript, summary, keywords)
            SELECT id, COALESCE(transcript,''), COALESCE(summary,''), COALESCE(keywords,'')
            FROM segments
            WHERE deleted_at IS NULL
            """
        ))

    # Triggers to keep FTS in sync
    triggers = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
            "('segments_ai','segments_ad','segments_au')"
        )
    ).fetchall()
    trigger_names = {r[0] for r in triggers}

    if "segments_ai" not in trigger_names:
        bind.execute(sa.text(
            """
            CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
              INSERT INTO segments_fts(rowid, transcript, summary, keywords)
              VALUES (new.id, COALESCE(new.transcript,''), COALESCE(new.summary,''), COALESCE(new.keywords,''));
            END
            """
        ))
    if "segments_ad" not in trigger_names:
        bind.execute(sa.text(
            """
            CREATE TRIGGER segments_ad AFTER DELETE ON segments BEGIN
              INSERT INTO segments_fts(segments_fts, rowid, transcript, summary, keywords)
              VALUES ('delete', old.id, COALESCE(old.transcript,''), COALESCE(old.summary,''), COALESCE(old.keywords,''));
            END
            """
        ))
    if "segments_au" not in trigger_names:
        bind.execute(sa.text(
            """
            CREATE TRIGGER segments_au AFTER UPDATE ON segments BEGIN
              INSERT INTO segments_fts(segments_fts, rowid, transcript, summary, keywords)
              VALUES ('delete', old.id, COALESCE(old.transcript,''), COALESCE(old.summary,''), COALESCE(old.keywords,''));
              INSERT INTO segments_fts(rowid, transcript, summary, keywords)
              VALUES (new.id, COALESCE(new.transcript,''), COALESCE(new.summary,''), COALESCE(new.keywords,''));
            END
            """
        ))


def downgrade() -> None:
    bind = op.get_bind()
    for trigger in ("segments_ai", "segments_ad", "segments_au"):
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
    bind.execute(sa.text("DROP TABLE IF EXISTS segments_fts"))
