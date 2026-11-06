"""Operator actions (docs/01-product-and-domain.md §6 F) and the dashboard's read queries (§10). ADR-028.

Writes (ADMIN, GATE_OPERATOR), each ONE transaction with an audit row and outbox messages:
  - manual_open: OPEN_BARRIER command for the gate's device + a `manual` COMPLETED passage. At an exit lane with a
    ticket given, the car is let out: ticket EXITED, its place released, any unpaid fee recorded as "released unpaid".
  - void_ticket: ticket VOID, whatever capacity it held is released.
  - lost_ticket: the driver at an exit lane has no ticket. The operator collects the flat lost-ticket fee at the
    booth (recorded as a SUCCEEDED LOST_TICKET payment), the ticket (if found by plate/code) becomes VOID, and the
    barrier opens.
Every write takes a client `request_id` (idempotency, like the gate API): a double-clicked button or a retried request
returns the first result instead of opening the barrier twice.

Lock order is the gate hot path's: event_zone -> ticket -> passage (services/gate.py), so operators and lanes can't
deadlock each other.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound, ValidationFailed
from eventpark.db.models import (
    Booking,
    Device,
    DeviceCommand,
    Event,
    EventPricing,
    EventZone,
    Gate,
    HourlyStat,
    Passage,
    Payment,
    Space,
    Ticket,
    Vehicle,
    Zone,
)
from eventpark.db.pagination import decode_cursor, encode_cursor
from eventpark.domain.events import EventStatus
from eventpark.domain.payments import BookingStatus, PaymentKind, PaymentStatus, normalize_plate
from eventpark.domain.pricing import FeeRules, calculate_drive_up_fee
from eventpark.domain.tickets import FINAL_TICKET_STATUSES, PassageStatus, TicketStatus, TicketType
from eventpark.domain.venues import DeviceCommandType, DeviceStatus, GateDirection
from eventpark.messaging import outbox
from eventpark.services import audit

ONLINE_WITHIN = timedelta(seconds=60)
ACTIVITY_WINDOW = timedelta(minutes=15)


# ---------------------------------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorResult:
    passage_id: uuid.UUID
    command_id: uuid.UUID | None
    ticket_id: uuid.UUID | None
    ticket_status: TicketStatus | None
    amount_due_minor: int
    amount_paid_minor: int
    currency: str
    replayed: bool = False


def _op_key(request_id: str) -> str:
    return f"op:{request_id}"


async def _replay(session: AsyncSession, request_id: str) -> OperatorResult | None:
    passage = await session.scalar(select(Passage).where(Passage.idempotency_key == _op_key(request_id)))
    if passage is None:
        return None
    r = passage.response
    return OperatorResult(
        passage_id=passage.id,
        command_id=uuid.UUID(r["command_id"]) if r.get("command_id") else None,
        ticket_id=passage.ticket_id,
        ticket_status=TicketStatus(r["ticket_status"]) if r.get("ticket_status") else None,
        amount_due_minor=int(r.get("amount_due_minor", 0)),
        amount_paid_minor=int(r.get("amount_paid_minor", 0)),
        currency=r.get("currency", "RSD"),
        replayed=True,
    )


async def _gate_with_device(session: AsyncSession, gate_id: uuid.UUID) -> tuple[Gate, Device]:
    row = (
        await session.execute(select(Gate, Device).join(Device, Device.gate_id == Gate.id).where(Gate.id == gate_id))
    ).first()
    if row is None:
        raise NotFound("No such gate, or it has no device to send the command to.", code="GATE_NOT_FOUND")
    return row[0], row[1]


async def _current_event(session: AsyncSession, venue_id: uuid.UUID) -> Event | None:
    """The venue's LIVE event, else the most recent ENDED one (exits are still allowed after the end)."""
    live = await session.scalar(select(Event).where(Event.venue_id == venue_id, Event.status == EventStatus.LIVE))
    if live is not None:
        return live
    return await session.scalar(
        select(Event)
        .where(Event.venue_id == venue_id, Event.status == EventStatus.ENDED)
        .order_by(Event.gates_close_at.desc())
        .limit(1)
    )


