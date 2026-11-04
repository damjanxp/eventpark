"""venues devices events auth tables

Revision ID: f5f05a147022
Revises: fc192ab73972
Create Date: 2026-11-04 15:25:17.021938
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f5f05a147022"
down_revision: str | None = "fc192ab73972"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), server_default="", nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venues")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gates_open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gates_close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booking_opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booking_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("booking_opens_at < booking_closes_at", name=op.f("ck_events_booking_times_ordered")),
        sa.CheckConstraint("gates_open_at < gates_close_at", name=op.f("ck_events_gate_times_ordered")),
        sa.CheckConstraint("starts_at < ends_at", name=op.f("ck_events_event_times_ordered")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_events_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_events_venue_id_venues"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'LIVE', 'ENDED', 'ARCHIVED', 'CANCELLED')", name=op.f("ck_events_status")
        ),
    )
    op.create_index("ix_events_status", "events", ["status"], unique=False)
    op.create_index("ix_events_venue_id_starts_at", "events", ["venue_id", "starts_at"], unique=False)
    op.create_table(
        "gates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "direction",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("has_printer", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_terminal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("NOT (has_printer AND direction = 'EXIT')", name=op.f("ck_gates_printer_only_on_entry")),
        sa.CheckConstraint("NOT (has_terminal AND direction = 'ENTRY')", name=op.f("ck_gates_terminal_only_on_exit")),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_gates_venue_id_venues"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gates")),
        sa.UniqueConstraint("venue_id", "code", name=op.f("uq_gates_venue_id_code")),
        sa.CheckConstraint("direction IN ('ENTRY', 'EXIT')", name=op.f("ck_gates_direction")),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_refresh_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(op.f("ix_refresh_tokens_family_id"), "refresh_tokens", ["family_id"], unique=False)
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "zones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("total_capacity", sa.Integer(), nullable=False),
        sa.CheckConstraint("total_capacity > 0", name=op.f("ck_zones_total_capacity_positive")),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_zones_venue_id_venues"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zones")),
        sa.UniqueConstraint("venue_id", "code", name=op.f("uq_zones_venue_id_code")),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("gate_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_key_prefix", sa.String(length=32), nullable=False),
        sa.Column("api_key_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            server_default="OFFLINE",
            nullable=False,
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("firmware_version", sa.Text(), nullable=True),
        sa.Column("last_seen_ip", sa.Text(), nullable=True),
        sa.Column(
            "printer_status",
            sa.String(32),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["gate_id"], ["gates.id"], name=op.f("fk_devices_gate_id_gates"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("api_key_prefix", name=op.f("uq_devices_api_key_prefix")),
        sa.UniqueConstraint("gate_id", name=op.f("uq_devices_gate_id")),
        sa.CheckConstraint("status IN ('ONLINE', 'OFFLINE', 'DISABLED')", name=op.f("ck_devices_status")),
        sa.CheckConstraint(
            "printer_status IN ('OK', 'PAPER_LOW', 'PAPER_OUT')", name=op.f("ck_devices_printer_status")
        ),
    )
    op.create_table(
        "event_pricing",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("drive_up_free_minutes", sa.Integer(), nullable=False),
        sa.Column("drive_up_hourly_minor", sa.BigInteger(), nullable=False),
        sa.Column("drive_up_daily_cap_minor", sa.BigInteger(), nullable=False),
        sa.Column("lost_ticket_fee_minor", sa.BigInteger(), nullable=False),
        sa.Column("post_payment_exit_grace_minutes", sa.Integer(), nullable=False),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_event_pricing_currency_iso4217")),
        sa.CheckConstraint(
            "drive_up_free_minutes >= 0 AND drive_up_hourly_minor >= 0 AND drive_up_daily_cap_minor >= 0 AND lost_ticket_fee_minor >= 0 AND post_payment_exit_grace_minutes >= 0",
            name=op.f("ck_event_pricing_amounts_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_pricing_event_id_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_pricing")),
    )
    op.create_table(
        "event_zones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("prebook_quota", sa.Integer(), nullable=False),
        sa.Column("prebook_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("allocation_priority", sa.Integer(), nullable=False),
        sa.Column("prebook_held", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prebook_inside", sa.Integer(), server_default="0", nullable=False),
        sa.Column("drive_up_occupied", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("allocation_priority >= 0", name=op.f("ck_event_zones_priority_not_negative")),
        sa.CheckConstraint("capacity >= 0", name=op.f("ck_event_zones_capacity_not_negative")),
        sa.CheckConstraint(
            "drive_up_occupied BETWEEN 0 AND capacity - prebook_quota",
            name=op.f("ck_event_zones_drive_up_within_capacity"),
        ),
        sa.CheckConstraint(
            "prebook_held BETWEEN 0 AND prebook_quota", name=op.f("ck_event_zones_prebook_held_within_quota")
        ),
        sa.CheckConstraint(
            "prebook_inside BETWEEN 0 AND prebook_held", name=op.f("ck_event_zones_prebook_inside_within_held")
        ),
        sa.CheckConstraint("prebook_price_minor >= 0", name=op.f("ck_event_zones_price_not_negative")),
        sa.CheckConstraint("prebook_quota BETWEEN 0 AND capacity", name=op.f("ck_event_zones_quota_within_capacity")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_zones_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_event_zones_zone_id_zones"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_zones")),
        sa.UniqueConstraint("event_id", "zone_id", name=op.f("uq_event_zones_event_id_zone_id")),
    )
    op.create_table(
        "spaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], name=op.f("fk_spaces_zone_id_zones"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spaces")),
        sa.UniqueConstraint("zone_id", "label", name=op.f("uq_spaces_zone_id_label")),
        sa.CheckConstraint("kind IN ('VIP', 'RESERVED', 'ACCESSIBLE', 'EV')", name=op.f("ck_spaces_kind")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_device_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_device_id"], ["devices.id"], name=op.f("fk_audit_log_actor_device_id_devices"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_audit_log_actor_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"], unique=False)
    op.create_table(
        "device_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column(
            "command",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_device_commands_device_id_devices"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_device_commands_requested_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_commands")),
        sa.CheckConstraint("command IN ('OPEN_BARRIER', 'REBOOT')", name=op.f("ck_device_commands_command")),
    )
    op.create_index(
        "ix_device_commands_pending",
        "device_commands",
        ["device_id"],
        unique=False,
        postgresql_where=sa.text("acked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_commands_pending", table_name="device_commands", postgresql_where=sa.text("acked_at IS NULL")
    )
    op.drop_table("device_commands")
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("spaces")
    op.drop_table("event_zones")
    op.drop_table("event_pricing")
    op.drop_table("devices")
    op.drop_table("zones")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("gates")
    op.drop_index("ix_events_venue_id_starts_at", table_name="events")
    op.drop_index("ix_events_status", table_name="events")
    op.drop_table("events")
    op.drop_table("venues")
