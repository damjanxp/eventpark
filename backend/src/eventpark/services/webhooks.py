"""Handling the payment provider's webhooks (docs/02-architecture.md §3.4).

Providers deliver webhooks AT LEAST once: they retry until we answer 2xx, and sometimes send duplicates. So:
1. verify the signature first (anyone on the internet can POST to this URL);
2. INSERT the provider's event id into webhook_events in the SAME transaction as the effects: a duplicate hits the
   primary key and does nothing, and two copies arriving at the same instant are serialised by the key (the second
   waits for the first to commit, then finds the row);
3. answer 2xx for anything we accepted, even "nothing to do", so the provider stops retrying.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Unauthorized, ValidationFailed
from eventpark.core.webhooks import InvalidSignature, verify_signature
from eventpark.db.models import Booking, EventZone, Payment, Ticket, WebhookEvent
from eventpark.domain.payments import (
    BookingStatus,
    PaymentKind,
    PaymentOutcome,
    PaymentStatus,
    PaymentTransitionError,
    payment_transition,
)
from eventpark.domain.tickets import TicketStatus, TicketType, new_ticket_code
from eventpark.messaging import outbox

log = structlog.get_logger()

WebhookResult = Literal["processed", "duplicate", "ignored"]
_OUTCOMES = {"payment_intent.succeeded": PaymentOutcome.SUCCEEDED, "payment_intent.failed": PaymentOutcome.FAILED}


def parse_and_verify(body: bytes, signature: str | None, *, secret: str, now: int) -> dict[str, Any]:
    try:
        verify_signature(signature, body, secret=secret, now=now)
    except InvalidSignature as exc:
        log.warning("webhook.bad_signature", reason=str(exc))
        raise Unauthorized("Invalid webhook signature.", code="INVALID_SIGNATURE") from exc
    try:
        event: dict[str, Any] = json.loads(body)
        _ = event["id"], event["type"], event["data"]["object"]["id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed("Malformed webhook payload.", code="INVALID_PAYLOAD") from exc
    return event


async def handle_payment_event(session: AsyncSession, event: dict[str, Any], *, now: datetime) -> WebhookResult:
    outcome = _OUTCOMES.get(event["type"])
    intent = event["data"]["object"]
    async with session.begin():
        stored = await session.scalar(
            insert(WebhookEvent)
            .values(provider_event_id=event["id"], payload=event)
            .on_conflict_do_nothing(index_elements=[WebhookEvent.provider_event_id])
            .returning(WebhookEvent.provider_event_id)
        )
        if stored is None:
            log.info("webhook.duplicate", provider_event_id=event["id"])
            return "duplicate"
        if outcome is None:
            return "ignored"  # an event type we don't use: recorded, acknowledged

        payment_peek = (
            await session.execute(
                select(Payment.id, Payment.booking_id).where(
                    Payment.provider_ref == intent["id"], Payment.kind == PaymentKind.BOOKING
                )
            )
        ).first()
        if payment_peek is None or payment_peek.booking_id is None:
            log.warning("webhook.unknown_intent", intent_id=intent["id"])
            return "ignored"
        booking_peek = await session.get(Booking, payment_peek.booking_id)
        if booking_peek is None:  # pragma: no cover (FK)
            return "ignored"

        # Lock order: event_zone -> booking -> payment (event_zone first, like every other use case).
        event_zone = await session.scalar(
            select(EventZone).where(EventZone.id == booking_peek.event_zone_id).with_for_update()
        )
        booking = await session.scalar(select(Booking).where(Booking.id == booking_peek.id).with_for_update())
        payment = await session.scalar(select(Payment).where(Payment.id == payment_peek.id).with_for_update())
        if event_zone is None or booking is None or payment is None:  # pragma: no cover (FKs)
            return "ignored"

        target = payment_transition(payment.status, outcome)
        if isinstance(target, PaymentTransitionError):
            log.error("webhook.contradiction", payment_id=str(payment.id), detail=target.message)
            return "ignored"
        payment.status = target
        if target is PaymentStatus.FAILED:
            payment.failure_reason = "declined_at_checkout"
            if booking.status is BookingStatus.PENDING_PAYMENT:
                booking.status = BookingStatus.CANCELLED
                event_zone.prebook_held -= 1  # give the place back right away
            return "processed"

        # SUCCEEDED
        if booking.status is BookingStatus.EXPIRED:
            # Paid after the 15-minute hold ran out: the place was released. Take it again if it's still free.
            if event_zone.prebook_held < event_zone.prebook_quota and not await _space_taken(session, booking):
                event_zone.prebook_held += 1
                booking.status = BookingStatus.PENDING_PAYMENT
            else:
                booking.status = BookingStatus.CANCELLED
                log.error("webhook.paid_but_sold_out", booking_id=str(booking.id), payment_id=str(payment.id))
                outbox.add(
                    session,
                    "booking.expired",
                    {"booking_id": booking.id, "refund_required": True},
                    event_id=booking.event_id,
                )
                return "processed"
        if booking.status is BookingStatus.PENDING_PAYMENT:
            await _confirm(session, booking, now)
    return "processed"


async def _space_taken(session: AsyncSession, booking: Booking) -> bool:
    if booking.space_id is None:
        return False
    other = await session.scalar(
        select(Booking.id).where(
            Booking.event_id == booking.event_id,
            Booking.space_id == booking.space_id,
            Booking.id != booking.id,
            Booking.status.in_([BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED]),
        )
    )
    return other is not None


async def _confirm(session: AsyncSession, booking: Booking, now: datetime) -> None:
    booking.status = BookingStatus.CONFIRMED
    ticket = Ticket(
        id=uuid.uuid4(),
        event_id=booking.event_id,
        type=TicketType.PREBOOKED,
        code=new_ticket_code(),
        booking_id=booking.id,  # UNIQUE: one booking can never produce two tickets
        event_zone_id=booking.event_zone_id,
        space_id=booking.space_id,
        vehicle_id=booking.vehicle_id,
        status=TicketStatus.VALID,
        issued_at=now,
    )
    session.add(ticket)
    await session.flush()
    outbox.add(
        session,
        "booking.confirmed",
        {
            "booking_id": booking.id,
            "ticket_id": ticket.id,
            "user_id": booking.user_id,
            "event_zone_id": booking.event_zone_id,
        },
        event_id=booking.event_id,
    )
    log.info("booking.confirmed", booking_id=str(booking.id), ticket_id=str(ticket.id))
