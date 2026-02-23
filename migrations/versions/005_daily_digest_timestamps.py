"""005 — Add created_at and updated_at to daily_digests.

Revision ID: 005
Revises: 004
Create Date: 2026-02-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("daily_digests")}
    for col_name in ("created_at", "updated_at"):
        if col_name not in existing_cols:
            op.add_column("daily_digests", sa.Column(col_name, sa.DateTime))


def downgrade() -> None:
    # SQLite does not support DROP COLUMN in older versions; no-op downgrade
    pass