async def _find_ticket_id(
    session: AsyncSession, *, event_id: uuid.UUID, ticket_code: str | None, plate: str | None
) -> uuid.UUID | None:
    if ticket_code:
        return await session.scalar(select(Ticket.id).where(Ticket.code == ticket_code, Ticket.event_id == event_id))
    if plate:
        normalized = normalize_plate(plate)
        return await session.scalar(
            select(Ticket.id)
            .join(Vehicle, Vehicle.id == Ticket.vehicle_id)
            .where(
                Ticket.event_id == event_id,
                Vehicle.plate_normalized == normalized,
                Ticket.status.in_([TicketStatus.INSIDE, TicketStatus.EXITING]),
            )
            .limit(1)
        )
    return None


async def _lock_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> tuple[Ticket, EventZone]:
    """event_zone first, then the ticket (the hot path's lock order)."""
    event_zone_id = await session.scalar(select(Ticket.event_zone_id).where(Ticket.id == ticket_id))
    if event_zone_id is None:
        raise NotFound("No such ticket.", code="TICKET_NOT_FOUND")
    event_zone = await session.scalar(select(EventZone).where(EventZone.id == event_zone_id).with_for_update())
    ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if event_zone is None or ticket is None:  # pragma: no cover (FK)
        raise NotFound("No such ticket.", code="TICKET_NOT_FOUND")
    return ticket, event_zone


async def _close_open_passages(session: AsyncSession, ticket_id: uuid.UUID) -> int:
    """Expire the ticket's open passages (a pending payment at the lane, an unfinished entry). Returns the amount
    that was due on a pending exit payment, if any."""
    open_passages = (
        await session.scalars(
            select(Passage)
            .where(
                Passage.ticket_id == ticket_id,
                Passage.status.in_([PassageStatus.AUTHORIZED, PassageStatus.PENDING_PAYMENT]),
            )
            .with_for_update()
        )
    ).all()
    due = 0
    for passage in open_passages:
        if passage.status is PassageStatus.PENDING_PAYMENT:
            due = max(due, passage.amount_due_minor or 0)
        passage.status = PassageStatus.EXPIRED
    return due


def _release_capacity(ticket: Ticket, event_zone: EventZone) -> None:
    """Give back what this (non-final) ticket holds. Mirrors the counters the hot path maintains:
    drive-up: drive_up_occupied counts ENTERING + INSIDE + EXITING; pre-booked: prebook_held counts every non-final
    pass, prebook_inside the ones inside."""
    if ticket.type is TicketType.DRIVE_UP:
        if ticket.status in (TicketStatus.ENTERING, TicketStatus.INSIDE, TicketStatus.EXITING):
            event_zone.drive_up_occupied -= 1
        return
    if ticket.status in (TicketStatus.INSIDE, TicketStatus.EXITING):
        event_zone.prebook_inside -= 1
    event_zone.prebook_held -= 1  # prebook_held counts ACTIVE bookings: the caller must cancel the booking too


def _exit_capacity(ticket: Ticket, event_zone: EventZone) -> None:
    """The car left (like complete_passage on an exit): drive-up frees its place; a pre-booked pass leaves
    `prebook_inside` but keeps its booking hold (single-entry pass)."""
    if ticket.type is TicketType.DRIVE_UP:
        event_zone.drive_up_occupied -= 1
    else:
        event_zone.prebook_inside -= 1


async def _cancel_booking(session: AsyncSession, ticket: Ticket) -> None:
    """A voided pre-booked pass ends its booking, keeping `prebook_held == active bookings` true."""
    if ticket.booking_id is not None:
        await session.execute(
            update(Booking).where(Booking.id == ticket.booking_id).values(status=BookingStatus.CANCELLED)
        )


async def _fee_due(session: AsyncSession, ticket: Ticket, now: datetime) -> tuple[int, str]:
    pricing = await session.get(EventPricing, ticket.event_id)
    currency = pricing.currency if pricing else "RSD"
    if pricing is None or ticket.type is not TicketType.DRIVE_UP or ticket.entered_at is None:
        return 0, currency
    rules = FeeRules(
        free_minutes=pricing.drive_up_free_minutes,
        hourly_minor=pricing.drive_up_hourly_minor,
        daily_cap_minor=pricing.drive_up_daily_cap_minor,
        exit_grace_minutes=pricing.post_payment_exit_grace_minutes,
    )
    fee = calculate_drive_up_fee(
        entered_at=ticket.entered_at, now=now, rules=rules, paid_minor=ticket.paid_minor, paid_at=ticket.paid_at
    )
    return fee, currency


