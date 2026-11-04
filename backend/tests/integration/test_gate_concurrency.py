"""Concurrency: the invariants must hold when many requests arrive at the same instant (docs/02-architecture.md §5).

asyncio.gather fires all requests together; the app's connection pool runs up to 15 real Postgres transactions in
parallel, so these are genuine races between database transactions, not just interleaved Python.
"""

import asyncio
import uuid
from collections import Counter

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.db.models import EventZone, Passage, Ticket
from eventpark.domain.tickets import TicketType

from .gate_world import add_prebooked_ticket, build_world

pytestmark = pytest.mark.integration


async def test_200_drive_ups_for_10_places_give_exactly_10_tickets(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """THE race of the project: many lanes pressing the ticket button for the last places at the same millisecond."""
    world = await build_world(db, zones={"A": (12, 2, 1)}, entry_gates=("N1", "N2", "N3", "N4"))

    async def press(i: int) -> str:
        gate = f"N{i % 4 + 1}"
        response = await client.post(
            "/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers(gate)
        )
        assert response.status_code == 200, response.text
        return str(response.json()["decision"])

    decisions = Counter(await asyncio.gather(*(press(i) for i in range(200))))

    assert decisions == {"OPEN": 10, "DENY": 190}
    async with db() as session:
        zone = await session.get(EventZone, world.zone_ids["A"])
        tickets = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.type == TicketType.DRIVE_UP)
        )
    assert zone is not None and zone.drive_up_occupied == 10 == zone.capacity - zone.prebook_quota
    assert tickets == 10  # counter and reality agree


async def test_same_ticket_at_two_entry_gates_at_once_opens_one(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    world = await build_world(db, entry_gates=("N1", "N2", "N3", "N4", "N5"))
    ticket = await add_prebooked_ticket(db, world)

    async def scan(gate: str) -> tuple[str, str | None]:
        response = await client.post(
            "/gate/v1/entry/scan",
            json={"scan_id": str(uuid.uuid4()), "ticket_code": ticket.code},
            headers=world.headers(gate),
        )
        body = response.json()
        return body["decision"], body["reason"]

    results = await asyncio.gather(*(scan(f"N{i}") for i in range(1, 6)))

    assert sorted(results) == [("DENY", "ENTRY_IN_PROGRESS")] * 4 + [("OPEN", None)]


async def test_same_scan_id_sent_twice_at_once_gives_identical_answers_and_one_passage(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """A device times out and retries while the first request is still being processed."""
    world = await build_world(db)
    scan_id = str(uuid.uuid4())

    async def press() -> dict[str, object]:
        response = await client.post("/gate/v1/entry/issue", json={"request_id": scan_id}, headers=world.headers("N1"))
        assert response.status_code == 200, response.text
        return dict(response.json())

    answers = await asyncio.gather(*(press() for _ in range(10)))

    assert all(a == answers[0] for a in answers)
    assert answers[0]["decision"] == "OPEN"
    async with db() as session:
        passages = await session.scalar(
            select(func.count()).select_from(Passage).where(Passage.idempotency_key == scan_id)
        )
        zone = await session.get(EventZone, world.zone_ids["A"])
    assert passages == 1
    assert zone is not None and zone.drive_up_occupied == 1  # one place, not ten


async def test_concurrent_complete_and_abort_leave_consistent_counters(
    client: AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Complete and abort racing for the same passage: exactly one wins, counters match the ticket's final state."""
    world = await build_world(db)
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()
    pid = issued["passage_id"]

    results = await asyncio.gather(
        client.post(f"/gate/v1/passages/{pid}/complete", headers=world.headers("N1")),
        client.post(f"/gate/v1/passages/{pid}/abort", headers=world.headers("N1")),
    )

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    async with db() as session:
        ticket = await session.scalar(select(Ticket).where(Ticket.code == issued["print"]["ticket_code"]))
        zone = await session.get(EventZone, world.zone_ids["A"])
    assert ticket is not None and zone is not None
    assert (ticket.status.value, zone.drive_up_occupied) in {("INSIDE", 1), ("VOID", 0)}
