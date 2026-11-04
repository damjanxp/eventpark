"""Scheduled maintenance jobs (docs/02-architecture.md §3.8), run by worker-maintenance on every `tick` (1/min).

Every job is safe to run twice or concurrently (two maintenance workers, a duplicate tick):
- candidates are re-checked after locking (another run may have handled them a moment ago);
- locks follow the global order event_zone -> ticket -> passage -> payment;
- SKIP LOCKED where a row may be busy (a gate is using it right now): we simply get it on the next tick.
Each job commits its own work in small transactions, so one slow job never holds locks for long.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.db.models import (
    Booking,
    Device,
    Event,
    EventZone,
    OutboxMessage,
    Passage,
    Payment,
    ProcessedMessage,
    Ticket,
)
from eventpark.domain.events import EventSnapshot, EventStatus, automatic_transition
from eventpark.domain.payments import BookingStatus, PaymentKind, PaymentOutcome, PaymentStatus
from eventpark.domain.tickets import PassageStatus
from eventpark.domain.venues import DeviceStatus
from eventpark.messaging import outbox
from eventpark.services import audit
from eventpark.services.bookings import release_expired_holds
from eventpark.services.exit_payment import record_exit_payment
from eventpark.services.gate import GateSettings, release_passage
from eventpark.services.provider import PaymentProvider, ProviderUnavailable
from eventpark.services.webhooks import handle_payment_event

log = structlog.get_logger()

DEVICE_OFFLINE_AFTER = timedelta(seconds=60)
RECONCILE_AFTER = timedelta(minutes=5)
RETENTION = timedelta(days=7)
BATCH = 200


@dataclass
class TickReport:
    expired_passages: int = 0
    expired_bookings: int = 0
    event_transitions: int = 0
    devices_offline: int = 0
    payments_reconciled: int = 0
    rows_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


async def expire_passages(sessions: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """AUTHORIZED / PENDING_PAYMENT passages past their deadline: release what they hold (barrier opened, no car)."""
    async with sessions() as session:
        candidates = (
            await session.execute(
                select(Passage.id, Ticket.event_zone_id, Passage.ticket_id)
                .join(Ticket, Ticket.id == Passage.ticket_id)
                .where(
                    Passage.status.in_([PassageStatus.AUTHORIZED, PassageStatus.PENDING_PAYMENT]),
                    Passage.deadline_at < now,
                )
                .order_by(Passage.deadline_at)
                .limit(BATCH)
            )
        ).all()
        await session.commit()
        done = 0
        for passage_id, event_zone_id, ticket_id in candidates:
            async with session.begin():
                event_zone = await session.scalar(
                    select(EventZone).where(EventZone.id == event_zone_id).with_for_update(skip_locked=True)
                )
                if event_zone is None:
                    continue  # a gate is busy with this zone right now: next tick
                ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
                passage = await session.scalar(select(Passage).where(Passage.id == passage_id).with_for_update())
                if ticket is None or passage is None:
                    continue
                if (
                    passage.status not in (PassageStatus.AUTHORIZED, PassageStatus.PENDING_PAYMENT)
                    or not passage.deadline_at
                ):
                    continue  # completed/aborted meanwhile
                if passage.deadline_at >= now:
                    continue
                await release_passage(session, passage, ticket, event_zone, new_status=PassageStatus.EXPIRED)
                outbox.add(
                    session,
                    "passage.expired",
                    {
                        "passage_id": passage.id,
                        "ticket_id": ticket.id,
                        "gate_id": passage.gate_id,
                        "direction": passage.direction,
                    },
                    event_id=passage.event_id,
                    direction=passage.direction.value,
                    producer="worker-maintenance",
                )
                done += 1
    return done


async def expire_bookings(sessions: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Unpaid bookings past their 15 minutes: expire them and give the places back (per zone, under its lock)."""
    async with sessions() as session:
        zone_ids = (
            await session.scalars(
                select(Booking.event_zone_id)
                .where(Booking.status == BookingStatus.PENDING_PAYMENT, Booking.expires_at <= now)
                .distinct()
            )
        ).all()
        await session.commit()
        total = 0
        for event_zone_id in zone_ids:
            async with session.begin():
                event_zone = await session.scalar(
                    select(EventZone).where(EventZone.id == event_zone_id).with_for_update(skip_locked=True)
                )
                if event_zone is None:
                    continue
                expired_ids = await release_expired_holds(session, event_zone, now)
                for booking_id in expired_ids:
                    outbox.add(
                        session,
                        "booking.expired",
                        {"booking_id": booking_id},
                        event_id=event_zone.event_id,
                        producer="worker-maintenance",
                    )
                total += len(expired_ids)
    return total