def _command(session: AsyncSession, device: Device, *, actor: uuid.UUID, reason: str) -> DeviceCommand:
    command = DeviceCommand(
        device_id=device.id, command=DeviceCommandType.OPEN_BARRIER, requested_by=actor, reason=reason
    )
    session.add(command)
    return command


def _manual_passage(
    session: AsyncSession,
    *,
    request_id: str,
    gate: Gate,
    device: Device,
    event_id: uuid.UUID | None,
    ticket: Ticket | None,
    actor: uuid.UUID,
    now: datetime,
    amount_due: int,
    response: dict[str, Any],
) -> Passage:
    passage = Passage(
        event_id=event_id,
        gate_id=gate.id,
        device_id=device.id,
        ticket_id=ticket.id if ticket else None,
        direction=gate.direction,
        status=PassageStatus.COMPLETED,
        amount_due_minor=amount_due,
        idempotency_key=_op_key(request_id),
        manual=True,
        operator_user_id=actor,
        authorized_at=now,
        completed_at=now,
        response=response,
    )
    session.add(passage)
    return passage


def _completed_message(
    session: AsyncSession, passage: Passage, ticket: Ticket | None, event_zone_id: uuid.UUID | None
) -> None:
    outbox.add(
        session,
        "passage.completed",
        {
            "passage_id": passage.id,
            "ticket_id": ticket.id if ticket else None,
            "ticket_type": ticket.type if ticket else None,
            "gate_id": passage.gate_id,
            "direction": passage.direction,
            "event_zone_id": event_zone_id,
            "completed_at": passage.completed_at,
            "entered_at": ticket.entered_at if ticket else None,
            "manual": True,
        },
        event_id=passage.event_id,
        direction=passage.direction.value,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------------------------------


async def manual_open(
    session: AsyncSession,
    *,
    gate_id: uuid.UUID,
    actor: uuid.UUID,
    reason: str,
    request_id: str,
    ticket_code: str | None,
    now: datetime,
) -> OperatorResult:
    async with session.begin():
        if (stored := await _replay(session, request_id)) is not None:
            return stored
        gate, device = await _gate_with_device(session, gate_id)
        if device.status is DeviceStatus.DISABLED:
            raise Conflict("This gate's device is disabled.", code="DEVICE_DISABLED")
        event = await _current_event(session, gate.venue_id)
        ticket: Ticket | None = None
        event_zone: EventZone | None = None
        amount_due, currency = 0, "RSD"
        if ticket_code:
            if gate.direction is not GateDirection.EXIT:
                raise ValidationFailed("A ticket can only be released at an exit gate.", code="NOT_AN_EXIT_GATE")
            if event is None:
                raise Conflict("No live or ended event at this venue.", code="NO_CURRENT_EVENT")
            ticket_id = await _find_ticket_id(session, event_id=event.id, ticket_code=ticket_code, plate=None)
            if ticket_id is None:
                raise NotFound("No such ticket for the current event.", code="TICKET_NOT_FOUND")
            ticket, event_zone = await _lock_ticket(session, ticket_id)
            if ticket.status not in (TicketStatus.INSIDE, TicketStatus.EXITING):
                raise Conflict(f"Ticket is {ticket.status.value}, not inside.", code="TICKET_NOT_INSIDE")
            pending_due = await _close_open_passages(session, ticket.id)
            fee, currency = await _fee_due(session, ticket, now)
            amount_due = max(pending_due, fee)
            _exit_capacity(ticket, event_zone)
            ticket.status, ticket.exited_at, ticket.hold_expires_at = TicketStatus.EXITED, now, None

        command = _command(session, device, actor=actor, reason=reason)
        await session.flush()
        response = {
            "command_id": str(command.id),
            "ticket_status": ticket.status.value if ticket else None,
            "amount_due_minor": amount_due,
            "amount_paid_minor": 0,
            "currency": currency,
        }
        passage = _manual_passage(
            session,
            request_id=request_id,
            gate=gate,
            device=device,
            event_id=event.id if event else None,
            ticket=ticket,
            actor=actor,
            now=now,
            amount_due=amount_due,
            response=response,
        )
        await session.flush()
        if passage.event_id is not None:
            _completed_message(session, passage, ticket, event_zone.id if event_zone else None)
            if amount_due > 0:
                outbox.add(
                    session,
                    "passage.released_unpaid",
                    {
                        "passage_id": passage.id,
                        "ticket_id": ticket.id if ticket else None,
                        "gate_id": gate.id,
                        "amount_due_minor": amount_due,
                        "currency": currency,
                    },
                    event_id=passage.event_id,
                )
        audit.record(
            session,
            action="gate.manual_open",
            target_type="gate",
            target_id=gate.id,
            actor_user_id=actor,
            reason=reason,
            details={
                "command_id": str(command.id),
                "passage_id": str(passage.id),
                "ticket_id": str(ticket.id) if ticket else None,
                "released_unpaid_minor": amount_due,
            },
        )
        return OperatorResult(
            passage.id,
            command.id,
            ticket.id if ticket else None,
            ticket.status if ticket else None,
            amount_due,
            0,
            currency,
        )


async def void_ticket(
    session: AsyncSession, *, ticket_id: uuid.UUID, actor: uuid.UUID, reason: str, now: datetime
) -> tuple[Ticket, uuid.UUID]:
    """Returns the voided ticket and its event id. Voiding a VOID ticket again is a no-op (idempotent)."""
    async with session.begin():
        ticket, event_zone = await _lock_ticket(session, ticket_id)
        if ticket.status is TicketStatus.VOID:
            return ticket, ticket.event_id
        if ticket.status in FINAL_TICKET_STATUSES:
            raise Conflict(f"Ticket is {ticket.status.value} and can't be voided.", code="TICKET_FINAL")
        await _close_open_passages(session, ticket.id)
        previous = ticket.status
        _release_capacity(ticket, event_zone)
        await _cancel_booking(session, ticket)
        ticket.status, ticket.hold_expires_at = TicketStatus.VOID, None
        audit.record(
            session,
            action="ticket.voided",
            target_type="ticket",
            target_id=ticket.id,
            actor_user_id=actor,
            reason=reason,
            details={"previous_status": previous.value},
        )
    return ticket, ticket.event_id


async def lost_ticket(
    session: AsyncSession,
    *,
    gate_id: uuid.UUID,
    actor: uuid.UUID,
    request_id: str,
    ticket_code: str | None,
    plate: str | None,
    now: datetime,
) -> OperatorResult:
    async with session.begin():
        if (stored := await _replay(session, request_id)) is not None:
            return stored
        gate, device = await _gate_with_device(session, gate_id)
        if gate.direction is not GateDirection.EXIT:
            raise ValidationFailed("Lost tickets are handled at an exit gate.", code="NOT_AN_EXIT_GATE")
        event = await _current_event(session, gate.venue_id)
        if event is None:
            raise Conflict("No live or ended event at this venue.", code="NO_CURRENT_EVENT")
        pricing = await session.get(EventPricing, event.id)
        if pricing is None or pricing.lost_ticket_fee_minor <= 0:
            raise Conflict("This event has no lost-ticket fee configured.", code="NO_LOST_TICKET_FEE")
        fee, currency = pricing.lost_ticket_fee_minor, pricing.currency

        ticket: Ticket | None = None
        event_zone: EventZone | None = None
        ticket_id = await _find_ticket_id(session, event_id=event.id, ticket_code=ticket_code, plate=plate)
        if ticket_id is not None:
            ticket, event_zone = await _lock_ticket(session, ticket_id)
            if ticket.status not in (TicketStatus.INSIDE, TicketStatus.EXITING):
                raise Conflict(f"Ticket is {ticket.status.value}, not inside.", code="TICKET_NOT_INSIDE")
            await _close_open_passages(session, ticket.id)
            _release_capacity(ticket, event_zone)
            await _cancel_booking(session, ticket)
            ticket.status, ticket.exited_at, ticket.hold_expires_at = TicketStatus.VOID, now, None
            ticket.paid_minor += fee
            ticket.paid_at = now

        command = _command(session, device, actor=actor, reason="lost ticket")
        await session.flush()
        response = {
            "command_id": str(command.id),
            "ticket_status": ticket.status.value if ticket else None,
            "amount_due_minor": fee,
            "amount_paid_minor": fee,
            "currency": currency,
        }
        passage = _manual_passage(
            session,
            request_id=request_id,
            gate=gate,
            device=device,
            event_id=event.id,
            ticket=ticket,
            actor=actor,
            now=now,
            amount_due=fee,
            response=response,
        )
        await session.flush()
        payment = Payment(
            kind=PaymentKind.LOST_TICKET,
            ticket_id=ticket.id if ticket else None,
            passage_id=passage.id,
            amount_minor=fee,
            currency=currency,
            status=PaymentStatus.SUCCEEDED,
            idempotency_key=f"lost:{request_id}",
        )
        session.add(payment)
        await session.flush()
        _completed_message(session, passage, ticket, event_zone.id if event_zone else None)
        outbox.add(
            session,
            "payment.succeeded",
            {
                "payment_id": payment.id,
                "kind": payment.kind,
                "amount_minor": fee,
                "currency": currency,
                "ticket_id": ticket.id if ticket else None,
                "passage_id": passage.id,
                "gate_id": gate.id,
            },
            event_id=event.id,
        )
        audit.record(
            session,
            action="ticket.lost",
            target_type="gate",
            target_id=gate.id,
            actor_user_id=actor,
            reason="lost ticket",
            details={
                "passage_id": str(passage.id),
                "ticket_id": str(ticket.id) if ticket else None,
                "fee_minor": fee,
                "found_by": "code" if ticket_code else "plate" if plate else None,
            },
        )
        return OperatorResult(
            passage.id, command.id, ticket.id if ticket else None, ticket.status if ticket else None, fee, fee, currency
        )


# ---------------------------------------------------------------------------------------------------------------------
# Reads (dashboard)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InsideRow:
    ticket_id: uuid.UUID
    code: str
    type: TicketType
    status: TicketStatus
    zone: str
    space: str | None
    plate: str | None
    entered_at: datetime | None


async def cars_inside(
    session: AsyncSession, event_id: uuid.UUID, *, zone: str | None, cursor: str | None, limit: int
) -> tuple[list[InsideRow], str | None]:
    query = (
        select(
            Ticket.id,
            Ticket.code,
            Ticket.type,
            Ticket.status,
            Zone.code,
            Space.label,
            Vehicle.plate_display,
            Ticket.entered_at,
        )
        .join(EventZone, EventZone.id == Ticket.event_zone_id)
        .join(Zone, Zone.id == EventZone.zone_id)
        .outerjoin(Space, Space.id == Ticket.space_id)
        .outerjoin(Vehicle, Vehicle.id == Ticket.vehicle_id)
        .where(Ticket.event_id == event_id, Ticket.status.in_([TicketStatus.INSIDE, TicketStatus.EXITING]))
    )
    if zone:
        query = query.where(Zone.code == zone)
    rows, next_cursor = await _keyset(session, query, Ticket.id, cursor=cursor, limit=limit, descending=False)
    return [InsideRow(*r) for r in rows], next_cursor


@dataclass(frozen=True)
class PassageRow:
    id: uuid.UUID
    gate_code: str
    direction: GateDirection
    status: PassageStatus
    reject_reason: str | None
    manual: bool
    ticket_type: TicketType | None
    zone: str | None
    amount_due_minor: int | None
    decision_ms: int | None
    authorized_at: datetime
    completed_at: datetime | None


async def passages(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    gate_id: uuid.UUID | None,
    status: PassageStatus | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[PassageRow], str | None]:
    """Newest first (live feed). UUIDv7 ids are time-ordered, so `id DESC` = newest first, and it's the cursor too."""
    query = (
        select(
            Passage.id,
            Gate.code,
            Passage.direction,
            Passage.status,
            Passage.reject_reason,
            Passage.manual,
            Ticket.type,
            Zone.code,
            Passage.amount_due_minor,
            Passage.decision_ms,
            Passage.authorized_at,
            Passage.completed_at,
        )
        .join(Gate, Gate.id == Passage.gate_id)
        .outerjoin(Ticket, Ticket.id == Passage.ticket_id)
        .outerjoin(EventZone, EventZone.id == Ticket.event_zone_id)
        .outerjoin(Zone, Zone.id == EventZone.zone_id)
        .where(Passage.event_id == event_id)
    )
    if gate_id:
        query = query.where(Passage.gate_id == gate_id)
    if status:
        query = query.where(Passage.status == status)
    rows, next_cursor = await _keyset(session, query, Passage.id, cursor=cursor, limit=limit, descending=True)
    return [PassageRow(r[0], r[1], r[2], r[3], r[4].value if r[4] else None, *r[5:]) for r in rows], next_cursor


async def _keyset(
    session: AsyncSession, query: Any, id_column: Any, *, cursor: str | None, limit: int, descending: bool
) -> tuple[list[Any], str | None]:
    """Keyset pagination over row tuples (db/pagination.py works on ORM entities); first column must be the id."""
    limit = max(1, min(limit, 200))
    if cursor:
        last = decode_cursor(cursor)
        query = query.where(id_column < last if descending else id_column > last)
    order = id_column.desc() if descending else id_column
    rows = list((await session.execute(query.order_by(order).limit(limit + 1))).all())
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1][0])
    return rows, None


