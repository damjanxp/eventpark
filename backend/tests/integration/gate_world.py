"""Builds a small, controllable venue + live event for gate tests (directly in the DB: fast and explicit)."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.security import new_device_api_key
from eventpark.db.models import Device, Event, EventPricing, EventZone, Gate, Space, Ticket, Venue, Zone
from eventpark.domain.events import EventStatus
from eventpark.domain.tickets import TicketStatus, TicketType, new_ticket_code
from eventpark.domain.venues import GateDirection


@dataclass
class World:
    venue_id: uuid.UUID
    event_id: uuid.UUID
    zone_ids: dict[str, uuid.UUID]  # code -> event_zone id
    keys: dict[str, str]  # gate code -> device api key
    device_ids: dict[str, uuid.UUID]
    spaces: dict[str, uuid.UUID] = field(default_factory=dict)

    def headers(self, gate: str) -> dict[str, str]:
        return {"Authorization": f"Device {self.keys[gate]}"}


async def build_world(
    db: async_sessionmaker[AsyncSession],
    *,
    zones: dict[str, tuple[int, int, int]] | None = None,  # code -> (capacity, prebook_quota, allocation_priority)
    entry_gates: tuple[str, ...] = ("N1", "N2"),
    exit_gates: tuple[str, ...] = ("X1",),
    status: EventStatus = EventStatus.LIVE,
    vip_spaces: int = 0,
) -> World:
    zones = zones or {"A": (10, 2, 1), "B": (5, 0, 2)}
    now = datetime.now(UTC)
    async with db() as session, session.begin():
        venue = Venue(name=f"Venue {uuid.uuid4().hex[:6]}", timezone="Europe/Belgrade")
        session.add(venue)
        await session.flush()
        event = Event(
            venue_id=venue.id,
            name="Test event",
            status=status,
            starts_at=now,
            ends_at=now + timedelta(hours=4),
            gates_open_at=now - timedelta(hours=1),
            gates_close_at=now + timedelta(hours=5),
            booking_opens_at=now - timedelta(days=7),
            booking_closes_at=now + timedelta(hours=2),
        )
        session.add(event)
        await session.flush()
        session.add(
            EventPricing(
                event_id=event.id,
                currency="RSD",
                drive_up_free_minutes=15,
                drive_up_hourly_minor=30_000,
                drive_up_daily_cap_minor=150_000,
                lost_ticket_fee_minor=200_000,
                post_payment_exit_grace_minutes=15,
            )
        )
        zone_ids: dict[str, uuid.UUID] = {}
        spaces: dict[str, uuid.UUID] = {}
        for code, (capacity, quota, priority) in zones.items():
            zone = Zone(venue_id=venue.id, code=code, name=f"Zone {code}", total_capacity=max(capacity, 1))
            session.add(zone)
            await session.flush()
            event_zone = EventZone(
                event_id=event.id,
                zone_id=zone.id,
                capacity=capacity,
                prebook_quota=quota,
                prebook_price_minor=80_000,
                allocation_priority=priority,
            )
            session.add(event_zone)
            await session.flush()
            zone_ids[code] = event_zone.id
            if code == "VIP":
                for n in range(1, vip_spaces + 1):
                    space = Space(zone_id=zone.id, label=f"VIP-{n:02d}", kind="VIP")
                    session.add(space)
                    await session.flush()
                    spaces[space.label] = space.id
        keys: dict[str, str] = {}
        device_ids: dict[str, uuid.UUID] = {}
        for code in entry_gates + exit_gates:
            entry = code in entry_gates
            gate = Gate(
                venue_id=venue.id,
                code=code,
                name=code,
                direction=GateDirection.ENTRY if entry else GateDirection.EXIT,
                has_printer=entry,
                has_terminal=not entry,
            )
            session.add(gate)
            await session.flush()
            key, prefix, key_hash = new_device_api_key()
            device = Device(gate_id=gate.id, name=f"{code}-pi", api_key_prefix=prefix, api_key_hash=key_hash)
            session.add(device)
            await session.flush()
            keys[code], device_ids[code] = key, device.id
    return World(venue.id, event.id, zone_ids, keys, device_ids, spaces)


async def add_prebooked_ticket(
    db: async_sessionmaker[AsyncSession], world: World, zone: str = "A", *, space: str | None = None
) -> Ticket:
    """What a confirmed booking produces (Phase 5): a VALID pre-booked ticket, counted in prebook_held."""
    async with db() as session, session.begin():
        event_zone = await session.scalar(
            select(EventZone).where(EventZone.id == world.zone_ids[zone]).with_for_update()
        )
        assert event_zone is not None
        event_zone.prebook_held += 1
        ticket = Ticket(
            event_id=world.event_id,
            type=TicketType.PREBOOKED,
            code=new_ticket_code(),
            event_zone_id=event_zone.id,
            space_id=world.spaces.get(space) if space else None,
            status=TicketStatus.VALID,
        )
        session.add(ticket)
    return ticket
