"""The gate hot path (docs/01-product-and-domain.md §6, docs/02-architecture.md §3.1-3.2).

Every gate request is answered from ONE database transaction:
  - rows are locked in the fixed order  event_zone -> ticket -> passage  (prevents deadlocks, §5)
  - capacity is taken with ONE conditional UPDATE ... WHERE occupied < capacity RETURNING (no read-then-write)
  - the device's scan_id / request_id is the passage's UNIQUE idempotency key: a retry returns the stored response
  - side effects (stats, dashboard, alerts) are only *recorded* in the outbox, never performed here

Nothing in this module talks to the network: the barrier must never wait for S3, email or SQS.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound, Unauthorized
from eventpark.core.ids import uuid7
from eventpark.core.security import InvalidDeviceKeyError, device_api_key_matches, parse_device_api_key
from eventpark.db.errors import constraint_name
from eventpark.db.models import (
    Device,
    DeviceCommand,
    Event,
    EventPricing,
    EventZone,
    Gate,
    Passage,
    Space,
    Ticket,
    Zone,
)
from eventpark.domain.events import ENTRY_ALLOWED_STATUSES, EXIT_ALLOWED_STATUSES, EventStatus
from eventpark.domain.pricing import FeeRules, calculate_drive_up_fee
from eventpark.domain.tickets import (
    DISPLAY_TEXT,
    Decision,
    PassageStatus,
    RejectReason,
    TicketStatus,
    TicketType,
    entry_rejection,
    exit_rejection,
    mask_ticket_code,
    new_ticket_code,
)
from eventpark.domain.venues import DeviceStatus, GateDirection, PrinterStatus
from eventpark.messaging import outbox

log = structlog.get_logger()

AllocationStrategy = Literal["atomic", "naive"]


# ---------------------------------------------------------------------------------------------------------------------
# Response shape (one for all gate endpoints, docs/02-architecture.md §8)
# ---------------------------------------------------------------------------------------------------------------------


class PrintPayload(BaseModel):
    ticket_code: str
    zone: str
    entered_at: datetime


class GateDecision(BaseModel):
    decision: Decision
    reason: RejectReason | None = None
    passage_id: uuid.UUID | None = None
    amount_minor: int | None = None
    currency: str | None = None
    zone: str | None = None
    space: str | None = None
    display_text: str
    print: PrintPayload | None = None


@dataclass(frozen=True)
class DeviceContext:
    """Who is calling, as proven by the device API key."""

    device_id: uuid.UUID
    gate_id: uuid.UUID
    gate_code: str
    venue_id: uuid.UUID
    direction: GateDirection
    has_printer: bool
    has_terminal: bool


@dataclass(frozen=True)
class GateSettings:
    hold: timedelta
    payment_window: timedelta


def format_money(amount_minor: int, currency: str) -> str:
    """120000, RSD -> '1,200.00 RSD' (integer arithmetic only)."""
    return f"{amount_minor // 100:,}.{amount_minor % 100:02d} {currency}"


# ---------------------------------------------------------------------------------------------------------------------
# Device authentication: `Authorization: Device epd_<prefix>_<secret>` (ADR-024)
# ---------------------------------------------------------------------------------------------------------------------


async def authenticate_device(session: AsyncSession, api_key: str) -> DeviceContext:
    try:
        prefix = parse_device_api_key(api_key)
    except InvalidDeviceKeyError as exc:
        raise Unauthorized("Invalid device key.", code="INVALID_DEVICE_KEY") from exc
    row = (
        await session.execute(
            select(
                Device.id,
                Device.api_key_hash,
                Device.status,
                Gate.id,
                Gate.code,
                Gate.venue_id,
                Gate.direction,
                Gate.has_printer,
                Gate.has_terminal,
            )
            .join(Gate, Gate.id == Device.gate_id)
            .where(Device.api_key_prefix == prefix)
        )
    ).first()
    # End the read-only transaction this lookup opened. We selected plain values, not ORM objects, so nothing gets
    # "expired" by the commit (touching an expired attribute later would trigger a hidden query).
    await session.commit()
    if row is None or not device_api_key_matches(api_key, row[1]):
        raise Unauthorized("Invalid device key.", code="INVALID_DEVICE_KEY")
    device_id, _hash, status, gate_id, gate_code, venue_id, direction, has_printer, has_terminal = row
    if status is DeviceStatus.DISABLED:
        raise Unauthorized("This device is disabled.", code="DEVICE_DISABLED")
    return DeviceContext(
        device_id=device_id,
        gate_id=gate_id,
        gate_code=gate_code,
        venue_id=venue_id,
        direction=direction,
        has_printer=has_printer,
        has_terminal=has_terminal,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------------------------------------------------


async def _stored_decision(session: AsyncSession, key: str, device: DeviceContext) -> GateDecision | None:
    passage = await session.scalar(select(Passage).where(Passage.idempotency_key == key))
    if passage is None:
        return None
    if passage.device_id != device.device_id:
        # A key is only meaningful for the device that generated it; another device reusing it is a bug or an attack.
        raise Conflict("This request id was already used by another device.", code="IDEMPOTENCY_KEY_REUSED")
    return GateDecision.model_validate(passage.response)


async def _idempotent(
    session: AsyncSession, key: str, device: DeviceContext, handle: Callable[[], Awaitable[GateDecision]]
) -> GateDecision:
    """Run `handle` in one transaction, unless this key was answered before (then return that exact answer).

    The race: two copies of the same request arrive at the same time (the device timed out and retried while the
    first is still running). Both find no stored passage, both decide, both INSERT the same idempotency_key: the
    UNIQUE constraint lets exactly one commit. The loser rolls back everything it did and returns the winner's answer.
    """
    try:
        async with session.begin():
            if (stored := await _stored_decision(session, key, device)) is not None:
                return stored
            return await handle()
    except IntegrityError as exc:
        if constraint_name(exc) != "uq_passages_idempotency_key":
            raise
    async with session.begin():
        stored = await _stored_decision(session, key, device)
    if stored is None:  # pragma: no cover (the unique violation means a row exists)
        raise RuntimeError("idempotency key conflict without a stored passage")
    log.info("gate.idempotent_race_resolved", idempotency_key=key)
    return stored


# ---------------------------------------------------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _record(
    session: AsyncSession,
    *,
    device: DeviceContext,
    key: str,
    decision: GateDecision,
    status: PassageStatus,
    started: float,
    now: datetime,
    event_id: uuid.UUID | None,
    ticket_id: uuid.UUID | None = None,
    reason: RejectReason | None = None,
    deadline_at: datetime | None = None,
    amount_due_minor: int | None = None,
) -> GateDecision:
    """Insert the passage (with the response stored for replays) and its outbox message."""
    decision.passage_id = uuid7()
    passage = Passage(
        id=decision.passage_id,
        event_id=event_id,
        gate_id=device.gate_id,
        device_id=device.device_id,
        ticket_id=ticket_id,
        direction=device.direction,
        status=status,
        reject_reason=reason,
        amount_due_minor=amount_due_minor,
        idempotency_key=key,
        authorized_at=now,
        deadline_at=deadline_at,
        decision_ms=_elapsed_ms(started),
        response=decision.model_dump(mode="json"),  # replayed verbatim on retries
    )
    session.add(passage)
    await session.flush()  # the INSERT happens here: a duplicate idempotency key fails now, inside the transaction

    message = {
        PassageStatus.REJECTED: "passage.rejected",
        PassageStatus.AUTHORIZED: "passage.authorized",
        PassageStatus.PENDING_PAYMENT: "passage.authorized",
    }[status]
    outbox.add(
        session,
        message,
        {
            "passage_id": passage.id,
            "ticket_id": ticket_id,
            "gate_id": device.gate_id,
            "direction": device.direction,
            "status": status,
            "reason": reason,
            "decision_ms": passage.decision_ms,
        },
        event_id=event_id,
        direction=device.direction.value,
    )
    log.info(
        "gate.decision",
        decision=decision.decision.value,
        reason=reason.value if reason else None,
        gate=device.gate_code,
        event_id=str(event_id) if event_id else None,
        ticket_id=str(ticket_id) if ticket_id else None,
        passage_id=str(passage.id),
        decision_ms=passage.decision_ms,
    )
    return decision


async def _reject(
    session: AsyncSession,
    reason: RejectReason,
    *,
    device: DeviceContext,
    key: str,
    started: float,
    now: datetime,
    event_id: uuid.UUID | None,
    ticket_id: uuid.UUID | None = None,
) -> GateDecision:
    """Every rejection is stored as a REJECTED passage, for statistics and so a retry gets the same answer."""
    decision = GateDecision(decision=Decision.DENY, reason=reason, display_text=DISPLAY_TEXT[reason])
    return await _record(
        session,
        device=device,
        key=key,
        decision=decision,
        status=PassageStatus.REJECTED,
        started=started,
        now=now,
        event_id=event_id,
        ticket_id=ticket_id,
        reason=reason,
    )


async def _live_event_for_venue(session: AsyncSession, venue_id: uuid.UUID, now: datetime) -> Event | None:
    """The venue's LIVE event (at most one: uq_events_one_live_per_venue), if its gates haven't closed yet."""
    event = await session.scalar(select(Event).where(Event.venue_id == venue_id, Event.status == EventStatus.LIVE))
    if event is None or now >= event.gates_close_at:
        return None
    return event


def _gates_accept(event: Event, statuses: frozenset[EventStatus], now: datetime, *, entry: bool) -> bool:
    if event.status not in statuses:
        return False
    # The maintenance job flips LIVE -> ENDED at gates_close_at once a minute; check the clock lazily too.
    return not (entry and now >= event.gates_close_at)


@dataclass
class _TicketRow:
    ticket: Ticket
    event: Event
    zone_code: str
    space_label: str | None


async def _lock_ticket_by_code(session: AsyncSession, code: str) -> _TicketRow | None:
    # FOR UPDATE OF tickets: the same ticket scanned at two lanes at once -> the second scan waits here until the
    # first commits, then sees ENTERING and is refused. Without the lock both would see VALID and both open.
    row = (
        await session.execute(
            select(Ticket, Event, Zone.code, Space.label)
            .join(Event, Event.id == Ticket.event_id)
            .join(EventZone, EventZone.id == Ticket.event_zone_id)
            .join(Zone, Zone.id == EventZone.zone_id)
            .outerjoin(Space, Space.id == Ticket.space_id)
            .where(Ticket.code == code)
            .with_for_update(of=Ticket)
        )
    ).first()
    return None if row is None else _TicketRow(row[0], row[1], row[2], row[3])


async def _expire_open_passages(session: AsyncSession, ticket_id: uuid.UUID) -> None:
    await session.execute(
        update(Passage)
        .where(
            Passage.ticket_id == ticket_id,
            Passage.status.in_([PassageStatus.AUTHORIZED, PassageStatus.PENDING_PAYMENT]),
        )
        .values(status=PassageStatus.EXPIRED)
    )


def _hold_expired(ticket: Ticket, now: datetime) -> bool:
    return ticket.hold_expires_at is not None and ticket.hold_expires_at <= now


# ---------------------------------------------------------------------------------------------------------------------
# Entry: scan a pre-booked QR code
# ---------------------------------------------------------------------------------------------------------------------


async def entry_scan(
    session: AsyncSession, device: DeviceContext, *, scan_id: str, ticket_code: str, now: datetime, cfg: GateSettings
) -> GateDecision:
    started = time.perf_counter()

    async def handle() -> GateDecision:
        def reject(
            reason: RejectReason, event_id: uuid.UUID | None = None, ticket_id: uuid.UUID | None = None
        ) -> Awaitable[GateDecision]:
            return _reject(
                session,
                reason,
                device=device,
                key=scan_id,
                started=started,
                now=now,
                event_id=event_id,
                ticket_id=ticket_id,
            )

        if device.direction is not GateDirection.ENTRY:
            return await reject(RejectReason.DEVICE_NOT_ALLOWED)
        row = await _lock_ticket_by_code(session, ticket_code)
        if row is None:
            live = await _live_event_for_venue(session, device.venue_id, now)
            log.info("gate.unknown_ticket", code=mask_ticket_code(ticket_code))
            return await reject(RejectReason.UNKNOWN_TICKET, live.id if live else None)
        ticket, event = row.ticket, row.event
        if event.venue_id != device.venue_id:
            return await reject(RejectReason.WRONG_EVENT, None, ticket.id)
        if not _gates_accept(event, ENTRY_ALLOWED_STATUSES, now, entry=True):
            return await reject(RejectReason.EVENT_NOT_LIVE, event.id, ticket.id)

        if ticket.type is TicketType.DRIVE_UP:
            # A printed drive-up ticket is never a way *in*: it was issued at the barrier. If its hold expired, the
            # maintenance job will VOID it and release the place (that needs the event_zone lock, which we must not
            # take after the ticket lock: lock order).
            stale = ticket.status is TicketStatus.ENTERING and _hold_expired(ticket, now)
            reason = RejectReason.TICKET_VOID if stale else entry_rejection(ticket.status)
            return await reject(reason or RejectReason.ALREADY_USED, event.id, ticket.id)

        if ticket.status is TicketStatus.ENTERING and _hold_expired(ticket, now):
            # Lazy expiry: the last entry attempt timed out (car never passed). Release it right now instead of
            # waiting up to a minute for the maintenance job, so this driver isn't blocked.
            await _expire_open_passages(session, ticket.id)
            ticket.status, ticket.hold_expires_at = TicketStatus.VALID, None
        if (reason := entry_rejection(ticket.status)) is not None:
            return await reject(reason, event.id, ticket.id)

        ticket.status = TicketStatus.ENTERING
        ticket.hold_expires_at = now + cfg.hold
        where = f"Zone {row.zone_code}" + (f", space {row.space_label}" if row.space_label else "")
        decision = GateDecision(
            decision=Decision.OPEN, zone=row.zone_code, space=row.space_label, display_text=f"Welcome! {where}"
        )
        return await _record(
            session,
            device=device,
            key=scan_id,
            decision=decision,
            status=PassageStatus.AUTHORIZED,
            started=started,
            now=now,
            event_id=event.id,
            ticket_id=ticket.id,
            deadline_at=ticket.hold_expires_at,
        )

    return await _idempotent(session, scan_id, device, handle)


# ---------------------------------------------------------------------------------------------------------------------
# Entry: drive-up ticket (button pressed, automatic zone allocation) - the race-critical one
# ---------------------------------------------------------------------------------------------------------------------


async def _allocate_atomic(session: AsyncSession, event_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """Try zones in priority order; take the first free place with ONE atomic statement per zone.

        UPDATE event_zones SET drive_up_occupied = drive_up_occupied + 1
         WHERE id = :zone AND drive_up_occupied < capacity - prebook_quota
        RETURNING id

    Postgres locks the row while updating and re-checks the WHERE condition against the newest committed value, so
    two lanes asking for the last place can't both succeed: one gets the row (1 row returned), the other waits, then
    sees the zone full (0 rows) and moves on. The CHECK constraint on the table is the safety net behind it.
    """
    zones = (
        await session.execute(
            select(EventZone.id, Zone.code)
            .join(Zone, Zone.id == EventZone.zone_id)
            .where(EventZone.event_id == event_id, EventZone.allocation_priority > 0)
            .order_by(EventZone.allocation_priority, Zone.code)
        )
    ).all()
    for zone_id, zone_code in zones:
        taken = await session.scalar(
            update(EventZone)
            .where(EventZone.id == zone_id, EventZone.drive_up_occupied < EventZone.capacity - EventZone.prebook_quota)
            .values(drive_up_occupied=EventZone.drive_up_occupied + 1)
            .returning(EventZone.id)
        )
        if taken is not None:
            return zone_id, zone_code
    return None


async def _allocate_naive(session: AsyncSession, event_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """⚠️ WRONG ON PURPOSE (Phase 3 experiment): read the counter, compare in Python, write the new value back.

    T1 reads occupied=9 (cap 10), T2 reads occupied=9, both decide "room left", both write 10: two cars got the last
    place, the counter says 10, and 11 tickets exist. The CHECK constraint can't catch it (10 <= 10 is legal).
    This is the classic *lost update*. Kept only so tests/experiments can show it failing. Never use it.
    """
    rows = (
        await session.execute(
            select(EventZone, Zone.code)
            .join(Zone, Zone.id == EventZone.zone_id)
            .where(EventZone.event_id == event_id, EventZone.allocation_priority > 0)
            .order_by(EventZone.allocation_priority, Zone.code)
        )
    ).all()
    for event_zone, zone_code in rows:
        if event_zone.drive_up_occupied < event_zone.capacity - event_zone.prebook_quota:
            new_value = event_zone.drive_up_occupied + 1  # computed in Python from a possibly stale read...
            await session.execute(
                update(EventZone).where(EventZone.id == event_zone.id).values(drive_up_occupied=new_value)
            )
            return event_zone.id, zone_code
    return None


ALLOCATORS: dict[AllocationStrategy, Callable[[AsyncSession, uuid.UUID], Awaitable[tuple[uuid.UUID, str] | None]]] = {
    "atomic": _allocate_atomic,
    "naive": _allocate_naive,
}


async def entry_issue(
    session: AsyncSession,
    device: DeviceContext,
    *,
    request_id: str,
    now: datetime,
    cfg: GateSettings,
    strategy: AllocationStrategy = "atomic",
) -> GateDecision:
    started = time.perf_counter()

    async def handle() -> GateDecision:
        def reject(reason: RejectReason, event_id: uuid.UUID | None = None) -> Awaitable[GateDecision]:
            return _reject(session, reason, device=device, key=request_id, started=started, now=now, event_id=event_id)

        if device.direction is not GateDirection.ENTRY or not device.has_printer:
            return await reject(RejectReason.DEVICE_NOT_ALLOWED)
        event = await _live_event_for_venue(session, device.venue_id, now)
        if event is None:
            return await reject(RejectReason.EVENT_NOT_LIVE)

        allocated = await ALLOCATORS[strategy](session, event.id)  # locks the event_zone row (lock order: first)
        if allocated is None:
            return await reject(RejectReason.CAR_PARK_FULL, event.id)
        event_zone_id, zone_code = allocated

        # A drive-up ticket is born ENTERING: printed and authorized in the same step (§5).
        ticket = Ticket(
            id=uuid7(),  # known now: the passage below refers to it before anything is flushed
            event_id=event.id,
            type=TicketType.DRIVE_UP,
            code=new_ticket_code(),
            event_zone_id=event_zone_id,
            status=TicketStatus.ENTERING,
            hold_expires_at=now + cfg.hold,
            issued_at=now,
            issued_by_device_id=device.device_id,
        )
        session.add(ticket)
        await session.flush()  # INSERT the ticket before the passage that references it (foreign key)
        decision = GateDecision(
            decision=Decision.OPEN,
            zone=zone_code,
            display_text=f"Please take your ticket - Zone {zone_code}",
            print=PrintPayload(ticket_code=ticket.code, zone=zone_code, entered_at=now),
        )
        return await _record(
            session,
            device=device,
            key=request_id,
            decision=decision,
            status=PassageStatus.AUTHORIZED,
            started=started,
            now=now,
            event_id=event.id,
            ticket_id=ticket.id,
            deadline_at=ticket.hold_expires_at,
        )

    return await _idempotent(session, request_id, device, handle)


# ---------------------------------------------------------------------------------------------------------------------
# Exit: scan (pre-booked -> OPEN; drive-up -> OPEN if nothing is due, else PAYMENT_REQUIRED)
# ---------------------------------------------------------------------------------------------------------------------


async def exit_scan(
    session: AsyncSession, device: DeviceContext, *, scan_id: str, ticket_code: str, now: datetime, cfg: GateSettings
) -> GateDecision:
    started = time.perf_counter()

    async def handle() -> GateDecision:
        def reject(
            reason: RejectReason, event_id: uuid.UUID | None = None, ticket_id: uuid.UUID | None = None
        ) -> Awaitable[GateDecision]:
            return _reject(
                session,
                reason,
                device=device,
                key=scan_id,
                started=started,
                now=now,
                event_id=event_id,
                ticket_id=ticket_id,
            )

        if device.direction is not GateDirection.EXIT:
            return await reject(RejectReason.DEVICE_NOT_ALLOWED)
        row = await _lock_ticket_by_code(session, ticket_code)
        if row is None:
            log.info("gate.unknown_ticket", code=mask_ticket_code(ticket_code))
            return await reject(RejectReason.UNKNOWN_TICKET)
        ticket, event = row.ticket, row.event
        if event.venue_id != device.venue_id:
            return await reject(RejectReason.WRONG_EVENT, None, ticket.id)
        if not _gates_accept(event, EXIT_ALLOWED_STATUSES, now, entry=False):
            return await reject(RejectReason.EVENT_NOT_LIVE, event.id, ticket.id)

        if ticket.status is TicketStatus.EXITING and _hold_expired(ticket, now):
            await _expire_open_passages(session, ticket.id)  # lazy expiry: last exit attempt timed out
            ticket.status, ticket.hold_expires_at = TicketStatus.INSIDE, None
        if (reason := exit_rejection(ticket.status)) is not None:
            return await reject(reason, event.id, ticket.id)

        # A driver re-scanning while a previous PAYMENT_REQUIRED is still open: that one is superseded.
        await _expire_open_passages(session, ticket.id)

        amount_due, currency = 0, "RSD"
        if ticket.type is TicketType.DRIVE_UP:
            pricing = await session.get(EventPricing, event.id)
            if pricing is not None and ticket.entered_at is not None:
                currency = pricing.currency
                rules = FeeRules(
                    free_minutes=pricing.drive_up_free_minutes,
                    hourly_minor=pricing.drive_up_hourly_minor,
                    daily_cap_minor=pricing.drive_up_daily_cap_minor,
                    exit_grace_minutes=pricing.post_payment_exit_grace_minutes,
                )
                amount_due = calculate_drive_up_fee(
                    entered_at=ticket.entered_at,
                    now=now,
                    rules=rules,
                    paid_minor=ticket.paid_minor,
                    paid_at=ticket.paid_at,
                )

        if amount_due > 0:
            # The ticket stays INSIDE; the passage waits for the card (POST /gate/v1/exit/pay, Phase 5).
            decision = GateDecision(
                decision=Decision.PAYMENT_REQUIRED,
                amount_minor=amount_due,
                currency=currency,
                zone=row.zone_code,
                display_text=f"Please pay {format_money(amount_due, currency)}",
            )
            return await _record(
                session,
                device=device,
                key=scan_id,
                decision=decision,
                status=PassageStatus.PENDING_PAYMENT,
                started=started,
                now=now,
                event_id=event.id,
                ticket_id=ticket.id,
                deadline_at=now + cfg.payment_window,
                amount_due_minor=amount_due,
            )

        ticket.status = TicketStatus.EXITING
        ticket.hold_expires_at = now + cfg.hold
        decision = GateDecision(decision=Decision.OPEN, zone=row.zone_code, display_text="Thank you, goodbye!")
        return await _record(
            session,
            device=device,
            key=scan_id,
            decision=decision,
            status=PassageStatus.AUTHORIZED,
            started=started,
            now=now,
            event_id=event.id,
            ticket_id=ticket.id,
            deadline_at=ticket.hold_expires_at,
            amount_due_minor=0,
        )

    return await _idempotent(session, scan_id, device, handle)


# ---------------------------------------------------------------------------------------------------------------------
# Second phase: the car passed the loop (complete) or reversed away (abort)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PassageResult:
    passage_id: uuid.UUID
    status: PassageStatus
    ticket_status: TicketStatus | None


async def _lock_passage_rows(
    session: AsyncSession, device: DeviceContext, passage_id: uuid.UUID
) -> tuple[Passage, Ticket, EventZone]:
    """Lock event_zone -> ticket -> passage, in that order (the deadlock-prevention rule), and return them."""
    peek = (
        await session.execute(
            select(Passage.device_id, Passage.ticket_id, Ticket.event_zone_id)
            .join(Ticket, Ticket.id == Passage.ticket_id)
            .where(Passage.id == passage_id)
        )
    ).first()
    if peek is None or peek.device_id != device.device_id:
        raise NotFound("No such passage at this gate.", code="PASSAGE_NOT_FOUND")
    event_zone = await session.scalar(select(EventZone).where(EventZone.id == peek.event_zone_id).with_for_update())
    ticket = await session.scalar(select(Ticket).where(Ticket.id == peek.ticket_id).with_for_update())
    passage = await session.scalar(select(Passage).where(Passage.id == passage_id).with_for_update())
    if event_zone is None or ticket is None or passage is None:  # pragma: no cover (FKs guarantee them)
        raise NotFound("No such passage at this gate.", code="PASSAGE_NOT_FOUND")
    return passage, ticket, event_zone


async def complete_passage(
    session: AsyncSession, device: DeviceContext, *, passage_id: uuid.UUID, now: datetime
) -> PassageResult:
    """The loop detector saw the car pass. Idempotent: completing a completed passage just answers again."""
    async with session.begin():
        passage, ticket, event_zone = await _lock_passage_rows(session, device, passage_id)
        if passage.status is PassageStatus.COMPLETED:
            return PassageResult(passage.id, passage.status, ticket.status)
        if passage.status is not PassageStatus.AUTHORIZED:
            # EXPIRED/ABORTED: the hold was already released (maybe the place was given to someone else). The device
            # shows "please call staff" and an operator sorts it out.
            raise Conflict(f"Passage is {passage.status.value}, it can't be completed.", code="PASSAGE_NOT_OPEN")
        # A completion arriving a few seconds after the deadline (slow network) is still accepted while the passage
        # is AUTHORIZED: the car really did pass, and the hold hasn't been released yet.
        passage.status, passage.completed_at = PassageStatus.COMPLETED, now
        ticket.hold_expires_at = None
        if passage.direction is GateDirection.ENTRY:
            ticket.status, ticket.entered_at = TicketStatus.INSIDE, now
            if ticket.type is TicketType.PREBOOKED:
                event_zone.prebook_inside += 1  # row is locked: a plain += is safe here (no concurrent writer)
            # drive-up: the place was taken at issue time (drive_up_occupied counts ENTERING + INSIDE)
        else:
            ticket.status, ticket.exited_at = TicketStatus.EXITED, now
            if ticket.type is TicketType.PREBOOKED:
                event_zone.prebook_inside -= 1  # prebook_held stays: single-entry pass keeps its reservation
            else:
                event_zone.drive_up_occupied -= 1  # the place is free again
        outbox.add(
            session,
            "passage.completed",
            {
                "passage_id": passage.id,
                "ticket_id": ticket.id,
                "ticket_type": ticket.type,
                "gate_id": passage.gate_id,
                "direction": passage.direction,
                "event_zone_id": event_zone.id,
                "completed_at": now,
                "entered_at": ticket.entered_at,
            },
            event_id=passage.event_id,
            direction=passage.direction.value,
        )
    log.info(
        "gate.passage_completed", passage_id=str(passage.id), direction=passage.direction.value, gate=device.gate_code
    )
    return PassageResult(passage.id, passage.status, ticket.status)


async def release_passage(
    session: AsyncSession, passage: Passage, ticket: Ticket, event_zone: EventZone, *, new_status: PassageStatus
) -> None:
    """Give back what an open passage holds. Shared by abort (device) and expiry (maintenance job, Phase 6).

    Caller must hold the locks (event_zone -> ticket -> passage) and have checked the passage is open.
    """
    was_authorized = passage.status is PassageStatus.AUTHORIZED
    passage.status = new_status
    if not was_authorized:
        return  # PENDING_PAYMENT holds nothing: the ticket is still INSIDE
    ticket.hold_expires_at = None
    if passage.direction is GateDirection.ENTRY:
        if ticket.type is TicketType.DRIVE_UP:
            ticket.status = TicketStatus.VOID  # never entered: the printed ticket is worthless...
            event_zone.drive_up_occupied -= 1  # ...and its place goes back to the pool
        else:
            ticket.status = TicketStatus.VALID  # the pass can be used again
    else:
        ticket.status = TicketStatus.INSIDE  # still in the car park


async def abort_passage(
    session: AsyncSession, device: DeviceContext, *, passage_id: uuid.UUID, now: datetime
) -> PassageResult:
    """The device reports the car reversed away. Idempotent like complete."""
    async with session.begin():
        passage, ticket, event_zone = await _lock_passage_rows(session, device, passage_id)
        if passage.status is PassageStatus.ABORTED:
            return PassageResult(passage.id, passage.status, ticket.status)
        if passage.status not in (PassageStatus.AUTHORIZED, PassageStatus.PENDING_PAYMENT):
            raise Conflict(f"Passage is {passage.status.value}, it can't be aborted.", code="PASSAGE_NOT_OPEN")
        await release_passage(session, passage, ticket, event_zone, new_status=PassageStatus.ABORTED)
        outbox.add(
            session,
            "passage.aborted",
            {
                "passage_id": passage.id,
                "ticket_id": ticket.id,
                "gate_id": passage.gate_id,
                "direction": passage.direction,
                "aborted_at": now,
            },
            event_id=passage.event_id,
            direction=passage.direction.value,
        )
    return PassageResult(passage.id, passage.status, ticket.status)


# ---------------------------------------------------------------------------------------------------------------------
# Heartbeat + commands
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingCommand:
    id: uuid.UUID
    command: str
    reason: str
    created_at: datetime


async def heartbeat(
    session: AsyncSession,
    device: DeviceContext,
    *,
    now: datetime,
    firmware_version: str | None,
    printer_status: PrinterStatus | None,
    ip: str | None,
) -> list[PendingCommand]:
    """'I'm alive' every 5 s. Marks the device ONLINE and hands over commands waiting for it (pull-based channel)."""
    async with session.begin():
        row = await session.get(Device, device.device_id, with_for_update=True)
        if row is None:  # pragma: no cover (authenticated a moment ago)
            raise Unauthorized("Unknown device.", code="INVALID_DEVICE_KEY")
        previous_printer = row.printer_status
        row.status = DeviceStatus.ONLINE
        row.last_heartbeat_at = now
        row.last_seen_ip = ip
        row.firmware_version = firmware_version or row.firmware_version
        row.printer_status = printer_status
        if (
            printer_status in (PrinterStatus.PAPER_LOW, PrinterStatus.PAPER_OUT)
            and printer_status is not previous_printer
        ):
            event = await _live_event_for_venue(session, device.venue_id, now)
            outbox.add(
                session,
                "alert.printer",
                {
                    "device_id": device.device_id,
                    "gate_id": device.gate_id,
                    "gate_code": device.gate_code,
                    "printer_status": printer_status,
                },
                event_id=event.id if event else None,
            )
        commands = (
            await session.scalars(
                select(DeviceCommand)
                .where(DeviceCommand.device_id == device.device_id, DeviceCommand.acked_at.is_(None))
                .order_by(DeviceCommand.created_at)
            )
        ).all()
        for command in commands:
            command.delivered_at = command.delivered_at or now
        result = [PendingCommand(c.id, c.command.value, c.reason, c.created_at) for c in commands]
    return result


async def ack_command(session: AsyncSession, device: DeviceContext, *, command_id: uuid.UUID, now: datetime) -> None:
    async with session.begin():
        command = await session.get(DeviceCommand, command_id, with_for_update=True)
        if command is None or command.device_id != device.device_id:
            raise NotFound("No such command for this device.", code="COMMAND_NOT_FOUND")
        command.acked_at = command.acked_at or now  # idempotent
