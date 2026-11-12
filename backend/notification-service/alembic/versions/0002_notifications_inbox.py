# pylint: disable=no-member,invalid-name  # alembic.op is populated at runtime; Alembic requires these names
"""The in-app inbox: who each notification belongs to (Cognito sub) and when it was read

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rows queued before this release have no sub, and so never show in the app.
    with op.batch_alter_table("notifications") as batch:
        batch.add_column(sa.Column("cognito_sub", sa.String(64)))
        batch.add_column(sa.Column("read_at", sa.DateTime()))
    op.create_index("ix_notifications_inbox", "notifications", ["cognito_sub", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("read_at")
        batch.drop_column("cognito_sub")
