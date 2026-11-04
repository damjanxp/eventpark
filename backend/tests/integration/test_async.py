"""Phase 6: outbox relay -> SQS -> workers -> S3/email/stats, and the maintenance jobs. moto plays S3 and SQS."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.config import Settings
from eventpark.core.mail import Mail, Mailer
from eventpark.core.webhooks import expected_signature
from eventpark.db.models import (
    Booking,
    Device,
    Event,
    EventSummary,
    EventZone,
    HourlyStat,
    OutboxMessage,
    Passage,
    Payment,
    ProcessedMessage,
    Report,
    Ticket,
)
from eventpark.domain.events import EventStatus
from eventpark.domain.roles import Role
from eventpark.messaging.relay import SqsPublisher, relay_once
from eventpark.messaging.schemas import Envelope
from eventpark.services.gate import GateSettings
from eventpark.services.maintenance import run_tick
from eventpark.services.provider import PaymentProvider
from eventpark.storage.s3 import Storage
from eventpark.workers.consumer import Consumer, Handler, process_one
from eventpark.workers.notifications import Notifications, qr_key
from eventpark.workers.reports import Reports
from eventpark.workers.stats import EntryStats, ExitStats, hour_bucket

from .aws_fixtures import BUCKET, AwsEnv
from .conftest import TEST_WEBHOOK_SECRET, HeadersFactory
from .gate_world import World, add_prebooked_ticket, build_world

pytestmark = pytest.mark.integration
CFG = GateSettings(hold=timedelta(seconds=60), payment_window=timedelta(minutes=5))


@pytest.fixture
def settings(settings: Settings, aws_env: AwsEnv) -> Settings:
    """The integration settings + moto endpoints, queues and bucket."""
    return settings.model_copy(
        update={
            "bucket_files": BUCKET,
            "s3_public_endpoint_url": aws_env.endpoint,
            **{
                f"queue_url_{name.replace('-', '_')}": url
                for name, url in aws_env.queue_urls.items()
                if not name.endswith("-dlq")
            },
        }
    )


@pytest.fixture
def sessions(db: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    return db


async def _relay_all(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    publisher, total = SqsPublisher(settings), 0
    async with sessions() as session:
        while n := await relay_once(session, publisher):
            total += n
    return total


async def _issue_and_complete(client: AsyncClient, world: World) -> dict[str, Any]:
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()
    await client.post(f"/gate/v1/passages/{issued['passage_id']}/complete", headers=world.headers("N1"))
    return dict(issued)


# --- relay ---


async def test_relay_publishes_committed_rows_once_and_marks_them(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings, aws_env: AwsEnv
) -> None:
    world = await build_world(db)
    await _issue_and_complete(client, world)

    published = await _relay_all(db, settings)
    again = await _relay_all(db, settings)

    assert (published, again) == (2, 0)
    bodies = [json.loads(b) for b in aws_env.drain("entry-events")]
    assert sorted(b["type"] for b in bodies) == ["passage.authorized", "passage.completed"]
    async with db() as session:
        unpublished = await session.scalar(
            select(func.count()).select_from(OutboxMessage).where(OutboxMessage.published_at.is_(None))
        )
    assert unpublished == 0


async def test_two_relays_in_parallel_never_send_a_row_twice(
    db: async_sessionmaker[AsyncSession], settings: Settings, aws_env: AwsEnv
) -> None:
    """FOR UPDATE SKIP LOCKED: each relay takes rows the other isn't holding."""
    async with db() as session, session.begin():
        for i in range(250):
            session.add(
                OutboxMessage(
                    message_id=uuid.uuid4(),
                    type="tick",
                    routing_key="maintenance",
                    payload={
                        "message_id": str(uuid.uuid4()),
                        "type": "tick",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "data": {"n": i},
                    },
                )
            )

    counts = await asyncio.gather(_relay_all(db, settings), _relay_all(db, settings))

    assert sum(counts) == 250
    numbers = [json.loads(b)["data"]["n"] for b in aws_env.drain("maintenance")]
    assert sorted(numbers) == list(range(250))  # all of them, each exactly once


