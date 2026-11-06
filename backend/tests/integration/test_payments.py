"""Phase 5: exit-lane payments, online booking, webhooks (fakepay mocked with respx at http://fakepay.test)."""

import asyncio
import json
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.config import Settings
from eventpark.core.webhooks import expected_signature
from eventpark.db.models import Booking, EventZone, OutboxMessage, Payment, Ticket
from eventpark.domain.roles import Role

from .conftest import TEST_WEBHOOK_SECRET, HeadersFactory
from .gate_world import World, build_world

pytestmark = pytest.mark.integration
FAKEPAY = "http://fakepay.test"


@pytest.fixture
def fakepay() -> Any:
    with respx.mock(base_url=FAKEPAY, assert_all_called=False) as mock:
        yield mock


# --- exit lane payments ---


async def _car_inside_with_fee(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], world: World
) -> dict[str, Any]:
    """Drive-up car that entered 215 min ago, now at the exit: PAYMENT_REQUIRED 1,200.00 RSD."""
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()
    await client.post(f"/gate/v1/passages/{issued['passage_id']}/complete", headers=world.headers("N1"))
    code = issued["print"]["ticket_code"]
    async with db() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.code == code).values(entered_at=datetime.now(UTC) - timedelta(minutes=215))
        )
    scan = (
        await client.post(
            "/gate/v1/exit/scan", json={"scan_id": str(uuid.uuid4()), "ticket_code": code}, headers=world.headers("X1")
        )
    ).json()
    assert scan["decision"] == "PAYMENT_REQUIRED"
    return scan


async def _pay(
    client: AsyncClient, world: World, passage_id: str, request_id: str, token: str = "tok_ok"
) -> dict[str, Any]:
    response = await client.post(
        "/gate/v1/exit/pay",
        json={"request_id": request_id, "passage_id": passage_id, "card_token": token},
        headers=world.headers("X1"),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_exit_payment_success_opens_and_records_payment(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter
) -> None:
    world = await build_world(db)
    scan = await _car_inside_with_fee(client, db, world)
    charge = fakepay.post("/v1/terminal/charges").mock(
        return_value=httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})
    )

    result = await _pay(client, world, scan["passage_id"], "pay-req-0001")

    assert result["decision"] == "OPEN" and result["amount_minor"] == 120_000
    assert json.loads(charge.calls[0].request.content)["amount"] == 120_000
    assert charge.calls[0].request.headers["idempotency-key"] == "exit:pay-req-0001"
    done = await client.post(f"/gate/v1/passages/{scan['passage_id']}/complete", headers=world.headers("X1"))
    assert done.json()["ticket_status"] == "EXITED"
    async with db() as session:
        payment = await session.scalar(select(Payment))
        succeeded = await session.scalar(
            select(func.count()).select_from(OutboxMessage).where(OutboxMessage.type == "payment.succeeded")
        )
    assert payment is not None and payment.status.value == "SUCCEEDED" and payment.provider_ref == "ch_1"
    assert succeeded == 1


async def test_declined_card_then_another_card(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter
) -> None:
    world = await build_world(db)
    scan = await _car_inside_with_fee(client, db, world)
    fakepay.post("/v1/terminal/charges").mock(
        side_effect=[
            httpx.Response(200, json={"id": "ch_1", "status": "declined", "decline_reason": "card_declined"}),
            httpx.Response(200, json={"id": "ch_2", "status": "succeeded"}),
        ]
    )

    declined = await _pay(client, world, scan["passage_id"], "pay-req-0001", "tok_decline")
    retry_same_id = await _pay(client, world, scan["passage_id"], "pay-req-0001", "tok_decline")
    second_card = await _pay(client, world, scan["passage_id"], "pay-req-0002", "tok_ok")

    assert declined["decision"] == "DECLINED" and retry_same_id["decision"] == "DECLINED"  # replay, no new charge
    assert second_card["decision"] == "OPEN"


async def test_provider_timeout_is_unknown_then_resolved_with_the_same_key(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter
) -> None:
    """Did the customer pay? We don't know -> PAYMENT_PENDING. Retrying with the same id asks the provider again
    with the same Idempotency-Key, so the card is charged at most once."""
    world = await build_world(db)
    scan = await _car_inside_with_fee(client, db, world)
    charge = fakepay.post("/v1/terminal/charges").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})]
    )

    first = await _pay(client, world, scan["passage_id"], "pay-req-0001", "tok_slow")
    async with db() as session:
        payment = await session.scalar(select(Payment))
    assert first["decision"] == "PAYMENT_PENDING"
    assert payment is not None and payment.status.value == "UNKNOWN"

    second = await _pay(client, world, scan["passage_id"], "pay-req-0001", "tok_slow")

    assert second["decision"] == "OPEN"
    assert {c.request.headers["idempotency-key"] for c in charge.calls} == {"exit:pay-req-0001"}
    async with db() as session:
        payments = await session.scalar(select(func.count()).select_from(Payment))
    assert payments == 1


