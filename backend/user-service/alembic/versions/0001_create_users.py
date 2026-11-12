# pylint: disable=no-member,invalid-name  # alembic.op is populated at runtime; Alembic requires these names
"""Create users table

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Matches the schema in cognito-settings.md §5. MySQL-only (RDS and the local container).
    op.create_table(
        "users",
        sa.Column("user_id", sa.CHAR(36), primary_key=True),
        sa.Column("cognito_sub", sa.CHAR(36), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("buyer", "organiser", name="user_role"),
            nullable=False,
            server_default="buyer",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("cognito_sub", name="uq_users_cognito_sub"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("users")
