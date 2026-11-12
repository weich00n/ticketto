# pylint: disable=invalid-name,no-member
"""Allow orders without hold_id or admission_id

Checkout may confirm an order without an admission_id, and Ticket only records these two IDs,
so they become nullable. Additive: the previous image still runs against this schema.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: a plain ALTER on MySQL, a table copy on SQLite (used by the tests).
    with op.batch_alter_table("ticket_orders") as batch:
        batch.alter_column("hold_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("admission_id", existing_type=sa.String(36), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("ticket_orders") as batch:
        batch.alter_column("admission_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("hold_id", existing_type=sa.String(36), nullable=False)