# --- consumer semantics ---


class CountingHandler(Handler):
    name = "test-counter"
    queue = "maintenance"
    applied = 0
    fail = False

    async def apply(self, session: AsyncSession, msg: Envelope) -> None:
        if CountingHandler.fail:
            raise RuntimeError("boom")
        CountingHandler.applied += 1


def _envelope(message_type: str = "tick", **data: Any) -> Envelope:
    return Envelope(message_id=uuid.uuid4(), type=message_type, occurred_at=datetime.now(UTC), data=data)


async def test_duplicate_message_is_applied_once(db: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    CountingHandler.applied, CountingHandler.fail = 0, False
    handler = CountingHandler(db, settings)
    msg = _envelope()

    first, second = await process_one(handler, msg), await process_one(handler, msg)

    assert (first, second) == (True, False) and CountingHandler.applied == 1


async def test_failed_message_is_not_deleted_and_ends_in_the_dlq(
    db: async_sessionmaker[AsyncSession], settings: Settings, aws_env: AwsEnv
) -> None:
    CountingHandler.applied, CountingHandler.fail = 0, True
    sqs = aws_env.sqs()
    sqs.set_queue_attributes(QueueUrl=aws_env.queue_urls["maintenance"], Attributes={"VisibilityTimeout": "0"})
    sqs.send_message(QueueUrl=aws_env.queue_urls["maintenance"], MessageBody=_envelope().model_dump_json())
    consumer = Consumer(CountingHandler(db, settings), wait_seconds=0)

    for _ in range(6):  # 5 failed receives, then SQS moves it to the DLQ
        await consumer.poll_once()

    assert consumer.stats.failed == 5 and CountingHandler.applied == 0
    assert len(aws_env.drain("maintenance-dlq")) == 1
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(ProcessedMessage)) == 0  # nothing recorded
    CountingHandler.fail = False


# --- workers end to end ---


async def test_entry_and_exit_passages_become_hourly_stats(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings, aws_env: AwsEnv
) -> None:
    world = await build_world(db)
    issued = await _issue_and_complete(client, world)
    await client.post(
        "/gate/v1/entry/scan",
        json={"scan_id": str(uuid.uuid4()), "ticket_code": "EP1-UNKNOWN"},
        headers=world.headers("N2"),
    )
    leaving = (
        await client.post(
            "/gate/v1/exit/scan",
            json={"scan_id": str(uuid.uuid4()), "ticket_code": issued["print"]["ticket_code"]},
            headers=world.headers("X1"),
        )
    ).json()
    await client.post(f"/gate/v1/passages/{leaving['passage_id']}/complete", headers=world.headers("X1"))
    await _relay_all(db, settings)

    entry, exit_ = Consumer(EntryStats(db, settings), wait_seconds=0), Consumer(ExitStats(db, settings), wait_seconds=0)
    while await entry.poll_once() or await exit_.poll_once():
        pass

    async with db() as session:
        totals = (
            await session.execute(
                select(
                    func.sum(HourlyStat.entries),
                    func.sum(HourlyStat.exits),
                    func.sum(HourlyStat.rejections),
                    func.sum(HourlyStat.decision_count),
                ).where(HourlyStat.event_id == world.event_id)
            )
        ).one()
    assert tuple(totals) == (1, 1, 1, 2)  # 1 in, 1 out, 1 rejected; 2 authorized decisions timed


def test_hours_are_bucketed_in_venue_time() -> None:
    moment = datetime(2026, 11, 4, 17, 45, tzinfo=UTC)  # 18:45 in Belgrade (UTC+1 in November)

    bucket = hour_bucket(moment, "Europe/Belgrade")

    assert bucket.hour == 18 and bucket.utcoffset() == timedelta(hours=1)
    assert bucket.astimezone(UTC) == datetime(2026, 11, 4, 17, 0, tzinfo=UTC)


class CaptureMailer(Mailer):
    def __init__(self) -> None:
        super().__init__(None, "test@eventpark.dev")
        self.sent: list[Mail] = []

    async def send(self, mail: Mail) -> None:
        self.sent.append(mail)