@dataclass(frozen=True)
class HourRow:
    hour_start: datetime
    entries: int
    exits: int
    rejections: int
    revenue_minor: int
    released_unpaid_minor: int
    avg_decision_ms: float | None


async def hourly_stats(session: AsyncSession, event_id: uuid.UUID) -> list[HourRow]:
    """Totals over all gates per venue-local hour (hour_start is stored as UTC; the client formats it)."""
    s = HourlyStat
    rows = (
        await session.execute(
            select(
                s.hour_start,
                func.sum(s.entries),
                func.sum(s.exits),
                func.sum(s.rejections),
                func.sum(s.revenue_minor),
                func.sum(s.released_unpaid_minor),
                func.sum(s.decision_ms_sum),
                func.sum(s.decision_count),
            )
            .where(s.event_id == event_id)
            .group_by(s.hour_start)
            .order_by(s.hour_start)
        )
    ).all()
    return [
        HourRow(h, int(e), int(x), int(r), int(rev), int(unpaid), (int(ms) / int(n)) if n else None)
        for h, e, x, r, rev, unpaid, ms, n in rows
    ]


@dataclass(frozen=True)
class GateActivity:
    gate_id: uuid.UUID
    code: str
    name: str
    direction: GateDirection
    device_status: DeviceStatus | None
    online: bool
    last_heartbeat_at: datetime | None
    printer_status: str | None
    passages_15m: int
    rejections_15m: int
    rejections_by_reason: dict[str, int]
    avg_decision_ms_15m: float | None


