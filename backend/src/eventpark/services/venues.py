"""Venue setup (ADMIN): venues, zones, numbered spaces, gates, gate devices."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound
from eventpark.core.security import new_device_api_key
from eventpark.db.base import Base
from eventpark.db.errors import constraint_name
from eventpark.db.models import Device, Gate, Space, Venue, Zone
from eventpark.domain.venues import DeviceStatus, GateDirection, SpaceKind
from eventpark.services import audit

# Constraint name -> what we tell the client. Everything else re-raises (a bug, not a user error).
_CONFLICTS = {
    "uq_zones_venue_id_code": ("ZONE_CODE_TAKEN", "A zone with this code already exists at this venue."),
    "uq_gates_venue_id_code": ("GATE_CODE_TAKEN", "A gate with this code already exists at this venue."),
    "uq_spaces_zone_id_label": ("SPACE_LABEL_TAKEN", "A space with this label already exists in this zone."),
    "uq_devices_gate_id": ("DEVICE_EXISTS", "This gate already has a device."),
    "ck_gates_printer_only_on_entry": ("INVALID_GATE", "Only entry lanes have a ticket printer."),
    "ck_gates_terminal_only_on_exit": ("INVALID_GATE", "Only exit lanes have a card terminal."),
    "fk_zones_venue_id_venues": ("VENUE_IN_USE", "The venue still has zones."),
    "fk_gates_venue_id_venues": ("VENUE_IN_USE", "The venue still has gates."),
    "fk_events_venue_id_venues": ("VENUE_IN_USE", "The venue has events."),
    "fk_event_zones_zone_id_zones": ("ZONE_IN_USE", "The zone is used by an event."),
}


async def _flush(session: AsyncSession) -> None:
    """Send pending SQL now, translating known constraint violations into 409 Conflict."""
    try:
        await session.flush()
    except IntegrityError as exc:
        known = _CONFLICTS.get(constraint_name(exc) or "")
        if known is None:
            raise
        raise Conflict(known[1], code=known[0]) from exc


async def _get[M: Base](
    session: AsyncSession, model: type[M], obj_id: uuid.UUID, what: str, *, lock: bool = False
) -> M:
    obj = await session.get(model, obj_id, with_for_update=lock)
    if obj is None:
        raise NotFound(f"{what.capitalize()} not found.", code=f"{what.upper()}_NOT_FOUND")
    return obj


def _apply(obj: Base, changes: dict[str, Any]) -> dict[str, Any]:
    """Set the given attributes, return only those that really changed (for the audit log)."""
    changed = {}
    for key, value in changes.items():
        if value is not None and getattr(obj, key) != value:
            setattr(obj, key, value)
            changed[key] = value.value if hasattr(value, "value") else value
    return changed


async def _list[M](session: AsyncSession, query: Select[M]) -> Sequence[M]:
    return (await session.scalars(query)).all()


# --- venues ---


async def list_venues(session: AsyncSession) -> Sequence[Venue]:
    return await _list(session, select(Venue).order_by(Venue.name))


async def get_venue(session: AsyncSession, venue_id: uuid.UUID) -> Venue:
    return await _get(session, Venue, venue_id, "venue")


async def create_venue(session: AsyncSession, *, actor: uuid.UUID, name: str, address: str, timezone: str) -> Venue:
    async with session.begin():
        venue = Venue(name=name, address=address, timezone=timezone)
        session.add(venue)
        await _flush(session)
        audit.record(session, actor_user_id=actor, action="venue.created", target_type="venue", target_id=venue.id)
    return venue


async def update_venue(session: AsyncSession, *, actor: uuid.UUID, venue_id: uuid.UUID, **changes: Any) -> Venue:
    async with session.begin():
        venue = await _get(session, Venue, venue_id, "venue", lock=True)
        if changed := _apply(venue, changes):
            audit.record(
                session,
                actor_user_id=actor,
                action="venue.updated",
                target_type="venue",
                target_id=venue.id,
                details=changed,
            )
    return venue


async def _delete(session: AsyncSession, obj: Base, *, actor: uuid.UUID, kind: str, obj_id: uuid.UUID) -> None:
    await session.delete(obj)
    await _flush(session)
    audit.record(session, actor_user_id=actor, action=f"{kind}.deleted", target_type=kind, target_id=obj_id)


async def delete_venue(session: AsyncSession, *, actor: uuid.UUID, venue_id: uuid.UUID) -> None:
    async with session.begin():
        await _delete(
            session, await _get(session, Venue, venue_id, "venue"), actor=actor, kind="venue", obj_id=venue_id
        )


# --- zones ---


async def list_zones(session: AsyncSession, venue_id: uuid.UUID) -> Sequence[Zone]:
    await get_venue(session, venue_id)
    return await _list(session, select(Zone).where(Zone.venue_id == venue_id).order_by(Zone.code))


async def create_zone(
    session: AsyncSession, *, actor: uuid.UUID, venue_id: uuid.UUID, code: str, name: str, total_capacity: int
) -> Zone:
    async with session.begin():
        await _get(session, Venue, venue_id, "venue")
        zone = Zone(venue_id=venue_id, code=code, name=name, total_capacity=total_capacity)
        session.add(zone)
        await _flush(session)
        audit.record(
            session,
            actor_user_id=actor,
            action="zone.created",
            target_type="zone",
            target_id=zone.id,
            details={"code": code, "total_capacity": total_capacity},
        )
    return zone


async def update_zone(session: AsyncSession, *, actor: uuid.UUID, zone_id: uuid.UUID, **changes: Any) -> Zone:
    async with session.begin():
        zone = await _get(session, Zone, zone_id, "zone", lock=True)
        if changed := _apply(zone, changes):
            await _flush(session)
            audit.record(
                session,
                actor_user_id=actor,
                action="zone.updated",
                target_type="zone",
                target_id=zone.id,
                details=changed,
            )
    return zone


async def delete_zone(session: AsyncSession, *, actor: uuid.UUID, zone_id: uuid.UUID) -> None:
    async with session.begin():
        await _delete(session, await _get(session, Zone, zone_id, "zone"), actor=actor, kind="zone", obj_id=zone_id)


# --- spaces ---


async def list_spaces(session: AsyncSession, zone_id: uuid.UUID) -> Sequence[Space]:
    await _get(session, Zone, zone_id, "zone")
    return await _list(session, select(Space).where(Space.zone_id == zone_id).order_by(Space.label))


async def create_spaces(
    session: AsyncSession, *, actor: uuid.UUID, zone_id: uuid.UUID, labels: list[str], kind: SpaceKind
) -> list[Space]:
    async with session.begin():
        zone = await _get(session, Zone, zone_id, "zone", lock=True)
        count = await session.scalar(select(func.count()).select_from(Space).where(Space.zone_id == zone_id)) or 0
        if count + len(labels) > zone.total_capacity:
            raise Conflict(
                f"Zone {zone.code} has room for {zone.total_capacity} spaces; it has {count} already.",
                code="TOO_MANY_SPACES",
            )
        spaces = [Space(zone_id=zone_id, label=label, kind=kind) for label in labels]
        session.add_all(spaces)
        await _flush(session)
        audit.record(
            session,
            actor_user_id=actor,
            action="spaces.created",
            target_type="zone",
            target_id=zone_id,
            details={"count": len(labels), "kind": kind.value},
        )
    return spaces


async def delete_space(session: AsyncSession, *, actor: uuid.UUID, space_id: uuid.UUID) -> None:
    async with session.begin():
        space = await _get(session, Space, space_id, "space")
        await _delete(session, space, actor=actor, kind="space", obj_id=space_id)


# --- gates ---


async def list_gates(session: AsyncSession, venue_id: uuid.UUID) -> Sequence[Gate]:
    await get_venue(session, venue_id)
    return await _list(session, select(Gate).where(Gate.venue_id == venue_id).order_by(Gate.code))


async def create_gate(
    session: AsyncSession,
    *,
    actor: uuid.UUID,
    venue_id: uuid.UUID,
    code: str,
    name: str,
    direction: GateDirection,
    has_printer: bool,
    has_terminal: bool,
) -> Gate:
    async with session.begin():
        await _get(session, Venue, venue_id, "venue")
        gate = Gate(
            venue_id=venue_id,
            code=code,
            name=name,
            direction=direction,
            has_printer=has_printer,
            has_terminal=has_terminal,
        )
        session.add(gate)
        await _flush(session)
        audit.record(
            session,
            actor_user_id=actor,
            action="gate.created",
            target_type="gate",
            target_id=gate.id,
            details={"code": code, "direction": direction.value},
        )
    return gate


async def update_gate(session: AsyncSession, *, actor: uuid.UUID, gate_id: uuid.UUID, **changes: Any) -> Gate:
    async with session.begin():
        gate = await _get(session, Gate, gate_id, "gate", lock=True)
        if changed := _apply(gate, changes):
            await _flush(session)
            audit.record(
                session,
                actor_user_id=actor,
                action="gate.updated",
                target_type="gate",
                target_id=gate.id,
                details=changed,
            )
    return gate


async def delete_gate(session: AsyncSession, *, actor: uuid.UUID, gate_id: uuid.UUID) -> None:
    async with session.begin():
        await _delete(session, await _get(session, Gate, gate_id, "gate"), actor=actor, kind="gate", obj_id=gate_id)


# --- devices ---


@dataclass(frozen=True)
class DeviceWithKey:
    device: Device
    api_key: str  # shown once; only its hash is stored


async def get_device(session: AsyncSession, gate_id: uuid.UUID) -> Device:
    device = await session.scalar(select(Device).where(Device.gate_id == gate_id))
    if device is None:
        raise NotFound("This gate has no device yet.", code="DEVICE_NOT_FOUND")
    return device


async def create_device(session: AsyncSession, *, actor: uuid.UUID, gate_id: uuid.UUID, name: str) -> DeviceWithKey:
    async with session.begin():
        await _get(session, Gate, gate_id, "gate")
        key, prefix, key_hash = new_device_api_key()
        device = Device(gate_id=gate_id, name=name, api_key_prefix=prefix, api_key_hash=key_hash)
        session.add(device)
        await _flush(session)
        audit.record(
            session,
            actor_user_id=actor,
            action="device.created",
            target_type="device",
            target_id=device.id,
            details={"gate_id": str(gate_id), "key_prefix": prefix},
        )
    return DeviceWithKey(device=device, api_key=key)


async def rotate_device_key(session: AsyncSession, *, actor: uuid.UUID, gate_id: uuid.UUID) -> DeviceWithKey:
    """New key, old one stops working immediately (e.g. a Pi was stolen or the key leaked)."""
    async with session.begin():
        device = await session.scalar(select(Device).where(Device.gate_id == gate_id).with_for_update())
        if device is None:
            raise NotFound("This gate has no device yet.", code="DEVICE_NOT_FOUND")
        key, prefix, key_hash = new_device_api_key()
        old_prefix = device.api_key_prefix
        device.api_key_prefix, device.api_key_hash = prefix, key_hash
        audit.record(
            session,
            actor_user_id=actor,
            action="device.key_rotated",
            target_type="device",
            target_id=device.id,
            details={"old_prefix": old_prefix, "new_prefix": prefix},
        )
    return DeviceWithKey(device=device, api_key=key)


async def update_device(
    session: AsyncSession, *, actor: uuid.UUID, gate_id: uuid.UUID, name: str | None, enabled: bool | None
) -> Device:
    async with session.begin():
        device = await session.scalar(select(Device).where(Device.gate_id == gate_id).with_for_update())
        if device is None:
            raise NotFound("This gate has no device yet.", code="DEVICE_NOT_FOUND")
        changed: dict[str, Any] = {}
        if name is not None and name != device.name:
            device.name = changed["name"] = name
        if enabled is False and device.status is not DeviceStatus.DISABLED:
            device.status = DeviceStatus.DISABLED
            changed["status"] = DeviceStatus.DISABLED.value
        elif enabled is True and device.status is DeviceStatus.DISABLED:
            device.status = DeviceStatus.OFFLINE  # back to normal; the next heartbeat marks it ONLINE
            changed["status"] = DeviceStatus.OFFLINE.value
        if changed:
            audit.record(
                session,
                actor_user_id=actor,
                action="device.updated",
                target_type="device",
                target_id=device.id,
                details=changed,
            )
    return device
