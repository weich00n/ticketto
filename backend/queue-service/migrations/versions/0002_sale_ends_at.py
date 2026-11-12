"""Add queue_configs.sale_ends_at (optional end of sale).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "queue_configs",
        sa.Column("sale_ends_at", mysql.DATETIME(fsp=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("queue_configs", "sale_ends_at")
