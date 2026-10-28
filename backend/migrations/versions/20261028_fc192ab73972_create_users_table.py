"""create users table

Revision ID: fc192ab73972
Revises:
Create Date: 2026-10-28 15:05:29.086535
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "fc192ab73972"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # citext (case-insensitive text) is a Postgres extension; autogenerate doesn't know about extensions.
    # Available on RDS too. IF NOT EXISTS: safe if it's already installed.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        # Written out as plain varchar + CHECK (ADR-023) instead of the model's sa.Enum(Role): a migration is a
        # frozen snapshot and must not change meaning when the Python enum gains a value later.
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint(
            "role IN ('ADMIN', 'EVENT_MANAGER', 'GATE_OPERATOR', 'PARKING_STAFF', 'DRIVER')",
            name=op.f("ck_users_role"),
        ),
    )


def downgrade() -> None:
    op.drop_table("users")
    # The citext extension is left installed on purpose: dropping it would break any other object using it.
