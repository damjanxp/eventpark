"""Events (ADMIN, EVENT_MANAGER): create/edit, pricing, per-zone capacity, lifecycle actions."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound, ValidationFailed
from eventpark.db.errors import constraint_name
from eventpark.db.models import Event, EventPricing, EventZone, Venue, Zone
from eventpark.db.pagination import paginate
from eventpark.domain.events import (
    EDITABLE_STATUSES,
    EventAction,
    EventSnapshot,
    EventStatus,
    TransitionError,
    transition,
)
from eventpark.services import audit

_TIME_FIELDS = ("starts_at", "ends_at", "gates_open_at", "gates_close_at", "booking_opens_at", "booking_closes_at")


@dataclass(frozen=True)
class EventDetail:
    event: Event
    pricing: EventPricing | None
    zones: Sequence[tuple[EventZone, Zone]]


@dataclass(frozen=True)
class EventZoneConfig:
    zone_id: uuid.UUID
    capacity: int
    prebook_quota: int
    prebook_price_minor: int
    allocation_priority: int


async def _get_event(session: AsyncSession, event_id: uuid.UUID, *, lock: bool = False) -> Event:
    event = await session.get(Event, event_id, with_for_update=lock)
    if event is None:
        raise NotFound("Event not found.", code="EVENT_NOT_FOUND")
    return event


def _require_editable(event: Event) -> None:
    if event.status not in EDITABLE_STATUSES:
        raise Conflict(f"A {event.status.value} event can't be changed.", code="EVENT_NOT_EDITABLE")


def _check_times(values: dict[str, datetime]) -> None:
    """Friendly 422s for the same rules the DB CHECK constraints enforce (the DB stays the last line of defence)."""
    for start, end in (
        ("starts_at", "ends_at"),
        ("gates_open_at", "gates_close_at"),
        ("booking_opens_at", "booking_closes_at"),
    ):
        if values[start] >= values[end]:
            raise ValidationFailed(f"{start} must be before {end}.", code="INVALID_TIMES")
    for name, value in values.items():
        if value.tzinfo is None:
            raise ValidationFailed(
                f"{name} needs a time zone offset (e.g. 2026-11-04T20:00:00+01:00).", code="NAIVE_DATETIME"
            )


async def list_events(
    session: AsyncSession, *, status: EventStatus | None, venue_id: uuid.UUID | None, cursor: str | None, limit: int
) -> tuple[Sequence[Event], str | None]:
    query = select(Event)
    if status is not None:
        query = query.where(Event.status == status)
    if venue_id is not None:
        query = query.where(Event.venue_id == venue_id)
    return await paginate(session, query, Event.id, cursor=cursor, limit=limit)


async def get_event_detail(session: AsyncSession, event_id: uuid.UUID) -> EventDetail:
    event = await _get_event(session, event_id)
    pricing = await session.get(EventPricing, event_id)
    zones = (
        await session.execute(
            select(EventZone, Zone)
            .join(Zone, Zone.id == EventZone.zone_id)
            .where(EventZone.event_id == event_id)
            .order_by(Zone.code)
        )
    ).all()
    return EventDetail(event=event, pricing=pricing, zones=zones)


async def create_event(
    session: AsyncSession, *, actor: uuid.UUID, venue_id: uuid.UUID, name: str, times: dict[str, datetime]
) -> Event:
    _check_times(times)
    async with session.begin():
        if await session.get(Venue, venue_id) is None:
            raise NotFound("Venue not found.", code="VENUE_NOT_FOUND")
        event = Event(venue_id=venue_id, name=name, created_by=actor, **times)
        session.add(event)
        await session.flush()
        audit.record(session, actor_user_id=actor, action="event.created", target_type="event", target_id=event.id)
    return event


async def update_event(
    session: AsyncSession, *, actor: uuid.UUID, event_id: uuid.UUID, name: str | None, times: dict[str, datetime | None]
) -> Event:
    async with session.begin():
        event = await _get_event(session, event_id, lock=True)
        _require_editable(event)
        merged = {f: times.get(f) or getattr(event, f) for f in _TIME_FIELDS}
        _check_times(merged)
        changed: dict[str, Any] = {}
        if name is not None and name != event.name:
            event.name = changed["name"] = name
        for field, value in merged.items():
            if value != getattr(event, field):
                setattr(event, field, value)
                changed[field] = value.isoformat()
        if changed:
            audit.record(
                session,
                actor_user_id=actor,
                action="event.updated",
                target_type="event",
                target_id=event.id,
                details=changed,
            )
    return event


async def set_pricing(
    session: AsyncSession, *, actor: uuid.UUID, event_id: uuid.UUID, values: dict[str, Any]
) -> EventPricing:
    async with session.begin():
        event = await _get_event(session, event_id, lock=True)
        _require_editable(event)
        pricing = await session.get(EventPricing, event_id)
        if pricing is None:
            pricing = EventPricing(event_id=event_id, **values)
            session.add(pricing)
        else:
            for key, value in values.items():
                setattr(pricing, key, value)
        audit.record(
            session,
            actor_user_id=actor,
            action="event.pricing_set",
            target_type="event",
            target_id=event_id,
            details=values,
        )
    return pricing


async def set_zones(
    session: AsyncSession, *, actor: uuid.UUID, event_id: uuid.UUID, configs: Sequence[EventZoneConfig]
) -> Sequence[tuple[EventZone, Zone]]:
    """Replace the event's zone configuration with `configs` (zones not listed are removed if still unused)."""
    if len({c.zone_id for c in configs}) != len(configs):
        raise ValidationFailed("Each zone may appear only once.", code="DUPLICATE_ZONE")
    async with session.begin():
        event = await _get_event(session, event_id, lock=True)
        _require_editable(event)
        venue_zones = {
            z.id: z for z in (await session.scalars(select(Zone).where(Zone.venue_id == event.venue_id))).all()
        }
        for c in configs:
            zone = venue_zones.get(c.zone_id)
            if zone is None:
                raise ValidationFailed("Zone doesn't belong to this event's venue.", code="ZONE_NOT_IN_VENUE")
            # Cross-table rule (a CHECK can't look at another table): enforce it here.
            if c.capacity > zone.total_capacity:
                raise ValidationFailed(
                    f"Zone {zone.code}: capacity {c.capacity} exceeds its physical capacity {zone.total_capacity}.",
                    code="CAPACITY_EXCEEDS_ZONE",
                )

        # Lock order rule (docs/02-architecture.md §5): event_zone rows first. Here they're the only rows we lock.
        current = {
            ez.zone_id: ez
            for ez in (
                await session.scalars(select(EventZone).where(EventZone.event_id == event_id).with_for_update())
            ).all()
        }
        wanted = {c.zone_id: c for c in configs}
        for zone_id, ez in current.items():
            if zone_id not in wanted:
                if ez.prebook_held or ez.drive_up_occupied:
                    raise Conflict(
                        f"Zone {venue_zones[zone_id].code} is in use and can't be removed.", code="ZONE_IN_USE"
                    )
                await session.delete(ez)
        for zone_id, c in wanted.items():
            existing = current.get(zone_id)
            if existing is None:
                session.add(
                    EventZone(
                        event_id=event_id,
                        zone_id=zone_id,
                        capacity=c.capacity,
                        prebook_quota=c.prebook_quota,
                        prebook_price_minor=c.prebook_price_minor,
                        allocation_priority=c.allocation_priority,
                    )
                )
            else:
                existing.capacity, existing.prebook_quota = c.capacity, c.prebook_quota
                existing.prebook_price_minor, existing.allocation_priority = (
                    c.prebook_price_minor,
                    c.allocation_priority,
                )
        try:
            await session.flush()
        except IntegrityError as exc:
            # The CHECKs on event_zones refuse e.g. a quota below what's already sold: that's a conflict with reality.
            name = constraint_name(exc) or ""
            if name.startswith("ck_event_zones_"):
                raise Conflict(
                    "New capacity/quota is below what's already sold or occupied (or quota > capacity).",
                    code="CAPACITY_BELOW_USAGE",
                    extra={"constraint": name},
                ) from exc
            raise
        audit.record(
            session,
            actor_user_id=actor,
            action="event.zones_set",
            target_type="event",
            target_id=event_id,
            details={
                "zones": [
                    {"zone_id": str(c.zone_id), "capacity": c.capacity, "prebook_quota": c.prebook_quota}
                    for c in configs
                ]
            },
        )
    return (await get_event_detail(session, event_id)).zones


async def apply_action(
    session: AsyncSession, *, actor: uuid.UUID, event_id: uuid.UUID, action: EventAction, now: datetime
) -> Event:
    async with session.begin():
        # FOR UPDATE: two managers clicking "publish" and "cancel" at the same moment are applied one after the other.
        event = await _get_event(session, event_id, lock=True)
        has_pricing = await session.get(EventPricing, event_id) is not None
        zone_count = (
            await session.scalar(select(func.count()).select_from(EventZone).where(EventZone.event_id == event_id)) or 0
        )
        snapshot = EventSnapshot(
            gates_open_at=event.gates_open_at,
            gates_close_at=event.gates_close_at,
            has_pricing=has_pricing,
            zone_count=zone_count,
        )
        result = transition(event.status, action, now=now, event=snapshot)
        if isinstance(result, TransitionError):
            raise Conflict(result.message, code=result.code)
        previous, event.status = event.status, result
        audit.record(
            session,
            actor_user_id=actor,
            action=f"event.{action.value}",
            target_type="event",
            target_id=event_id,
            details={"from": previous.value, "to": result.value},
        )
    return event
