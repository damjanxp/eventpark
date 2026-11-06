"""Operator actions and dashboard reads (services/operations.py, api/admin/operations.py, ADR-028)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.db.models import (
    AuditLog,
    Device,
    DeviceCommand,
    EventZone,
    HourlyStat,
    OutboxMessage,
    Passage,
    Payment,
    Ticket,
)
from eventpark.domain.roles import Role
from eventpark.domain.tickets import TicketStatus

from .conftest import HeadersFactory
from .gate_world import World, add_prebooked_ticket, build_world

pytestmark = pytest.mark.integration


def rid() -> str:
    return str(uuid.uuid4())


async def gate_id(db: async_sessionmaker[AsyncSession], world: World, code: str) -> uuid.UUID:
    async with db() as session:
        result = await session.scalar(select(Device.gate_id).where(Device.id == world.device_ids[code]))
    assert result is not None
    return result


async def drive_in(client: AsyncClient, world: World, *, hours_ago: float = 0) -> dict[str, Any]:
    issued = (await client.post("/gate/v1/entry/issue", json={"request_id": rid()}, headers=world.headers("N1"))).json()
    assert issued["decision"] == "OPEN"
    done = await client.post(f"/gate/v1/passages/{issued['passage_id']}/complete", headers=world.headers("N1"))
    assert done.status_code == 200
    return issued


async def backdate_entries(db: async_sessionmaker[AsyncSession], world: World, hours: float) -> None:
    async with db() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.event_id == world.event_id)
            .values(entered_at=datetime.now(UTC) - timedelta(hours=hours))
        )


async def event_zone(db: async_sessionmaker[AsyncSession], world: World, code: str = "A") -> EventZone:
    async with db() as session:
        result = await session.get(EventZone, world.zone_ids[code])
    assert result is not None
    return result


async def only_ticket(db: async_sessionmaker[AsyncSession], world: World) -> Ticket:
    async with db() as session:
        result = await session.scalar(select(Ticket).where(Ticket.event_id == world.event_id))
    assert result is not None
    return result


# --- manual open ---


async def test_manual_open_queues_a_command_for_the_device_and_is_audited(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    operator = await auth_headers(Role.GATE_OPERATOR)
    gid = await gate_id(db, world, "N1")
    body = {"request_id": rid(), "reason": "printer jammed, ticket handed out by hand"}

    first = await client.post(f"/admin/v1/gates/{gid}/manual-open", json=body, headers=operator)
    again = await client.post(f"/admin/v1/gates/{gid}/manual-open", json=body, headers=operator)  # double click
    beat = await client.post("/gate/v1/heartbeat", json={}, headers=world.headers("N1"))

    assert first.status_code == 200, first.text
    assert again.json()["passage_id"] == first.json()["passage_id"] and again.json()["replayed"] is True
    commands = beat.json()["commands"]
    assert [c["command"] for c in commands] == ["OPEN_BARRIER"]  # exactly one, despite the retry
    assert commands[0]["reason"] == body["reason"]
    async with db() as session:
        passage = await session.get(Passage, uuid.UUID(first.json()["passage_id"]))
        audits = await session.scalar(select(func.count()).where(AuditLog.action == "gate.manual_open"))
    assert passage is not None and passage.manual and passage.status.value == "COMPLETED"
    assert audits == 1


async def test_manual_open_at_exit_lets_the_car_out_and_records_released_unpaid(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    await drive_in(client, world)
    await backdate_entries(db, world, hours=2)  # 2 h inside -> a fee is due
    ticket = await only_ticket(db, world)
    operator = await auth_headers(Role.GATE_OPERATOR)
    gid = await gate_id(db, world, "X1")

    response = await client.post(
        f"/admin/v1/gates/{gid}/manual-open",
        json={"request_id": rid(), "reason": "card terminal down, queue building", "ticket_code": ticket.code},
        headers=operator,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ticket_status"] == "EXITED"
    assert result["amount_due_minor"] == 90_000  # 2 h and a few seconds = 3 started hours x 300 RSD
    assert (await event_zone(db, world)).drive_up_occupied == 0  # the place is free again
    async with db() as session:
        types = (await session.scalars(select(OutboxMessage.type).order_by(OutboxMessage.id))).all()
    assert "passage.released_unpaid" in types


async def test_parking_staff_cannot_open_barriers(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    gid = await gate_id(db, world, "N1")
    response = await client.post(
        f"/admin/v1/gates/{gid}/manual-open",
        json={"request_id": rid(), "reason": "let me in"},
        headers=await auth_headers(Role.PARKING_STAFF),
    )
    assert response.status_code == 403
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceCommand)) == 0


# --- void ---


async def test_void_releases_a_drive_up_place_and_is_idempotent(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    await drive_in(client, world)
    ticket = await only_ticket(db, world)
    operator = await auth_headers(Role.GATE_OPERATOR)
    assert (await event_zone(db, world)).drive_up_occupied == 1

    first = await client.post(f"/admin/v1/tickets/{ticket.id}/void", json={"reason": "fraud"}, headers=operator)
    again = await client.post(f"/admin/v1/tickets/{ticket.id}/void", json={"reason": "fraud"}, headers=operator)

    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == "VOID"
    assert (await event_zone(db, world)).drive_up_occupied == 0  # released once, not twice


async def test_void_releases_a_prebooked_hold(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    response = await client.post(
        f"/admin/v1/tickets/{ticket.id}/void",
        json={"reason": "duplicate booking"},
        headers=await auth_headers(Role.ADMIN),
    )
    assert response.status_code == 200
    assert (await event_zone(db, world)).prebook_held == 0


async def test_a_ticket_that_left_cannot_be_voided(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    async with db() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(status=TicketStatus.EXITED, entered_at=datetime.now(UTC))
        )
    response = await client.post(
        f"/admin/v1/tickets/{ticket.id}/void", json={"reason": "late"}, headers=await auth_headers(Role.ADMIN)
    )
    assert response.status_code == 409 and response.json()["code"] == "TICKET_FINAL"


# --- lost ticket ---


async def test_lost_ticket_charges_the_flat_fee_voids_the_ticket_and_opens(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    await drive_in(client, world)
    ticket = await only_ticket(db, world)
    gid = await gate_id(db, world, "X1")
    body = {"request_id": rid(), "gate_id": str(gid), "ticket_code": ticket.code}
    operator = await auth_headers(Role.GATE_OPERATOR)

    first = await client.post("/admin/v1/tickets/lost", json=body, headers=operator)
    again = await client.post("/admin/v1/tickets/lost", json=body, headers=operator)

    assert first.status_code == 200, first.text
    assert first.json()["amount_paid_minor"] == 200_000 and first.json()["ticket_status"] == "VOID"
    assert again.json()["replayed"] is True
    assert (await event_zone(db, world)).drive_up_occupied == 0
    async with db() as session:
        payments = (await session.scalars(select(Payment))).all()
        commands = await session.scalar(select(func.count()).select_from(DeviceCommand))
    assert [(p.kind.value, p.status.value, p.amount_minor) for p in payments] == [("LOST_TICKET", "SUCCEEDED", 200_000)]
    assert commands == 1


async def test_lost_ticket_without_finding_the_ticket_still_charges_and_opens(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    gid = await gate_id(db, world, "X1")
    response = await client.post(
        "/admin/v1/tickets/lost",
        json={"request_id": rid(), "gate_id": str(gid), "plate": "BG 000-XX"},
        headers=await auth_headers(Role.GATE_OPERATOR),
    )
    assert response.status_code == 200, response.text
    assert response.json()["ticket_id"] is None and response.json()["amount_paid_minor"] == 200_000


async def test_lost_ticket_only_at_exit_gates(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    gid = await gate_id(db, world, "N1")
    response = await client.post(
        "/admin/v1/tickets/lost",
        json={"request_id": rid(), "gate_id": str(gid)},
        headers=await auth_headers(Role.GATE_OPERATOR),
    )
    assert response.status_code == 422 and response.json()["code"] == "NOT_AN_EXIT_GATE"


# --- reads ---


async def test_inside_list_passages_feed_and_lookup(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    for _ in range(3):
        await drive_in(client, world)
    await client.post(
        "/gate/v1/entry/scan", json={"scan_id": rid(), "ticket_code": "EP1-NOPE"}, headers=world.headers("N2")
    )
    staff = await auth_headers(Role.PARKING_STAFF)

    page1 = (await client.get(f"/admin/v1/events/{world.event_id}/inside?limit=2", headers=staff)).json()
    page2 = (
        await client.get(
            f"/admin/v1/events/{world.event_id}/inside?limit=2&cursor={page1['next_cursor']}", headers=staff
        )
    ).json()
    feed = (await client.get(f"/admin/v1/events/{world.event_id}/passages?limit=50", headers=staff)).json()
    ticket = await only_ticket(db, world)
    found = (await client.get(f"/admin/v1/tickets/lookup?code={ticket.code}", headers=staff)).json()

    assert len(page1["items"]) == 2 and len(page2["items"]) == 1 and page2["next_cursor"] is None
    assert {i["ticket_id"] for i in page1["items"]}.isdisjoint({i["ticket_id"] for i in page2["items"]})
    assert feed["items"][0]["status"] == "REJECTED"  # newest first: the unknown ticket at N2 came last
    assert feed["items"][0]["reject_reason"] == "UNKNOWN_TICKET"
    assert len(found) == 1 and found[0]["status"] == "INSIDE" and found[0]["passages"][0]["gate_code"] == "N1"


async def test_gate_activity_and_hourly_stats(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    await drive_in(client, world)
    await client.post("/gate/v1/heartbeat", json={}, headers=world.headers("N1"))
    n1 = await gate_id(db, world, "N1")
    hour = datetime(2026, 11, 6, 17, tzinfo=UTC)
    async with db() as session, session.begin():
        session.add(HourlyStat(event_id=world.event_id, gate_id=n1, hour_start=hour, entries=5, revenue_minor=90_000))
        session.add(
            HourlyStat(event_id=world.event_id, gate_id=n1, hour_start=hour + timedelta(hours=1), entries=2, exits=1)
        )
    staff = await auth_headers(Role.PARKING_STAFF)

    activity = (await client.get(f"/admin/v1/events/{world.event_id}/gates/activity", headers=staff)).json()
    stats = (await client.get(f"/admin/v1/events/{world.event_id}/stats/hourly", headers=staff)).json()

    by_code = {g["code"]: g for g in activity}
    assert by_code["N1"]["online"] is True and by_code["N1"]["passages_15m"] == 1
    assert by_code["X1"]["online"] is False  # never sent a heartbeat
    assert [h["entries"] for h in stats["hours"]] == [5, 2]
    assert stats["total_entries"] == 7 and stats["total_revenue_minor"] == 90_000


async def test_prebooked_counters_stay_consistent_with_bookings(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], auth_headers: HeadersFactory
) -> None:
    """prebook_held == active bookings: a manual exit keeps the hold (single-entry pass); a void releases it."""
    world = await build_world(db)
    operator = await auth_headers(Role.GATE_OPERATOR)
    leaving = await add_prebooked_ticket(db, world)
    voided = await add_prebooked_ticket(db, world)
    for ticket in (leaving, voided):
        scan = (
            await client.post(
                "/gate/v1/entry/scan", json={"scan_id": rid(), "ticket_code": ticket.code}, headers=world.headers("N1")
            )
        ).json()
        await client.post(f"/gate/v1/passages/{scan['passage_id']}/complete", headers=world.headers("N1"))
    assert (await event_zone(db, world)).prebook_inside == 2

    x1 = await gate_id(db, world, "X1")
    released = await client.post(
        f"/admin/v1/gates/{x1}/manual-open",
        json={"request_id": rid(), "reason": "scanner broken", "ticket_code": leaving.code},
        headers=operator,
    )
    voided_response = await client.post(
        f"/admin/v1/tickets/{voided.id}/void", json={"reason": "fraud"}, headers=operator
    )

    assert released.status_code == voided_response.status_code == 200
    ez = await event_zone(db, world)
    assert ez.prebook_inside == 0
    assert ez.prebook_held == 1  # the exited pass keeps its reservation, the voided one gave it back
