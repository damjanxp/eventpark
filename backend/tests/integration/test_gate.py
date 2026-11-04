"""Gate hot path, one request at a time: flows A-E of docs/01-product-and-domain.md §6, idempotency, expiry."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.db.models import Device, DeviceCommand, EventZone, OutboxMessage, Passage, Ticket
from eventpark.domain.events import EventStatus

from .gate_world import World, add_prebooked_ticket, build_world

pytestmark = pytest.mark.integration


def sid() -> str:
    return str(uuid.uuid4())


async def scan(client: AsyncClient, world: World, gate: str, code: str, scan_id: str | None = None) -> dict[str, Any]:
    path = "/gate/v1/entry/scan" if gate.startswith(("N", "S")) else "/gate/v1/exit/scan"
    response = await client.post(
        path, json={"scan_id": scan_id or sid(), "ticket_code": code}, headers=world.headers(gate)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def issue(client: AsyncClient, world: World, gate: str = "N1", request_id: str | None = None) -> dict[str, Any]:
    response = await client.post(
        "/gate/v1/entry/issue", json={"request_id": request_id or sid()}, headers=world.headers(gate)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def complete(client: AsyncClient, world: World, gate: str, passage_id: str) -> Any:
    return await client.post(f"/gate/v1/passages/{passage_id}/complete", headers=world.headers(gate))


async def zone(db: async_sessionmaker[AsyncSession], world: World, code: str = "A") -> EventZone:
    async with db() as session:
        result = await session.get(EventZone, world.zone_ids[code])
    assert result is not None
    return result


# --- device authentication ---


@pytest.mark.parametrize("header", [None, "Bearer xyz", "Device nope", "Device epd_0123456789ab_wrongsecret"])
async def test_bad_device_credentials_are_401(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], header: str | None
) -> None:
    await build_world(db)
    headers = {"Authorization": header} if header else {}

    response = await client.post("/gate/v1/entry/issue", json={"request_id": sid()}, headers=headers)

    assert response.status_code == 401


async def test_disabled_device_is_refused(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db)
    async with db() as session, session.begin():
        await session.execute(update(Device).where(Device.id == world.device_ids["N1"]).values(status="DISABLED"))

    response = await client.post("/gate/v1/entry/issue", json={"request_id": sid()}, headers=world.headers("N1"))

    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_DISABLED"


# --- A: pre-booked entry, then C: pre-booked exit ---


async def test_prebooked_full_cycle_with_anti_passback(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)

    opened = await scan(client, world, "N1", ticket.code)
    assert opened["decision"] == "OPEN" and opened["zone"] == "A"
    done = await complete(client, world, "N1", opened["passage_id"])
    assert done.json() == {"passage_id": opened["passage_id"], "status": "COMPLETED", "ticket_status": "INSIDE"}
    assert (await zone(db, world)).prebook_inside == 1

    again = await scan(client, world, "N2", ticket.code)
    assert again["decision"] == "DENY" and again["reason"] == "ALREADY_INSIDE"

    leaving = await scan(client, world, "X1", ticket.code)
    assert leaving["decision"] == "OPEN"  # pre-booked: never pays at the exit
    assert (await complete(client, world, "X1", leaving["passage_id"])).json()["ticket_status"] == "EXITED"
    after = await zone(db, world)
    assert after.prebook_inside == 0 and after.prebook_held == 1  # single-entry pass keeps its reservation

    reentry = await scan(client, world, "N1", ticket.code)
    assert reentry["reason"] == "ALREADY_USED"


async def test_vip_space_is_shown(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db, zones={"VIP": (5, 5, 0)}, vip_spaces=5)
    ticket = await add_prebooked_ticket(db, world, "VIP", space="VIP-03")

    opened = await scan(client, world, "N1", ticket.code)

    assert opened["space"] == "VIP-03"
    assert opened["display_text"] == "Welcome! Zone VIP, space VIP-03"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("unknown", "UNKNOWN_TICKET"),
        ("exit_gate", "DEVICE_NOT_ALLOWED"),
        ("not_live", "EVENT_NOT_LIVE"),
        ("not_inside", "NOT_INSIDE"),
    ],
)
async def test_rejections(client: AsyncClient, db: async_sessionmaker[AsyncSession], setup: str, reason: str) -> None:
    world = await build_world(db, status=EventStatus.PUBLISHED if setup == "not_live" else EventStatus.LIVE)
    ticket = await add_prebooked_ticket(db, world)

    if setup == "unknown":
        result = await scan(client, world, "N1", "EP1-NOPENOPENOPE")
    elif setup == "exit_gate":  # entry scan sent from an exit lane device
        response = await client.post(
            "/gate/v1/entry/scan", json={"scan_id": sid(), "ticket_code": ticket.code}, headers=world.headers("X1")
        )
        result = response.json()
    elif setup == "not_live":
        result = await scan(client, world, "N1", ticket.code)
    else:  # exit scan for a ticket that never entered
        result = await scan(client, world, "X1", ticket.code)

    assert result["decision"] == "DENY"
    assert result["reason"] == reason
    async with db() as session:
        stored = await session.get(Passage, uuid.UUID(result["passage_id"]))
    assert stored is not None and stored.status.value == "REJECTED" and stored.reject_reason.value == reason


async def test_ticket_of_another_venue_is_wrong_event(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    here, elsewhere = await build_world(db), await build_world(db)
    foreign = await add_prebooked_ticket(db, elsewhere)

    assert (await scan(client, here, "N1", foreign.code))["reason"] == "WRONG_EVENT"


# --- idempotency ---


async def test_retried_scan_returns_the_same_decision_and_creates_one_passage(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    scan_id = sid()

    first = await scan(client, world, "N1", ticket.code, scan_id)
    retry = await scan(client, world, "N1", ticket.code, scan_id)

    assert retry == first  # byte-for-byte the stored response, not "ENTRY_IN_PROGRESS"
    async with db() as session:
        count = await session.scalar(
            select(func.count()).select_from(Passage).where(Passage.idempotency_key == scan_id)
        )
    assert count == 1


async def test_scan_id_of_another_device_is_409(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    scan_id = sid()
    await scan(client, world, "N1", ticket.code, scan_id)

    response = await client.post(
        "/gate/v1/entry/scan", json={"scan_id": scan_id, "ticket_code": ticket.code}, headers=world.headers("N2")
    )

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_complete_twice_is_fine(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    opened = await scan(client, world, "N1", ticket.code)

    first = await complete(client, world, "N1", opened["passage_id"])
    second = await complete(client, world, "N1", opened["passage_id"])

    assert first.json() == second.json()
    assert (await zone(db, world)).prebook_inside == 1  # counted once


async def test_another_gate_cannot_complete_my_passage(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    opened = await scan(client, world, "N1", ticket.code)

    assert (await complete(client, world, "N2", opened["passage_id"])).status_code == 404


# --- B: drive-up issue + D: exit with fee ---


async def test_drive_up_issue_takes_a_place_and_prints(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)

    result = await issue(client, world)

    assert result["decision"] == "OPEN"
    assert result["zone"] == "A"  # priority 1 before B (priority 2)
    assert result["print"]["ticket_code"].startswith("EP1-")
    assert (await zone(db, world)).drive_up_occupied == 1


async def test_drive_up_fills_zones_in_priority_order_then_denies(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db, zones={"A": (3, 1, 1), "B": (1, 0, 2), "VIP": (5, 5, 0)})

    zones_given = [(await issue(client, world)).get("zone") for _ in range(4)]
    full = await issue(client, world)

    assert zones_given == ["A", "A", "B", None]  # A has 3 - 1 pre-booked = 2 drive-up places, B has 1
    assert full["decision"] == "DENY" and full["reason"] == "CAR_PARK_FULL"
    assert (await zone(db, world, "VIP")).drive_up_occupied == 0  # priority 0 = never used for drive-up


async def test_drive_up_exit_free_within_free_minutes(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    issued = await issue(client, world)
    await complete(client, world, "N1", issued["passage_id"])

    leaving = await scan(client, world, "X1", issued["print"]["ticket_code"])
    assert leaving["decision"] == "OPEN"
    await complete(client, world, "X1", leaving["passage_id"])
    assert (await zone(db, world)).drive_up_occupied == 0  # place released


async def test_drive_up_exit_after_hours_requires_payment(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    issued = await issue(client, world)
    await complete(client, world, "N1", issued["passage_id"])
    code = issued["print"]["ticket_code"]
    async with db() as session, session.begin():  # time travel: the car entered 3 h 35 min ago
        await session.execute(
            update(Ticket).where(Ticket.code == code).values(entered_at=datetime.now(UTC) - timedelta(minutes=215))
        )

    result = await scan(client, world, "X1", code)

    assert result["decision"] == "PAYMENT_REQUIRED"
    assert result["amount_minor"] == 120_000 and result["currency"] == "RSD"
    assert result["display_text"] == "Please pay 1,200.00 RSD"
    async with db() as session:
        ticket = await session.scalar(select(Ticket).where(Ticket.code == code))
    assert ticket is not None and ticket.status.value == "INSIDE"  # stays inside until paid


async def test_abort_drive_up_voids_ticket_and_releases_place(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    issued = await issue(client, world)

    aborted = await client.post(f"/gate/v1/passages/{issued['passage_id']}/abort", headers=world.headers("N1"))
    late_complete = await complete(client, world, "N1", issued["passage_id"])

    assert aborted.json()["ticket_status"] == "VOID"
    assert (await zone(db, world)).drive_up_occupied == 0
    assert late_complete.status_code == 409


async def test_expired_entry_hold_is_released_lazily_on_next_scan(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Barrier opened, car never passed, 60 s later the driver scans again: they must not be stuck."""
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    first = await scan(client, world, "N1", ticket.code)
    async with db() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(hold_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    second = await scan(client, world, "N2", ticket.code)

    assert second["decision"] == "OPEN"
    async with db() as session:
        old = await session.get(Passage, uuid.UUID(first["passage_id"]))
    assert old is not None and old.status.value == "EXPIRED"


async def test_active_hold_blocks_a_second_entry(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db)
    ticket = await add_prebooked_ticket(db, world)
    await scan(client, world, "N1", ticket.code)

    assert (await scan(client, world, "N2", ticket.code))["reason"] == "ENTRY_IN_PROGRESS"


# --- side effects are recorded in the outbox, in the same transaction ---


async def test_decisions_write_outbox_messages(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db)
    issued = await issue(client, world, request_id="req-trace-12345")
    await complete(client, world, "N1", issued["passage_id"])

    async with db() as session:
        messages = (await session.scalars(select(OutboxMessage).order_by(OutboxMessage.id))).all()

    assert [(m.type, m.routing_key) for m in messages] == [
        ("passage.authorized", "entry-events"),
        ("passage.completed", "entry-events"),
    ]
    assert all(m.published_at is None for m in messages)
    assert messages[1].payload["data"]["passage_id"] == issued["passage_id"]


# --- heartbeat + commands ---


async def test_heartbeat_marks_online_and_delivers_commands(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db)
    async with db() as session, session.begin():
        command = DeviceCommand(device_id=world.device_ids["N1"], command="OPEN_BARRIER", reason="stuck car")
        session.add(command)

    beat = await client.post(
        "/gate/v1/heartbeat",
        json={"firmware_version": "1.2.3", "printer_status": "PAPER_LOW"},
        headers=world.headers("N1"),
    )
    ack = await client.post(f"/gate/v1/commands/{beat.json()['commands'][0]['id']}/ack", headers=world.headers("N1"))
    second = await client.post("/gate/v1/heartbeat", json={}, headers=world.headers("N1"))

    assert beat.status_code == 200 and beat.json()["commands"][0]["command"] == "OPEN_BARRIER"
    assert ack.status_code == 204
    assert second.json()["commands"] == []
    async with db() as session:
        device = await session.get(Device, world.device_ids["N1"])
        alert = await session.scalar(select(OutboxMessage).where(OutboxMessage.type == "alert.printer"))
    assert device is not None and device.status.value == "ONLINE" and device.firmware_version == "1.2.3"
    assert alert is not None