async def test_after_paying_the_next_scan_is_free_within_grace(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter
) -> None:
    world = await build_world(db)
    scan = await _car_inside_with_fee(client, db, world)
    fakepay.post("/v1/terminal/charges").mock(
        return_value=httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})
    )
    await _pay(client, world, scan["passage_id"], "pay-req-0001")
    await client.post(f"/gate/v1/passages/{scan['passage_id']}/abort", headers=world.headers("X1"))  # car reversed

    async with db() as session:
        ticket = await session.scalar(select(Ticket).where(Ticket.paid_minor > 0))
    assert ticket is not None
    again = (
        await client.post(
            "/gate/v1/exit/scan",
            json={"scan_id": str(uuid.uuid4()), "ticket_code": ticket.code},
            headers=world.headers("X1"),
        )
    ).json()

    assert again["decision"] == "OPEN"  # paid a moment ago: within the 15-min exit grace


async def test_nothing_to_pay_is_409(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter
) -> None:
    world = await build_world(db)
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()

    response = await client.post(
        "/gate/v1/exit/pay",
        json={"request_id": "pay-req-0001", "passage_id": issued["passage_id"], "card_token": "tok_ok"},
        headers=world.headers("X1"),
    )

    assert response.status_code == 404  # not this exit gate's passage


# --- booking + webhook ---


def _webhook(
    intent_id: str,
    status: str = "succeeded",
    event_id: str | None = None,
    t: int | None = None,
    metadata: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "id": event_id or f"evt_{uuid.uuid4().hex}",
            "type": f"payment_intent.{status}",
            "data": {"object": {"id": intent_id, "metadata": metadata or {}}},
        }
    ).encode()
    t = t or int(time.time())
    return body, {
        "Fakepay-Signature": f"t={t},v1={expected_signature(TEST_WEBHOOK_SECRET, t, body)}",
        "Content-Type": "application/json",
    }


def _intents(fakepay: respx.MockRouter) -> respx.Route:
    counter = iter(range(10_000))

    def make(request: httpx.Request) -> httpx.Response:
        n = next(counter)
        return httpx.Response(
            201, json={"id": f"pi_{n}", "status": "requires_payment", "checkout_url": f"{FAKEPAY}/checkout/pi_{n}"}
        )

    return fakepay.post("/v1/payment_intents").mock(side_effect=make)


async def _book(
    client: AsyncClient, headers: dict[str, Any], world: World, zone: str = "A", **extra: Any
) -> httpx.Response:
    return await client.post(
        "/public/v1/bookings",
        json={
            "event_id": str(world.event_id),
            "event_zone_id": str(world.zone_ids[zone]),
            "plate": "BG 123-AB",
            **extra,
        },
        headers=headers,
    )


async def test_booking_hold_webhook_confirm_and_ticket(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    intents = _intents(fakepay)
    driver = await auth_headers(Role.DRIVER)

    booked = await _book(client, driver, world)
    assert booked.status_code == 201, booked.text
    body = booked.json()
    assert body["status"] == "PENDING_PAYMENT" and body["checkout_url"].endswith("/checkout/pi_0")
    assert intents.calls[0].request.headers["idempotency-key"] == f"booking:{body['id']}"
    sent = json.loads(intents.calls[0].request.content)
    assert sent["webhook_url"].endswith("/webhooks/fakepay") and sent["amount"] == 80_000
    async with db() as session:
        assert (await session.get(EventZone, world.zone_ids["A"])).prebook_held == 1  # type: ignore[union-attr]

    payload, headers = _webhook("pi_0")
    hook = await client.post("/webhooks/fakepay", content=payload, headers=headers)
    assert hook.json() == {"result": "processed"}

    view = (await client.get(f"/public/v1/bookings/{body['id']}", headers=driver)).json()
    assert view["status"] == "CONFIRMED" and view["ticket_code"].startswith("EP1-") and view["checkout_url"] is None
    # the new ticket works at the gate
    scan = (
        await client.post(
            "/gate/v1/entry/scan",
            json={"scan_id": str(uuid.uuid4()), "ticket_code": view["ticket_code"]},
            headers=world.headers("N1"),
        )
    ).json()
    assert scan["decision"] == "OPEN"


async def test_duplicate_webhooks_create_one_ticket(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    _intents(fakepay)
    booking = (await _book(client, await auth_headers(Role.DRIVER), world)).json()
    payload, headers = _webhook("pi_0", event_id="evt_same")

    results = await asyncio.gather(
        *(client.post("/webhooks/fakepay", content=payload, headers=headers) for _ in range(10))
    )

    assert Counter(r.json()["result"] for r in results) == {"processed": 1, "duplicate": 9}
    async with db() as session:
        tickets = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.booking_id == uuid.UUID(booking["id"]))
        )
    assert tickets == 1