async def event_transitions(sessions: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """PUBLISHED -> LIVE at gates_open_at, LIVE -> ENDED at gates_close_at (domain/events.py automatic_transition)."""
    async with sessions() as session:
        ids = (
            await session.scalars(select(Event.id).where(Event.status.in_([EventStatus.PUBLISHED, EventStatus.LIVE])))
        ).all()
        await session.commit()
        changed = 0
        for event_id in ids:
            async with session.begin():
                event = await session.scalar(
                    select(Event).where(Event.id == event_id).with_for_update(skip_locked=True)
                )
                if event is None:
                    continue
                snapshot = EventSnapshot(event.gates_open_at, event.gates_close_at, has_pricing=True, zone_count=1)
                target = automatic_transition(event.status, now=now, event=snapshot)
                if target is None:
                    continue
                if target is EventStatus.LIVE:
                    other_live = await session.scalar(
                        select(Event.id).where(
                            Event.venue_id == event.venue_id, Event.status == EventStatus.LIVE, Event.id != event.id
                        )
                    )
                    if other_live is not None:
                        log.warning(
                            "maintenance.go_live_blocked", event_id=str(event.id), live_event_id=str(other_live)
                        )
                        continue  # ADR-025: one LIVE event per venue; the other one must end first
                previous, event.status = event.status, target
                audit.record(
                    session,
                    action=f"event.auto_{target.value.lower()}",
                    target_type="event",
                    target_id=event.id,
                    details={"from": previous.value, "to": target.value},
                )
                changed += 1
    return changed


async def mark_devices_offline(sessions: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with sessions() as session, session.begin():
        rows = (
            await session.execute(
                update(Device)
                .where(Device.status == DeviceStatus.ONLINE, Device.last_heartbeat_at < now - DEVICE_OFFLINE_AFTER)
                .values(status=DeviceStatus.OFFLINE)
                .returning(Device.id, Device.gate_id, Device.name)
            )
        ).all()
        for device_id, gate_id, name in rows:
            outbox.add(
                session,
                "alert.device_offline",
                {"device_id": device_id, "gate_id": gate_id, "device_name": name},
                producer="worker-maintenance",
            )
    return len(rows)


async def reconcile_payments(
    sessions: async_sessionmaker[AsyncSession], provider: PaymentProvider, cfg: GateSettings, now: datetime
) -> int:
    """'Never trust that you received every webhook.' Ask the provider about payments stuck in PENDING/UNKNOWN."""
    async with sessions() as session:
        stuck = (
            await session.execute(
                select(Payment.id, Payment.kind, Payment.idempotency_key, Payment.provider_ref)
                .where(
                    Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.UNKNOWN]),
                    Payment.updated_at < now - RECONCILE_AFTER,
                )
                .limit(BATCH)
            )
        ).all()
        await session.commit()
        resolved = 0
        for payment_id, kind, key, provider_ref in stuck:
            if kind in (PaymentKind.EXIT_FEE, PaymentKind.LOST_TICKET):
                result = await provider.get_charge(key)
                if result.outcome is PaymentOutcome.NO_ANSWER:
                    continue
                await record_exit_payment(session, None, payment_id=payment_id, result=result, now=now, cfg=cfg)
                resolved += 1
            elif kind is PaymentKind.BOOKING and provider_ref:
                try:
                    intent = await provider.get_intent(provider_ref)
                except ProviderUnavailable:
                    continue
                if intent is None or intent.status not in ("succeeded", "failed"):
                    continue
                # Re-use the webhook path with a synthetic event: same rules, same dedupe.
                event = {
                    "id": f"reconcile:{payment_id}:{intent.status}",
                    "type": f"payment_intent.{intent.status}",
                    "data": {"object": {"id": intent.intent_id}},
                }
                if await handle_payment_event(session, event, now=now) == "processed":
                    resolved += 1
    return resolved


async def cleanup(sessions: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with sessions() as session, session.begin():
        outbox_rows = await session.execute(
            delete(OutboxMessage).where(
                OutboxMessage.published_at.is_not(None), OutboxMessage.published_at < now - RETENTION
            )
        )
        processed = await session.execute(
            delete(ProcessedMessage).where(ProcessedMessage.processed_at < now - RETENTION)
        )
    return int(outbox_rows.rowcount or 0) + int(processed.rowcount or 0)  # type: ignore[attr-defined]


async def run_tick(
    sessions: async_sessionmaker[AsyncSession], provider: PaymentProvider, cfg: GateSettings, now: datetime
) -> TickReport:
    report = TickReport()
    jobs: dict[str, Callable[[], Awaitable[int]]] = {
        "expired_passages": lambda: expire_passages(sessions, now),
        "expired_bookings": lambda: expire_bookings(sessions, now),
        "event_transitions": lambda: event_transitions(sessions, now),
        "devices_offline": lambda: mark_devices_offline(sessions, now),
        "payments_reconciled": lambda: reconcile_payments(sessions, provider, cfg, now),
        "rows_cleaned": lambda: cleanup(sessions, now),
    }
    for name, job in jobs.items():
        try:
            setattr(report, name, await job())
        except Exception as exc:  # one broken job must not stop the others
            log.exception("maintenance.job_failed", job=name)
            report.errors.append(f"{name}: {type(exc).__name__}")
    log.info("maintenance.tick", **{k: v for k, v in report.__dict__.items() if v})
    return report