async def gate_activity(session: AsyncSession, event_id: uuid.UUID, now: datetime) -> list[GateActivity]:
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFound("No such event.", code="EVENT_NOT_FOUND")
    since = now - ACTIVITY_WINDOW
    gates = (
        await session.execute(
            select(Gate, Device)
            .outerjoin(Device, Device.gate_id == Gate.id)
            .where(Gate.venue_id == event.venue_id)
            .order_by(Gate.direction, Gate.code)
        )
    ).all()
    recent = and_(Passage.event_id == event_id, Passage.authorized_at >= since)
    counts = {
        gate_id: (int(done), int(rejected), (int(ms or 0) / int(n)) if n else None)
        for gate_id, done, rejected, ms, n in (
            await session.execute(
                select(
                    Passage.gate_id,
                    func.count(case((Passage.status == PassageStatus.COMPLETED, 1))),
                    func.count(case((Passage.status == PassageStatus.REJECTED, 1))),
                    func.coalesce(func.sum(Passage.decision_ms), 0),
                    func.count(Passage.decision_ms),
                )
                .where(recent)
                .group_by(Passage.gate_id)
            )
        ).all()
    }
    reasons: dict[uuid.UUID, dict[str, int]] = {}
    for gate_id, reason, n in (
        await session.execute(
            select(Passage.gate_id, Passage.reject_reason, func.count())
            .where(recent, Passage.status == PassageStatus.REJECTED)
            .group_by(Passage.gate_id, Passage.reject_reason)
        )
    ).all():
        if reason is not None:
            reasons.setdefault(gate_id, {})[reason.value] = int(n)
    result = []
    for gate, device in gates:
        done, rejected, avg = counts.get(gate.id, (0, 0, None))
        last = device.last_heartbeat_at if device else None
        result.append(
            GateActivity(
                gate_id=gate.id,
                code=gate.code,
                name=gate.name,
                direction=gate.direction,
                device_status=device.status if device else None,
                online=bool(
                    device and device.status is not DeviceStatus.DISABLED and last and now - last <= ONLINE_WITHIN
                ),
                last_heartbeat_at=last,
                printer_status=device.printer_status.value if device and device.printer_status else None,
                passages_15m=done,
                rejections_15m=rejected,
                rejections_by_reason=reasons.get(gate.id, {}),
                avg_decision_ms_15m=avg,
            )
        )
    return result


