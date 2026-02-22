"""Add failed_segments dead-letter table (Phase 5).

Revision ID: 003
Revises: 002
Create Date: 2026-02-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "failed_segments" not in inspector.get_table_names():
        op.create_table(
            "failed_segments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("audio_path", sa.String, nullable=False),
            sa.Column("error", sa.Text, nullable=False),
            sa.Column("attempts", sa.Integer, default=1),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("failed_segments")