async def test_failed_payment_releases_the_hold(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    _intents(fakepay)
    driver = await auth_headers(Role.DRIVER)
    booking = (await _book(client, driver, world)).json()

    payload, headers = _webhook("pi_0", status="failed")
    await client.post("/webhooks/fakepay", content=payload, headers=headers)

    assert (await client.get(f"/public/v1/bookings/{booking['id']}", headers=driver)).json()["status"] == "CANCELLED"
    async with db() as session:
        assert (await session.get(EventZone, world.zone_ids["A"])).prebook_held == 0  # type: ignore[union-attr]


@pytest.mark.parametrize("problem", ["tampered", "wrong_secret", "too_old", "missing"])
async def test_bad_webhook_signatures_are_rejected(client: AsyncClient, problem: str) -> None:
    payload, headers = _webhook("pi_0", t=int(time.time()) - (600 if problem == "too_old" else 0))
    if problem == "tampered":
        payload = payload.replace(b"succeeded", b"failed")
    elif problem == "wrong_secret":
        t = int(time.time())
        headers["Fakepay-Signature"] = f"t={t},v1={expected_signature('attacker-secret', t, payload)}"
    elif problem == "missing":
        del headers["Fakepay-Signature"]

    response = await client.post("/webhooks/fakepay", content=payload, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SIGNATURE"


async def test_expired_hold_is_released_when_the_next_driver_books(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db, zones={"A": (10, 1, 1)})  # quota of 1
    _intents(fakepay)
    first = (await _book(client, await auth_headers(Role.DRIVER), world)).json()
    sold_out = await _book(client, await auth_headers(Role.DRIVER), world)
    assert sold_out.json()["code"] == "SOLD_OUT"
    async with db() as session, session.begin():  # 15 minutes pass
        await session.execute(
            update(Booking)
            .where(Booking.id == uuid.UUID(first["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    later = await _book(client, await auth_headers(Role.DRIVER), world)

    assert later.status_code == 201
    async with db() as session:
        old = await session.get(Booking, uuid.UUID(first["id"]))
    assert old is not None and old.status.value == "EXPIRED"


async def test_drivers_only_see_their_own_bookings(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    _intents(fakepay)
    mine = (await _book(client, await auth_headers(Role.DRIVER), world)).json()
    other_driver = await auth_headers(Role.DRIVER)

    response = await client.get(f"/public/v1/bookings/{mine['id']}", headers=other_driver)

    assert response.status_code == 404  # not 403: don't even reveal that it exists
    assert (await client.get("/public/v1/bookings", headers=other_driver)).json() == []


async def test_idempotency_key_makes_double_clicks_safe(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    _intents(fakepay)
    driver = {**(await auth_headers(Role.DRIVER)), "Idempotency-Key": "click-123456"}

    first = (await _book(client, driver, world)).json()
    second = (await _book(client, driver, world)).json()

    assert first["id"] == second["id"]
    async with db() as session:
        assert (await session.get(EventZone, world.zone_ids["A"])).prebook_held == 1  # type: ignore[union-attr]


async def test_provider_down_keeps_the_hold_and_checkout_can_be_retried(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    fakepay.post("/v1/payment_intents").mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(201, json={"id": "pi_9", "status": "requires_payment", "checkout_url": "x"}),
        ]
    )
    driver = await auth_headers(Role.DRIVER)

    booking = (await _book(client, driver, world)).json()
    assert booking["status"] == "PENDING_PAYMENT" and booking["checkout_url"] is None
    retried = (await client.post(f"/public/v1/bookings/{booking['id']}/checkout", headers=driver)).json()

    assert retried["checkout_url"].endswith("/checkout/pi_9")


# --- VIP spaces under contention ---


async def test_100_drivers_for_the_last_5_vip_spaces(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    """Everyone wants one of 5 spaces (several want the SAME space): exactly 5 holds, each space at most once."""
    world = await build_world(db, zones={"VIP": (5, 5, 0)}, vip_spaces=5)
    _intents(fakepay)
    drivers = [await auth_headers(Role.DRIVER) for _ in range(100)]
    spaces = list(world.spaces.values())

    async def book(i: int) -> str:
        response = await _book(client, drivers[i], world, "VIP", space_id=str(spaces[i % 5]))
        return "held" if response.status_code == 201 else response.json()["code"]

    outcomes = Counter(await asyncio.gather(*(book(i) for i in range(100))))

    assert outcomes["held"] == 5
    assert set(outcomes) <= {"held", "SOLD_OUT", "SPACE_TAKEN"}
    async with db() as session:
        zone = await session.get(EventZone, world.zone_ids["VIP"])
        per_space = (await session.execute(select(Booking.space_id, func.count()).group_by(Booking.space_id))).all()
    assert zone is not None and zone.prebook_held == 5
    assert sorted(count for _, count in per_space) == [1, 1, 1, 1, 1]


async def test_vip_zone_requires_a_space(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db, zones={"VIP": (5, 5, 0)}, vip_spaces=5)
    _intents(fakepay)

    response = await _book(client, await auth_headers(Role.DRIVER), world, "VIP")

    assert response.status_code == 422 and response.json()["code"] == "SPACE_REQUIRED"


async def test_public_event_shows_availability_and_free_spaces(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db, zones={"A": (10, 4, 1), "VIP": (5, 5, 0)}, vip_spaces=5)
    _intents(fakepay)
    await _book(client, await auth_headers(Role.DRIVER), world, "VIP", space_id=str(world.spaces["VIP-02"]))

    detail = (await client.get(f"/public/v1/events/{world.event_id}")).json()
    spaces = (await client.get(f"/public/v1/events/{world.event_id}/zones/{world.zone_ids['VIP']}/spaces")).json()

    zones = {z["zone_code"]: z for z in detail["zones"]}
    assert zones["A"]["available"] == 4 and zones["VIP"]["available"] == 4 and zones["VIP"]["has_spaces"]
    assert "VIP-02" not in [s["label"] for s in spaces] and len(spaces) == 4


async def test_webhook_that_beats_our_commit_is_matched_by_booking_id(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    """Phase 9 race: the provider's webhook arrives before we stored the intent id on the payment."""
    world = await build_world(db)
    _intents(fakepay)
    driver = await auth_headers(Role.DRIVER)
    booking = (await _book(client, driver, world)).json()
    async with db() as session, session.begin():  # rewind to "create_intent returned, provider_ref not saved yet"
        await session.execute(
            update(Payment).where(Payment.booking_id == uuid.UUID(booking["id"])).values(provider_ref=None)
        )

    payload, headers = _webhook("pi_0", metadata={"booking_id": booking["id"]})
    hook = await client.post("/webhooks/fakepay", content=payload, headers=headers)

    assert hook.json() == {"result": "processed"}
    assert (await client.get(f"/public/v1/bookings/{booking['id']}", headers=driver)).json()["status"] == "CONFIRMED"


async def test_unknown_intent_is_retryable_and_not_recorded(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], fakepay: respx.MockRouter, auth_headers: HeadersFactory
) -> None:
    """503 + nothing stored: the provider's retry of the SAME event is processed, not dropped as a duplicate."""
    world = await build_world(db)
    _intents(fakepay)
    driver = await auth_headers(Role.DRIVER)
    booking = (await _book(client, driver, world)).json()
    async with db() as session, session.begin():
        await session.execute(
            update(Payment).where(Payment.booking_id == uuid.UUID(booking["id"])).values(provider_ref=None)
        )
    early, headers = _webhook("pi_0", event_id="evt_retry")
    first = await client.post("/webhooks/fakepay", content=early, headers=headers)
    assert first.status_code == 503 and first.json()["code"] == "UNKNOWN_INTENT"

    async with db() as session, session.begin():  # our commit lands
        await session.execute(
            update(Payment).where(Payment.booking_id == uuid.UUID(booking["id"])).values(provider_ref="pi_0")
        )
    retry = await client.post("/webhooks/fakepay", content=early, headers=_webhook("pi_0", event_id="evt_retry")[1])
    assert retry.json() == {"result": "processed"}


async def test_locked_reads_never_return_stale_cached_objects(
    db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Phase 9 chaos bug: with expire_on_commit=False a SELECT ... FOR UPDATE returned a cached object."""
    from eventpark.db.session import create_sessionmaker

    world = await build_world(db)
    sessions = create_sessionmaker(db.kw["bind"])
    async with sessions() as mine:
        async with mine.begin():
            ez = await mine.get(EventZone, world.zone_ids["A"])  # now cached in `mine`
            assert ez is not None and ez.drive_up_occupied == 0
        async with db() as other, other.begin():  # someone else commits a change
            await other.execute(
                update(EventZone).where(EventZone.id == world.zone_ids["A"]).values(drive_up_occupied=3)
            )
        async with mine.begin():
            locked = await mine.scalar(select(EventZone).where(EventZone.id == world.zone_ids["A"]).with_for_update())
            assert locked is not None and locked.drive_up_occupied == 3  # fresh, not the cached 0