async def test_booking_confirmation_makes_qr_in_s3_and_sends_email(
    client: AsyncClient,
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    aws_env: AwsEnv,
    auth_headers: HeadersFactory,
) -> None:
    world = await build_world(db)
    with respx.mock(base_url="http://fakepay.test", assert_all_called=False) as fakepay:
        fakepay.post("/v1/payment_intents").mock(
            return_value=httpx.Response(201, json={"id": "pi_1", "status": "requires_payment", "checkout_url": "x"})
        )
        booking = (
            await client.post(
                "/public/v1/bookings",
                json={"event_id": str(world.event_id), "event_zone_id": str(world.zone_ids["A"])},
                headers=await auth_headers(Role.DRIVER),
            )
        ).json()
    body = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_1"}}}).encode()
    t = int(time.time())
    await client.post(
        "/webhooks/fakepay",
        content=body,
        headers={"Fakepay-Signature": f"t={t},v1={expected_signature(TEST_WEBHOOK_SECRET, t, body)}"},
    )
    await _relay_all(db, settings)
    mailer = CaptureMailer()

    await Consumer(Notifications(db, settings, mailer=mailer), wait_seconds=0).poll_once()

    async with db() as session:
        ticket = await session.scalar(select(Ticket).where(Ticket.booking_id == uuid.UUID(booking["id"])))
    assert ticket is not None and ticket.qr_s3_key == qr_key(ticket.id)
    png = await Storage(settings).get(ticket.qr_s3_key)
    assert png.startswith(b"\x89PNG")
    assert len(mailer.sent) == 1 and mailer.sent[0].attachments[0].filename == "parking-pass.png"
    assert ticket.code in mailer.sent[0].text


async def test_csv_report_is_generated_and_downloadable(
    client: AsyncClient,
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    aws_env: AwsEnv,
    auth_headers: HeadersFactory,
) -> None:
    world = await build_world(db)
    await _issue_and_complete(client, world)
    manager = await auth_headers(Role.EVENT_MANAGER)

    queued = await client.post(f"/admin/v1/events/{world.event_id}/reports", json={"kind": "CSV"}, headers=manager)
    assert queued.status_code == 202 and queued.json()["status"] == "QUEUED"
    not_yet = await client.get(f"/admin/v1/reports/{queued.json()['id']}/download", headers=manager)
    assert not_yet.json()["code"] == "REPORT_NOT_READY"
    await _relay_all(db, settings)
    await Consumer(Reports(db, settings), wait_seconds=0).poll_once()

    report = (await client.get(f"/admin/v1/reports/{queued.json()['id']}", headers=manager)).json()
    assert report["status"] == "READY"
    url = (await client.get(f"/admin/v1/reports/{queued.json()['id']}/download", headers=manager)).json()["url"]
    async with httpx.AsyncClient() as browser:  # what the user's browser does with the presigned URL
        csv_text = (await browser.get(url)).text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("passage_id,gate,direction") and len(lines) == 2  # header + 1 entry passage


async def test_pdf_report(
    client: AsyncClient,
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    aws_env: AwsEnv,
    auth_headers: HeadersFactory,
) -> None:
    world = await build_world(db)
    manager = await auth_headers(Role.ADMIN)
    queued = (
        await client.post(f"/admin/v1/events/{world.event_id}/reports", json={"kind": "PDF"}, headers=manager)
    ).json()
    await _relay_all(db, settings)

    await Consumer(Reports(db, settings), wait_seconds=0).poll_once()

    async with db() as session:
        report = await session.get(Report, uuid.UUID(queued["id"]))
    assert report is not None and report.status == "READY"
    assert (await Storage(settings).get(report.s3_key or "")).startswith(b"%PDF")


