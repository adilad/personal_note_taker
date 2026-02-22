"""Initial schema — creates tables or adds missing columns to existing DB.

Revision ID: 001
Revises:
Create Date: 2026-02-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # --- segments ---
    if "segments" not in tables:
        op.create_table(
            "segments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("start_ts", sa.String, nullable=False),
            sa.Column("end_ts", sa.String, nullable=False),
            sa.Column("duration_sec", sa.Float, default=0.0),
            sa.Column("audio_path", sa.String),
            sa.Column("audio_key", sa.String),
            sa.Column("transcript", sa.Text, default=""),
            sa.Column("summary", sa.Text, default=""),
            sa.Column("keywords", sa.Text, default=""),
            sa.Column("speakers", sa.Text, default=""),
            sa.Column("participants", sa.Text, default=""),
            sa.Column("category", sa.String, default=""),
            sa.Column("action_items", sa.Text, default=""),
            sa.Column("questions", sa.Text, default=""),
            sa.Column("sentiment", sa.String, default=""),
            sa.Column("important", sa.Boolean, default=False),
            sa.Column("tags", sa.Text, default="[]"),
            sa.Column("word_count", sa.Integer, default=0),
            sa.Column("char_count", sa.Integer, default=0),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("deleted_at", sa.DateTime, nullable=True),
        )
        op.create_index("idx_segments_start", "segments", ["start_ts"])
        op.create_index("idx_segments_end", "segments", ["end_ts"])
        op.create_index("idx_segments_important", "segments", ["important"])
        op.create_index("idx_segments_deleted", "segments", ["deleted_at"])
    else:
        # Migrate existing table: add new columns if absent
        existing_cols = {c["name"] for c in inspector.get_columns("segments")}
        new_cols = {
            "audio_key": sa.String,
            "speakers": sa.Text,
            "participants": sa.Text,
            "category": sa.String,
            "action_items": sa.Text,
            "questions": sa.Text,
            "sentiment": sa.String,
            "tags": sa.Text,
            "word_count": sa.Integer,
            "char_count": sa.Integer,
            "created_at": sa.DateTime,
            "updated_at": sa.DateTime,
            "deleted_at": sa.DateTime,
        }
        for col_name, col_type in new_cols.items():
            if col_name not in existing_cols:
                op.add_column("segments", sa.Column(col_name, col_type))

        # Update tags default for existing rows
        bind.execute(sa.text("UPDATE segments SET tags='[]' WHERE tags IS NULL"))

    # --- hourly_digests ---
    if "hourly_digests" not in tables:
        op.create_table(
            "hourly_digests",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("hour_start", sa.String, unique=True, nullable=False),
            sa.Column("hour_end", sa.String, nullable=False),
            sa.Column("summary", sa.Text, default=""),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        )

    # --- daily_digests ---
    if "daily_digests" not in tables:
        op.create_table(
            "daily_digests",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("date", sa.String, unique=True, nullable=False),
            sa.Column("summary", sa.Text, default=""),
            sa.Column("action_items", sa.Text, default=""),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        )
    else:
        existing_cols = {c["name"] for c in inspector.get_columns("daily_digests")}
        if "action_items" not in existing_cols:
            op.add_column("daily_digests", sa.Column("action_items", sa.Text))


def downgrade() -> None:
    op.drop_table("segments")
    op.drop_table("hourly_digests")
    op.drop_table("daily_digests")
