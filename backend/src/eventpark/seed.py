"""Demo data: `python -m eventpark.seed [--out devices.json]` (or `make seed`).

Creates (once) the venue from docs/01-product-and-domain.md §3:
  Belgrade Arena Grounds: zones A (1200), B (800), VIP (50, spaces VIP-01..VIP-50)
  entry gates N1 N2 N3 S1 S2 (with printers), exit gates X1 X2 X3 (with card terminals), one device each
  users: one per role (password from SEED_PASSWORD, default "eventpark-demo-pass")
  events: "Demo Night (live)" with gates open now, and "Next Week Concert" (published, bookable)

Safe to run again: existing rows are kept. Device keys exist only at creation (we store hashes), so every run
ROTATES all device keys and writes the new ones to --out, for the gate controller / simulator to use.
"""

import argparse
import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.config import get_settings
from eventpark.core.logging import configure_logging
from eventpark.core.security import hash_password
from eventpark.db.engine import create_engine
from eventpark.db.models import Device, Event, Gate, User, Venue, Zone
from eventpark.db.session import create_sessionmaker
from eventpark.domain.events import EventAction, EventStatus
from eventpark.domain.roles import Role
from eventpark.domain.venues import GateDirection, SpaceKind
from eventpark.services import events, venues
from eventpark.services.events import EventZoneConfig

log = structlog.get_logger()

VENUE_NAME = "Belgrade Arena Grounds"
ZONES = [("A", "Zone A", 1200), ("B", "Zone B", 800), ("VIP", "VIP", 50)]
# zone code -> (prebook quota, prebook price in para, drive-up priority: lower first, 0 = no drive-up)
EVENT_ZONES = {"A": (300, 80_000, 1), "B": (200, 60_000, 2), "VIP": (50, 250_000, 0)}
ENTRY_GATES = ["N1", "N2", "N3", "S1", "S2"]
EXIT_GATES = ["X1", "X2", "X3"]
PRICING = {
    "currency": "RSD",
    "drive_up_free_minutes": 15,
    "drive_up_hourly_minor": 30_000,
    "drive_up_daily_cap_minor": 150_000,
    "lost_ticket_fee_minor": 200_000,
    "post_payment_exit_grace_minutes": 15,
}


async def _ensure_users(session: AsyncSession, password: str) -> uuid.UUID:
    admin_id: uuid.UUID | None = None
    async with session.begin():
        for role in Role:
            email = f"{role.value.lower().replace('_', '.')}@eventpark.dev"
            user = await session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    password_hash=hash_password(password),
                    full_name=f"Demo {role.value.title()}",
                    role=role,
                )
                session.add(user)
                await session.flush()
            if role is Role.ADMIN:
                admin_id = user.id
    if admin_id is None:  # pragma: no cover (Role always contains ADMIN)
        raise RuntimeError("no admin user")
    return admin_id


async def _ensure_venue(session: AsyncSession, admin: uuid.UUID) -> tuple[Venue, dict[str, Zone], list[Gate]]:
    venue = await session.scalar(select(Venue).where(Venue.name == VENUE_NAME))
    await session.commit()
    if venue is None:
        venue = await venues.create_venue(
            session,
            actor=admin,
            name=VENUE_NAME,
            address="Bulevar Arsenija Carnojevica 58, Belgrade",
            timezone="Europe/Belgrade",
        )
    zones = {z.code: z for z in await venues.list_zones(session, venue.id)}
    await session.commit()
    for code, name, capacity in ZONES:
        if code not in zones:
            zones[code] = await venues.create_zone(
                session, actor=admin, venue_id=venue.id, code=code, name=name, total_capacity=capacity
            )
            if code == "VIP":
                await venues.create_spaces(
                    session,
                    actor=admin,
                    zone_id=zones[code].id,
                    labels=[f"VIP-{n:02d}" for n in range(1, 51)],
                    kind=SpaceKind.VIP,
                )
    gates = {g.code: g for g in await venues.list_gates(session, venue.id)}
    await session.commit()
    for code in ENTRY_GATES + EXIT_GATES:
        if code not in gates:
            entry = code in ENTRY_GATES
            gates[code] = await venues.create_gate(
                session,
                actor=admin,
                venue_id=venue.id,
                code=code,
                name=f"{'Entry' if entry else 'Exit'} {code}",
                direction=GateDirection.ENTRY if entry else GateDirection.EXIT,
                has_printer=entry,
                has_terminal=not entry,
            )
    return venue, zones, list(gates.values())