async def test_document_upload_goes_browser_to_s3(
    client: AsyncClient,
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    aws_env: AwsEnv,
    auth_headers: HeadersFactory,
) -> None:
    world = await build_world(db)
    manager = await auth_headers(Role.EVENT_MANAGER)
    content = b"%PDF-1.4 traffic plan"

    ticket = (
        await client.post(
            f"/admin/v1/events/{world.event_id}/documents/upload-url",
            json={
                "filename": "../../etc/Traffic Plan.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(content),
            },
            headers=manager,
        )
    ).json()
    early = await client.post(f"/admin/v1/documents/{ticket['document']['id']}/confirm", headers=manager)
    async with httpx.AsyncClient() as browser:
        put = await browser.put(ticket["upload_url"], content=content, headers={"Content-Type": "application/pdf"})
    confirmed = await client.post(f"/admin/v1/documents/{ticket['document']['id']}/confirm", headers=manager)
    listed = (await client.get(f"/admin/v1/events/{world.event_id}/documents", headers=manager)).json()

    assert early.json()["code"] == "UPLOAD_MISSING"
    assert put.status_code == 200 and confirmed.json()["status"] == "READY"
    assert ticket["document"]["filename"] == "Traffic_Plan.pdf"  # path stripped, unsafe characters replaced
    assert len(listed) == 1 and listed[0]["download_url"].startswith(aws_env.endpoint)


