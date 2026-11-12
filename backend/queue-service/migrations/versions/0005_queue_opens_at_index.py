"""Index queue_configs (status, queue_opens_at).

queue_opens_at is now sent by Event (queue_join_opens_at in EventPublished)
instead of being release_at - 15 min, so the admitter finds due events by it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("idx_status_opens", "queue_configs", ["status", "queue_opens_at"])


def downgrade() -> None:
    op.drop_index("idx_status_opens", table_name="queue_configs")
