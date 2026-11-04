"""tickets passages vehicles outbox

Revision ID: 84e2e5d03d60
Revises: f5f05a147022
Create Date: 2026-11-04 15:44:28.844712
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "84e2e5d03d60"
down_revision: str | None = "f5f05a147022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("routing_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("message_id", name=op.f("uq_outbox_message_id")),
    )
    op.create_index(
        "ix_outbox_unpublished", "outbox", ["id"], unique=False, postgresql_where=sa.text("published_at IS NULL")
    )
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plate_normalized", sa.String(length=16), nullable=False),
        sa.Column("plate_display", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_vehicles_owner_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vehicles")),
        sa.UniqueConstraint("plate_normalized", name=op.f("uq_vehicles_plate_normalized")),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=True),
        sa.Column("event_zone_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("vehicle_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("issued_by_device_id", sa.Uuid(), nullable=True),
        sa.Column("qr_s3_key", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status IN ('ENTERING', 'EXITING')) = (hold_expires_at IS NOT NULL)",
            name=op.f("ck_tickets_hold_only_while_moving"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('INSIDE', 'EXITING', 'EXITED') OR entered_at IS NOT NULL",
            name=op.f("ck_tickets_inside_has_entry"),
        ),
        sa.CheckConstraint("paid_minor >= 0", name=op.f("ck_tickets_paid_not_negative")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_tickets_event_id_events"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["event_zone_id"],
            ["event_zones.id"],
            name=op.f("fk_tickets_event_zone_id_event_zones"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_device_id"],
            ["devices.id"],
            name=op.f("fk_tickets_issued_by_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_tickets_space_id_spaces"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"], ["vehicles.id"], name=op.f("fk_tickets_vehicle_id_vehicles"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("booking_id", name=op.f("uq_tickets_booking_id")),
        sa.UniqueConstraint("code", name=op.f("uq_tickets_code")),
        sa.CheckConstraint("type IN ('PREBOOKED', 'DRIVE_UP')", name=op.f("ck_tickets_type")),
        sa.CheckConstraint(
            "status IN ('VALID', 'ENTERING', 'INSIDE', 'EXITING', 'EXITED', 'VOID', 'EXPIRED')",
            name=op.f("ck_tickets_status"),
        ),
    )
    op.create_index("ix_tickets_event_id_status", "tickets", ["event_id", "status"], unique=False)
    op.create_index(
        "uq_tickets_active_space",
        "tickets",
        ["event_id", "space_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL AND status NOT IN ('VOID', 'EXPIRED')"),
    )
    op.create_table(
        "passages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("gate_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("ticket_id", sa.Uuid(), nullable=True),
        sa.Column(
            "direction",
            sa.String(32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
        ),
        sa.Column(
            "reject_reason",
            sa.String(32),
            nullable=True,
        ),
        sa.Column("amount_due_minor", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("operator_user_id", sa.Uuid(), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(status = 'REJECTED') = (reject_reason IS NOT NULL)", name=op.f("ck_passages_reason_only_when_rejected")
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_passages_device_id_devices"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_passages_event_id_events"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["gate_id"], ["gates.id"], name=op.f("fk_passages_gate_id_gates"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["operator_user_id"], ["users.id"], name=op.f("fk_passages_operator_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_passages_ticket_id_tickets"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passages")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_passages_idempotency_key")),
        sa.CheckConstraint("direction IN ('ENTRY', 'EXIT')", name=op.f("ck_passages_direction")),
        sa.CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'AUTHORIZED', 'COMPLETED', 'EXPIRED', 'ABORTED', 'REJECTED')",
            name=op.f("ck_passages_status"),
        ),
        sa.CheckConstraint(
            "reject_reason IN ('UNKNOWN_TICKET', 'WRONG_EVENT', 'EVENT_NOT_LIVE', 'ALREADY_INSIDE', 'NOT_INSIDE', 'ALREADY_USED', 'TICKET_VOID', 'ENTRY_IN_PROGRESS', 'EXIT_IN_PROGRESS', 'CAR_PARK_FULL', 'DEVICE_NOT_ALLOWED')",
            name=op.f("ck_passages_reject_reason"),
        ),
    )
    op.create_index("ix_passages_event_id_completed_at", "passages", ["event_id", "completed_at"], unique=False)
    op.create_index(
        "ix_passages_open_deadline",
        "passages",
        ["status", "deadline_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('AUTHORIZED', 'PENDING_PAYMENT')"),
    )
    op.create_index(op.f("ix_passages_ticket_id"), "passages", ["ticket_id"], unique=False)
    op.create_index(
        "uq_events_one_live_per_venue", "events", ["venue_id"], unique=True, postgresql_where=sa.text("status = 'LIVE'")
    )


def downgrade() -> None:
    op.drop_index("uq_events_one_live_per_venue", table_name="events", postgresql_where=sa.text("status = 'LIVE'"))
    op.drop_index(op.f("ix_passages_ticket_id"), table_name="passages")
    op.drop_index(
        "ix_passages_open_deadline",
        table_name="passages",
        postgresql_where=sa.text("status IN ('AUTHORIZED', 'PENDING_PAYMENT')"),
    )
    op.drop_index("ix_passages_event_id_completed_at", table_name="passages")
    op.drop_table("passages")
    op.drop_index(
        "uq_tickets_active_space",
        table_name="tickets",
        postgresql_where=sa.text("space_id IS NOT NULL AND status NOT IN ('VOID', 'EXPIRED')"),
    )
    op.drop_index("ix_tickets_event_id_status", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("vehicles")
    op.drop_index("ix_outbox_unpublished", table_name="outbox", postgresql_where=sa.text("published_at IS NULL"))
    op.drop_table("outbox")