async def test_document_type_and_size_are_checked(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    manager = await auth_headers(Role.EVENT_MANAGER)

    exe = await client.post(
        f"/admin/v1/events/{world.event_id}/documents/upload-url",
        json={"filename": "x.exe", "content_type": "application/x-msdownload", "size_bytes": 10},
        headers=manager,
    )
    huge = await client.post(
        f"/admin/v1/events/{world.event_id}/documents/upload-url",
        json={"filename": "x.pdf", "content_type": "application/pdf", "size_bytes": 50_000_000},
        headers=manager,
    )

    assert exe.json()["code"] == "CONTENT_TYPE_NOT_ALLOWED" and huge.json()["code"] == "FILE_TOO_LARGE"


# --- maintenance ---


def _provider() -> PaymentProvider:
    return PaymentProvider("http://fakepay.test", terminal_timeout=1)


async def test_maintenance_expires_stale_holds_and_releases_places(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    world = await build_world(db)
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()
    prebooked = await add_prebooked_ticket(db, world)
    scanned = (
        await client.post(
            "/gate/v1/entry/scan",
            json={"scan_id": str(uuid.uuid4()), "ticket_code": prebooked.code},
            headers=world.headers("N2"),
        )
    ).json()
    async with db() as session, session.begin():  # a minute passes, no car drives through either barrier
        await session.execute(update(Passage).values(deadline_at=datetime.now(UTC) - timedelta(seconds=1)))

    report = await run_tick(db, _provider(), CFG, datetime.now(UTC))
    second = await run_tick(db, _provider(), CFG, datetime.now(UTC))

    assert report.expired_passages == 2 and second.expired_passages == 0  # idempotent
    async with db() as session:
        zone = await session.get(EventZone, world.zone_ids["A"])
        drive_up = await session.scalar(select(Ticket).where(Ticket.code == issued["print"]["ticket_code"]))
        pass_ = await session.get(Ticket, prebooked.id)
        expired_msgs = await session.scalar(
            select(func.count()).select_from(OutboxMessage).where(OutboxMessage.type == "passage.expired")
        )
    assert zone is not None and zone.drive_up_occupied == 0
    assert drive_up is not None and drive_up.status.value == "VOID"
    assert pass_ is not None and pass_.status.value == "VALID"  # can try again
    assert expired_msgs == 2
    assert scanned["decision"] == "OPEN"


async def test_maintenance_expires_unpaid_bookings(
    db: async_sessionmaker[AsyncSession], settings: Settings, make_user: Any
) -> None:
    world = await build_world(db)
    driver = await make_user(Role.DRIVER)
    async with db() as session, session.begin():
        zone = await session.get(EventZone, world.zone_ids["A"], with_for_update=True)
        assert zone is not None
        zone.prebook_held += 1
        session.add(
            Booking(
                user_id=driver.id,
                event_id=world.event_id,
                event_zone_id=zone.id,
                status="PENDING_PAYMENT",
                price_minor=80_000,
                currency="RSD",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )

    report = await run_tick(db, _provider(), CFG, datetime.now(UTC))

    async with db() as session:
        zone_after = await session.get(EventZone, world.zone_ids["A"])
        booking = await session.scalar(select(Booking))
    assert report.expired_bookings == 1
    assert zone_after is not None and zone_after.prebook_held == 0
    assert booking is not None and booking.status.value == "EXPIRED"


async def test_maintenance_moves_events_by_the_clock_and_marks_silent_devices_offline(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    world = await build_world(db, status=EventStatus.PUBLISHED)  # gates_open_at was an hour ago
    await client.post("/gate/v1/heartbeat", json={}, headers=world.headers("N1"))
    async with db() as session, session.begin():
        await session.execute(
            update(Device)
            .where(Device.id == world.device_ids["N1"])
            .values(last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=2))
        )

    report = await run_tick(db, _provider(), CFG, datetime.now(UTC))

    async with db() as session:
        event = await session.get(Event, world.event_id)
        device = await session.get(Device, world.device_ids["N1"])
        alert = await session.scalar(select(OutboxMessage).where(OutboxMessage.type == "alert.device_offline"))
    assert report.event_transitions == 1 and event is not None and event.status is EventStatus.LIVE
    assert report.devices_offline == 1 and device is not None and device.status.value == "OFFLINE" and alert is not None


async def test_maintenance_reconciles_an_unknown_exit_payment(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """The terminal timed out (UNKNOWN), the device gave up. Reconciliation asks the provider: it was paid."""
    world = await build_world(db)
    issued = await _issue_and_complete(client, world)
    code = issued["print"]["ticket_code"]
    async with db() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.code == code).values(entered_at=datetime.now(UTC) - timedelta(hours=2))
        )
    scan = (
        await client.post(
            "/gate/v1/exit/scan", json={"scan_id": str(uuid.uuid4()), "ticket_code": code}, headers=world.headers("X1")
        )
    ).json()
    with respx.mock(base_url="http://fakepay.test", assert_all_called=False) as fakepay:
        fakepay.post("/v1/terminal/charges").mock(side_effect=httpx.ReadTimeout("slow"))
        await client.post(
            "/gate/v1/exit/pay",
            json={"request_id": "pay-unknown-1", "passage_id": scan["passage_id"], "card_token": "tok_slow"},
            headers=world.headers("X1"),
        )
    async with db() as session, session.begin():
        await session.execute(update(Payment).values(updated_at=datetime.now(UTC) - timedelta(minutes=10)))

    with respx.mock(base_url="http://fakepay.test", assert_all_called=False) as fakepay:
        fakepay.get("/v1/terminal/charges/exit:pay-unknown-1").mock(
            return_value=httpx.Response(200, json={"id": "ch_9", "status": "succeeded"})
        )
        report = await run_tick(db, _provider(), CFG, datetime.now(UTC))

    async with db() as session:
        payment = await session.scalar(select(Payment))
        ticket = await session.scalar(select(Ticket).where(Ticket.code == code))
    assert report.payments_reconciled == 1
    assert payment is not None and payment.status.value == "SUCCEEDED"
    assert ticket is not None and ticket.paid_minor == payment.amount_minor


async def test_archiving_expires_unused_passes_and_computes_the_summary(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], settings: Settings, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    unused = await add_prebooked_ticket(db, world)
    await _issue_and_complete(client, world)
    await _issue_and_complete(client, world)
    manager = await auth_headers(Role.EVENT_MANAGER)

    await client.post(f"/admin/v1/events/{world.event_id}/end", headers=manager)
    archived = await client.post(f"/admin/v1/events/{world.event_id}/archive", headers=manager)
    history = (await client.get("/admin/v1/history/events", headers=manager)).json()

    assert archived.json()["status"] == "ARCHIVED"
    async with db() as session:
        ticket = await session.get(Ticket, unused.id)
        summary = await session.get(EventSummary, world.event_id)
    assert ticket is not None and ticket.status.value == "EXPIRED"
    assert summary is not None and summary.total_entries == 2 and summary.peak_occupancy == 2
    assert history[0]["event_id"] == str(world.event_id)