async def _device_keys(session: AsyncSession, admin: uuid.UUID, gates: list[Gate]) -> list[dict[str, str]]:
    out = []
    for gate in sorted(gates, key=lambda g: g.code):
        has_device = await session.scalar(select(Device.id).where(Device.gate_id == gate.id))
        await session.commit()
        if has_device is None:
            result = await venues.create_device(session, actor=admin, gate_id=gate.id, name=f"{gate.code.lower()}-pi")
        else:
            result = await venues.rotate_device_key(session, actor=admin, gate_id=gate.id)
        out.append(
            {
                "gate_code": gate.code,
                "gate_id": str(gate.id),
                "direction": gate.direction.value,
                "api_key": result.api_key,
            }
        )
    return out


async def _ensure_event(
    session: AsyncSession,
    admin: uuid.UUID,
    venue: Venue,
    zones: dict[str, Zone],
    name: str,
    gates_open: datetime,
    *,
    go_live: bool,
) -> Event:
    existing = await session.scalar(select(Event).where(Event.venue_id == venue.id, Event.name == name))
    await session.commit()
    if existing is not None and existing.status in (EventStatus.LIVE, EventStatus.PUBLISHED):
        return existing
    times = {
        "gates_open_at": gates_open,
        "starts_at": gates_open + timedelta(hours=2),
        "ends_at": gates_open + timedelta(hours=5),
        "gates_close_at": gates_open + timedelta(hours=6),
        "booking_opens_at": gates_open - timedelta(days=14),
        "booking_closes_at": gates_open + timedelta(hours=3),
    }
    event = await events.create_event(session, actor=admin, venue_id=venue.id, name=name, times=times)
    await events.set_pricing(session, actor=admin, event_id=event.id, values=PRICING)
    configs = [
        EventZoneConfig(
            zone_id=zones[code].id,
            capacity=zones[code].total_capacity,
            prebook_quota=q,
            prebook_price_minor=p,
            allocation_priority=prio,
        )
        for code, (q, p, prio) in EVENT_ZONES.items()
    ]
    await events.set_zones(session, actor=admin, event_id=event.id, configs=configs)
    now = datetime.now(UTC)
    await events.apply_action(session, actor=admin, event_id=event.id, action=EventAction.PUBLISH, now=now)
    if go_live:
        event = await events.apply_action(session, actor=admin, event_id=event.id, action=EventAction.GO_LIVE, now=now)
    return event


async def seed() -> dict[str, object]:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    password = os.environ.get("SEED_PASSWORD", "eventpark-demo-pass")
    try:
        async with create_sessionmaker(engine)() as session:
            admin = await _ensure_users(session, password)
            venue, zones, gates = await _ensure_venue(session, admin)
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            live = await _ensure_event(
                session, admin, venue, zones, "Demo Night (live)", now - timedelta(hours=1), go_live=True
            )
            future = await _ensure_event(
                session, admin, venue, zones, "Next Week Concert", now + timedelta(days=7), go_live=False
            )
            devices = await _device_keys(session, admin, gates)
    finally:
        await engine.dispose()

    result: dict[str, object] = {
        "venue_id": str(venue.id),
        "live_event_id": str(live.id),
        "future_event_id": str(future.id),
        "users": {r.value: f"{r.value.lower().replace('_', '.')}@eventpark.dev" for r in Role},
        "devices": devices,
    }
    log.info("seed.done", venue_id=result["venue_id"], live_event_id=result["live_event_id"], devices=len(devices))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="write venue/event ids and fresh device keys to this JSON file")
    args = parser.parse_args()
    result = asyncio.run(seed())
    if args.out is not None:
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        args.out.chmod(0o600)  # contains device keys: readable only by you
        log.info("seed.keys_written", path=str(args.out))


if __name__ == "__main__":
    main()
