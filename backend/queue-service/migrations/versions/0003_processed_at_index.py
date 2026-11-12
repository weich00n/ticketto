"""Index processed_messages.processed_at for the retention job.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("idx_processed_at", "processed_messages", ["processed_at"])


def downgrade() -> None:
    op.drop_index("idx_processed_at", table_name="processed_messages")
