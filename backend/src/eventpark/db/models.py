"""Table models (SQLAlchemy ORM). Spec: docs/02-architecture.md §4 "Data model".

Each class describes one table. Alembic compares these classes with the real database to draft migrations;
the migration files (not these classes) are what actually change the database.

The database enforces every rule it can (NOT NULL, UNIQUE, CHECK, foreign keys): app validation gives nice error
messages, constraints give guarantees, even when the Python code has a bug or someone runs SQL by hand.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eventpark.core.ids import uuid7
from eventpark.db.base import Base
from eventpark.domain.events import EventStatus
from eventpark.domain.roles import Role
from eventpark.domain.venues import DeviceCommandType, DeviceStatus, GateDirection, PrinterStatus, SpaceKind


def enum_column[E: StrEnum](enum: type[E], name: str) -> Enum:
    """A StrEnum stored as varchar + CHECK (value IN (...)) (ADR-023), named ck_<table>_<name>."""
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    # Generated in Python (UUIDv7, see core/ids.py), so the app knows the ID before the INSERT.
    return mapped_column(primary_key=True, default=uuid7)


def created_at() -> Mapped[datetime]:
    # server_default: the database fills it in (its clock, in UTC), so every writer agrees on the time.
    return mapped_column(server_default=func.now())


# ---------------------------------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    # citext = case-insensitive text: "Damjan@Example.com" and "damjan@example.com" are the same email,
    # so the UNIQUE constraint can't be fooled by capital letters.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)  # argon2id (core/security.py), never the password itself
    full_name: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(enum_column(Role, "role"))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = created_at()


class RefreshToken(Base):
    """One row per issued refresh token. A login starts a *family*; every refresh adds a row to the same family.

    Reusing an already-rotated (revoked) token means someone has a stolen copy: the whole family is revoked.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)  # SHA-256 of the token, never the token
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at()


class AuditLog(Base):
    """Who did what, when, and why. Append-only: rows are never updated or deleted by the app."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text)  # e.g. "venue.created", "event.published", "device.key_rotated"
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_audit_log_target", "target_type", "target_id"),)


# ---------------------------------------------------------------------------------------------------------------------
# Venue & devices
# ---------------------------------------------------------------------------------------------------------------------


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, server_default="")
    timezone: Mapped[str] = mapped_column(Text)  # IANA name, e.g. "Europe/Belgrade"; used only for display/stats
    created_at: Mapped[datetime] = created_at()


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(Text)
    total_capacity: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("venue_id", "code"),
        CheckConstraint("total_capacity > 0", name="total_capacity_positive"),
    )


class Space(Base):
    """A numbered spot inside a zone (VIP-07). Only zones that sell specific spots have spaces."""

    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(32))
    kind: Mapped[SpaceKind] = mapped_column(enum_column(SpaceKind, "kind"))

    __table_args__ = (UniqueConstraint("zone_id", "label"),)


class Gate(Base):
    """One lane with one direction. Entry lanes may have a ticket printer, exit lanes a card terminal."""

    __tablename__ = "gates"

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(Text)
    direction: Mapped[GateDirection] = mapped_column(enum_column(GateDirection, "direction"))
    has_printer: Mapped[bool] = mapped_column(server_default=text("false"))
    has_terminal: Mapped[bool] = mapped_column(server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("venue_id", "code"),
        CheckConstraint("NOT (has_printer AND direction = 'EXIT')", name="printer_only_on_entry"),
        CheckConstraint("NOT (has_terminal AND direction = 'ENTRY')", name="terminal_only_on_exit"),
    )


class Device(Base):
    """The controller at a gate (the Raspberry Pi). Authenticates with its own API key, not as a user.

    Key format `epd_<prefix>_<secret>`: we look the row up by the public prefix and compare the hash of the whole key
    (ADR-024). The key itself is shown once, at creation/rotation, and never stored.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = uuid_pk()
    gate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gates.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(Text)
    api_key_prefix: Mapped[str] = mapped_column(String(32), unique=True)
    api_key_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[DeviceStatus] = mapped_column(
        enum_column(DeviceStatus, "status"), server_default=DeviceStatus.OFFLINE.value
    )
    last_heartbeat_at: Mapped[datetime | None]
    firmware_version: Mapped[str | None] = mapped_column(Text)
    last_seen_ip: Mapped[str | None] = mapped_column(Text)
    printer_status: Mapped[PrinterStatus | None] = mapped_column(enum_column(PrinterStatus, "printer_status"))
    created_at: Mapped[datetime] = created_at()


