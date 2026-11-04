"""bookings payments webhook events

Revision ID: 654739fe891b
Revises: 84e2e5d03d60
Create Date: 2026-11-04 16:14:57.055067
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "654739fe891b"
down_revision: str | None = "84e2e5d03d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("provider_event_id", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("provider_event_id", name=op.f("pk_webhook_events")),
    )
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_zone_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("vehicle_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("price_minor >= 0", name=op.f("ck_bookings_price_not_negative")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_bookings_event_id_events"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["event_zone_id"],
            ["event_zones.id"],
            name=op.f("fk_bookings_event_zone_id_event_zones"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_bookings_space_id_spaces"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_bookings_user_id_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["vehicle_id"], ["vehicles.id"], name=op.f("fk_bookings_vehicle_id_vehicles"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_bookings_idempotency_key")),
        sa.CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'CONFIRMED', 'EXPIRED', 'CANCELLED')", name=op.f("ck_bookings_status")
        ),
    )
    op.create_index(
        "ix_bookings_pending_expiry",
        "bookings",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING_PAYMENT'"),
    )
    op.create_index(op.f("ix_bookings_user_id"), "bookings", ["user_id"], unique=False)
    op.create_index(
        "uq_bookings_active_space",
        "bookings",
        ["event_id", "space_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL AND status IN ('PENDING_PAYMENT', 'CONFIRMED')"),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("booking_id", sa.Uuid(), nullable=True),
        sa.Column("ticket_id", sa.Uuid(), nullable=True),
        sa.Column("passage_id", sa.Uuid(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("provider_ref", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["bookings.id"], name=op.f("fk_payments_booking_id_bookings"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["passage_id"], ["passages.id"], name=op.f("fk_payments_passage_id_passages"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_payments_ticket_id_tickets"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payments_idempotency_key")),
        sa.CheckConstraint("kind IN ('BOOKING', 'EXIT_FEE', 'LOST_TICKET')", name=op.f("ck_payments_kind")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'REFUNDED')", name=op.f("ck_payments_status")
        ),
    )
    op.create_index(op.f("ix_payments_booking_id"), "payments", ["booking_id"], unique=False)
    op.create_index(op.f("ix_payments_provider_ref"), "payments", ["provider_ref"], unique=False)
    op.create_index(op.f("ix_payments_ticket_id"), "payments", ["ticket_id"], unique=False)
    op.create_index(
        "ix_payments_unresolved",
        "payments",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'UNKNOWN')"),
    )
    op.create_foreign_key(
        op.f("fk_tickets_booking_id_bookings"), "tickets", "bookings", ["booking_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_tickets_booking_id_bookings"), "tickets", type_="foreignkey")
    op.drop_index(
        "ix_payments_unresolved", table_name="payments", postgresql_where=sa.text("status IN ('PENDING', 'UNKNOWN')")
    )
    op.drop_index(op.f("ix_payments_ticket_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_provider_ref"), table_name="payments")
    op.drop_index(op.f("ix_payments_booking_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(
        "uq_bookings_active_space",
        table_name="bookings",
        postgresql_where=sa.text("space_id IS NOT NULL AND status IN ('PENDING_PAYMENT', 'CONFIRMED')"),
    )
    op.drop_index(op.f("ix_bookings_user_id"), table_name="bookings")
    op.drop_index(
        "ix_bookings_pending_expiry", table_name="bookings", postgresql_where=sa.text("status = 'PENDING_PAYMENT'")
    )
    op.drop_table("bookings")
    op.drop_table("webhook_events")
