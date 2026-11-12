# pylint: disable=no-member,invalid-name
"""one refund per order

An order has one payment, so it gets at most one (full) refund. The old
(order_id, reason) key let two causes - an event cancelled mid-confirmation, then
reservation's RefundRequired - record two refunds for the same money.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the new key first: the order_id foreign key needs an index at every step.
    op.create_unique_constraint("uq_refunds_order_id", "refunds", ["order_id"])
    op.drop_constraint("uq_refunds_order_reason", "refunds", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_refunds_order_reason", "refunds", ["order_id", "reason"])
    op.drop_constraint("uq_refunds_order_id", "refunds", type_="unique")