class DeviceCommand(Base):
    """Server -> device instructions (e.g. operator's manual open), delivered in the heartbeat response."""

    __tablename__ = "device_commands"

    id: Mapped[uuid.UUID] = uuid_pk()
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    command: Mapped[DeviceCommandType] = mapped_column(enum_column(DeviceCommandType, "command"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    delivered_at: Mapped[datetime | None]
    acked_at: Mapped[datetime | None]

    __table_args__ = (
        # Heartbeats ask "what's pending for me?" every 5 s per device: keep that lookup small and fast.
        Index("ix_device_commands_pending", "device_id", postgresql_where=text("acked_at IS NULL")),
    )


# ---------------------------------------------------------------------------------------------------------------------
# Events, capacity, pricing
# ---------------------------------------------------------------------------------------------------------------------


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    gates_open_at: Mapped[datetime]
    gates_close_at: Mapped[datetime]
    booking_opens_at: Mapped[datetime]
    booking_closes_at: Mapped[datetime]
    status: Mapped[EventStatus] = mapped_column(
        enum_column(EventStatus, "status"), server_default=EventStatus.DRAFT.value
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="event_times_ordered"),
        CheckConstraint("gates_open_at < gates_close_at", name="gate_times_ordered"),
        CheckConstraint("booking_opens_at < booking_closes_at", name="booking_times_ordered"),
        Index("ix_events_venue_id_starts_at", "venue_id", "starts_at"),
        Index("ix_events_status", "status"),
    )


class EventPricing(Base):
    """Drive-up fee rules for one event (docs/01-product-and-domain.md §8). Money = integer minor units (para)."""

    __tablename__ = "event_pricing"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    drive_up_free_minutes: Mapped[int] = mapped_column(Integer)
    drive_up_hourly_minor: Mapped[int] = mapped_column(BigInteger)
    drive_up_daily_cap_minor: Mapped[int] = mapped_column(BigInteger)
    lost_ticket_fee_minor: Mapped[int] = mapped_column(BigInteger)
    post_payment_exit_grace_minutes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        CheckConstraint(
            "drive_up_free_minutes >= 0 AND drive_up_hourly_minor >= 0 AND drive_up_daily_cap_minor >= 0"
            " AND lost_ticket_fee_minor >= 0 AND post_payment_exit_grace_minutes >= 0",
            name="amounts_not_negative",
        ),
    )


class EventZone(Base):
    """Capacity of one zone for one event, plus the live counters the gate hot path updates atomically (Phase 3).

    The CHECK constraints below are the database-level guarantee of invariants 1 and 2
    (docs/01-product-and-domain.md §7): even a buggy UPDATE can never push a counter past its limit.
    """

    __tablename__ = "event_zones"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("zones.id", ondelete="RESTRICT"))
    capacity: Mapped[int] = mapped_column(Integer)
    prebook_quota: Mapped[int] = mapped_column(Integer)
    prebook_price_minor: Mapped[int] = mapped_column(BigInteger)
    allocation_priority: Mapped[int] = mapped_column(Integer)  # lower = filled first; 0 = no drive-up here
    prebook_held: Mapped[int] = mapped_column(Integer, server_default="0")
    prebook_inside: Mapped[int] = mapped_column(Integer, server_default="0")
    drive_up_occupied: Mapped[int] = mapped_column(Integer, server_default="0")

    __table_args__ = (
        UniqueConstraint("event_id", "zone_id"),
        CheckConstraint("capacity >= 0", name="capacity_not_negative"),
        CheckConstraint("prebook_quota BETWEEN 0 AND capacity", name="quota_within_capacity"),
        CheckConstraint("prebook_price_minor >= 0", name="price_not_negative"),
        CheckConstraint("allocation_priority >= 0", name="priority_not_negative"),
        CheckConstraint("prebook_held BETWEEN 0 AND prebook_quota", name="prebook_held_within_quota"),
        CheckConstraint("prebook_inside BETWEEN 0 AND prebook_held", name="prebook_inside_within_held"),
        CheckConstraint("drive_up_occupied BETWEEN 0 AND capacity - prebook_quota", name="drive_up_within_capacity"),
    )