@dataclass(frozen=True)
class LookupPassage:
    id: uuid.UUID
    gate_code: str
    direction: GateDirection
    status: PassageStatus
    manual: bool
    authorized_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class LookupResult:
    ticket_id: uuid.UUID
    code: str
    event_id: uuid.UUID
    event_name: str
    type: TicketType
    status: TicketStatus
    zone: str
    space: str | None
    plate: str | None
    entered_at: datetime | None
    exited_at: datetime | None
    paid_minor: int
    passages: list[LookupPassage]


async def lookup(session: AsyncSession, *, code: str | None, plate: str | None, limit: int = 20) -> list[LookupResult]:
    if not code and not plate:
        raise ValidationFailed("Give a ticket code or a plate.", code="LOOKUP_NEEDS_QUERY")
    query = (
        select(Ticket, Event.name, Zone.code, Space.label, Vehicle.plate_display)
        .join(Event, Event.id == Ticket.event_id)
        .join(EventZone, EventZone.id == Ticket.event_zone_id)
        .join(Zone, Zone.id == EventZone.zone_id)
        .outerjoin(Space, Space.id == Ticket.space_id)
        .outerjoin(Vehicle, Vehicle.id == Ticket.vehicle_id)
    )
    conditions = []
    if code:
        conditions.append(Ticket.code == code.strip())
    if plate:
        conditions.append(Vehicle.plate_normalized == normalize_plate(plate))
    rows = (await session.execute(query.where(or_(*conditions)).order_by(Ticket.id.desc()).limit(limit))).all()
    ids = [r[0].id for r in rows]
    history: dict[uuid.UUID, list[LookupPassage]] = {}
    if ids:
        for p, gate_code in (
            await session.execute(
                select(Passage, Gate.code)
                .join(Gate, Gate.id == Passage.gate_id)
                .where(Passage.ticket_id.in_(ids))
                .order_by(Passage.id)
            )
        ).all():
            if p.ticket_id is None:  # pragma: no cover (filtered by the IN above)
                continue
            history.setdefault(p.ticket_id, []).append(
                LookupPassage(p.id, gate_code, p.direction, p.status, p.manual, p.authorized_at, p.completed_at)
            )
    return [
        LookupResult(
            ticket_id=t.id,
            code=t.code,
            event_id=t.event_id,
            event_name=event_name,
            type=t.type,
            status=t.status,
            zone=zone,
            space=space,
            plate=plate_display,
            entered_at=t.entered_at,
            exited_at=t.exited_at,
            paid_minor=t.paid_minor,
            passages=history.get(t.id, []),
        )
        for t, event_name, zone, space, plate_display in rows
    ]


async def event_of_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> uuid.UUID | None:
    event_id = await session.scalar(select(Ticket.event_id).where(Ticket.id == ticket_id))
    await session.commit()
    return event_id
