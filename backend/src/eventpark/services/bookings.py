"""Online booking (docs/01-product-and-domain.md §9, docs/02-architecture.md §3.4).

    POST /public/v1/bookings
      tx:   lock event_zone row; release expired holds; take a place (prebook_held + 1); insert booking
            PENDING_PAYMENT (expires in 15 min; a VIP space is protected by a partial UNIQUE index); payment PENDING
            COMMIT                                   <- the hold is committed BEFORE talking to the provider
      call: fakepay create payment intent (Idempotency-Key = booking id) -> checkout_url
    driver pays on fakepay's page -> fakepay POSTs a signed webhook -> services/webhooks.py confirms the booking.

The browser coming back to our site ("payment=succeeded" in the URL) proves nothing: anyone can type that URL.
Only the signed webhook (or asking the provider ourselves) confirms a payment.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.config import Settings
from eventpark.core.errors import Conflict, NotFound, ValidationFailed
from eventpark.db.errors import constraint_name
from eventpark.db.models import Booking, Event, EventZone, Payment, Space, Ticket, Vehicle, Zone
from eventpark.domain.events import EventStatus
from eventpark.domain.payments import BookingStatus, PaymentKind, PaymentStatus, normalize_plate
from eventpark.services.provider import PaymentProvider, ProviderUnavailable

log = structlog.get_logger()

BOOKABLE_STATUSES = (EventStatus.PUBLISHED, EventStatus.LIVE)


@dataclass(frozen=True)
class ZoneAvailability:
    event_zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    price_minor: int
    prebook_quota: int
    available: int
    has_spaces: bool


@dataclass(frozen=True)
class BookingView:
    booking: Booking
    zone_code: str
    space_label: str | None
    plate: str | None
    ticket_code: str | None
    checkout_url: str | None


def is_bookable(event: Event, now: datetime) -> bool:
    return event.status in BOOKABLE_STATUSES and event.booking_opens_at <= now < event.booking_closes_at


async def list_events(session: AsyncSession, now: datetime) -> Sequence[Event]:
    return (
        await session.scalars(
            select(Event).where(Event.status.in_(BOOKABLE_STATUSES), Event.ends_at > now).order_by(Event.starts_at)
        )
    ).all()


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> Event:
    event = await session.get(Event, event_id)
    if event is None or event.status not in BOOKABLE_STATUSES:
        raise NotFound("Event not found.", code="EVENT_NOT_FOUND")  # drafts are invisible to drivers
    return event


async def availability(session: AsyncSession, event_id: uuid.UUID) -> list[ZoneAvailability]:
    """From Postgres for now; Phase 7 serves this from the Redis read model (it's read far more than written)."""
    has_spaces = exists().where(Space.zone_id == Zone.id)
    rows = (
        await session.execute(
            select(EventZone, Zone, has_spaces)
            .join(Zone, Zone.id == EventZone.zone_id)
            .where(EventZone.event_id == event_id, EventZone.prebook_quota > 0)
            .order_by(Zone.code)
        )
    ).all()
    return [
        ZoneAvailability(
            ez.id,
            z.code,
            z.name,
            ez.prebook_price_minor,
            ez.prebook_quota,
            ez.prebook_quota - ez.prebook_held,
            bool(spaces),
        )
        for ez, z, spaces in rows
    ]


def _active_booking_for_space(event_id: uuid.UUID, now: datetime):  # type: ignore[no-untyped-def]
    return exists().where(
        Booking.event_id == event_id,
        Booking.space_id == Space.id,
        (Booking.status == BookingStatus.CONFIRMED)
        | ((Booking.status == BookingStatus.PENDING_PAYMENT) & (Booking.expires_at > now)),
    )


async def free_spaces(
    session: AsyncSession, event_id: uuid.UUID, event_zone_id: uuid.UUID, now: datetime
) -> Sequence[Space]:
    zone_id = await session.scalar(
        select(EventZone.zone_id).where(EventZone.id == event_zone_id, EventZone.event_id == event_id)
    )
    if zone_id is None:
        raise NotFound("Zone not found for this event.", code="ZONE_NOT_FOUND")
    return (
        await session.scalars(
            select(Space)
            .where(Space.zone_id == zone_id, ~_active_booking_for_space(event_id, now))
            .order_by(Space.label)
        )
    ).all()


async def release_expired_holds(session: AsyncSession, event_zone: EventZone, now: datetime) -> list[uuid.UUID]:
    """Expire unpaid bookings of this zone whose 15 minutes are up, and give their places back.

    Caller holds the event_zone row lock, so the plain `-=` below is safe. Called lazily before taking a place (so a
    driver never sees "sold out" because of a stale hold) and by the maintenance job every minute (Phase 6).
    """
    expired = (
        await session.scalars(
            update(Booking)
            .where(
                Booking.event_zone_id == event_zone.id,
                Booking.status == BookingStatus.PENDING_PAYMENT,
                Booking.expires_at <= now,
            )
            .values(status=BookingStatus.EXPIRED)
            .returning(Booking.id)
        )
    ).all()
    if expired:
        event_zone.prebook_held -= len(expired)
        await session.execute(
            update(Payment)
            .where(Payment.booking_id.in_(expired), Payment.status == PaymentStatus.PENDING)
            .values(status=PaymentStatus.FAILED, failure_reason="booking_expired")
        )
    return list(expired)


async def _vehicle_id(session: AsyncSession, plate: str, user_id: uuid.UUID) -> uuid.UUID | None:
    normalized = normalize_plate(plate)
    if not normalized:
        return None
    statement = (
        insert(Vehicle)
        .values(id=uuid.uuid4(), plate_normalized=normalized, plate_display=plate.strip(), owner_user_id=user_id)
        .on_conflict_do_update(index_elements=[Vehicle.plate_normalized], set_={"plate_display": plate.strip()})
        .returning(Vehicle.id)
    )
    return await session.scalar(statement)


async def create_booking(
    session: AsyncSession,
    provider: PaymentProvider,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    event_id: uuid.UUID,
    event_zone_id: uuid.UUID,
    space_id: uuid.UUID | None,
    plate: str,
    idempotency_key: str | None,
    now: datetime,
    test_outcome: str | None = None,
) -> BookingView:
    if idempotency_key:
        existing = await session.scalar(select(Booking).where(Booking.idempotency_key == idempotency_key))
        await session.commit()
        if existing is not None:
            if existing.user_id != user_id:
                raise Conflict("This idempotency key was used by someone else.", code="IDEMPOTENCY_KEY_REUSED")
            return await get_booking(session, settings, user_id, existing.id)

    async with session.begin():
        event = await session.get(Event, event_id)
        if event is None or not is_bookable(event, now):
            raise Conflict("Booking is not open for this event.", code="BOOKING_CLOSED")
        # Lock order: event_zone first. FOR UPDATE makes this "read, check, write" safe: concurrent bookings of the
        # same zone queue up on this row, so each one sees the counter its predecessor wrote.
        event_zone = await session.scalar(
            select(EventZone).where(EventZone.id == event_zone_id, EventZone.event_id == event_id).with_for_update()
        )
        if event_zone is None:
            raise NotFound("Zone not found for this event.", code="ZONE_NOT_FOUND")
        await release_expired_holds(session, event_zone, now)
        if event_zone.prebook_held >= event_zone.prebook_quota:
            raise Conflict("Sold out in this zone.", code="SOLD_OUT")

        zone_has_spaces = await session.scalar(select(exists().where(Space.zone_id == event_zone.zone_id)))
        if zone_has_spaces and space_id is None:
            raise ValidationFailed("Choose a space in this zone.", code="SPACE_REQUIRED")
        if space_id is not None:
            space_zone = await session.scalar(select(Space.zone_id).where(Space.id == space_id))
            if space_zone != event_zone.zone_id:
                raise ValidationFailed("That space is not in this zone.", code="SPACE_NOT_IN_ZONE")

        event_zone.prebook_held += 1
        booking = Booking(
            user_id=user_id,
            event_id=event_id,
            event_zone_id=event_zone_id,
            space_id=space_id,
            vehicle_id=await _vehicle_id(session, plate, user_id),
            status=BookingStatus.PENDING_PAYMENT,
            price_minor=event_zone.prebook_price_minor,
            currency="RSD",
            expires_at=now + timedelta(minutes=settings.booking_hold_minutes),
            idempotency_key=idempotency_key,
        )
        session.add(booking)
        try:
            await session.flush()
        except IntegrityError as exc:
            name = constraint_name(exc)
            if name == "uq_bookings_active_space":
                raise Conflict("Someone just booked that space. Please pick another.", code="SPACE_TAKEN") from exc
            if name == "uq_bookings_idempotency_key":
                raise Conflict("Duplicate request, please refresh.", code="DUPLICATE_REQUEST") from exc
            raise
        payment = Payment(
            kind=PaymentKind.BOOKING,
            booking_id=booking.id,
            amount_minor=booking.price_minor,
            currency=booking.currency,
            status=PaymentStatus.PENDING,
            idempotency_key=f"booking:{booking.id}",
        )
        session.add(payment)
    log.info("booking.held", booking_id=str(booking.id), event_id=str(event_id), event_zone_id=str(event_zone_id))

    await start_checkout(session, provider, settings, booking_id=booking.id, test_outcome=test_outcome)
    return await get_booking(session, settings, user_id, booking.id)


async def start_checkout(
    session: AsyncSession,
    provider: PaymentProvider,
    settings: Settings,
    *,
    booking_id: uuid.UUID,
    test_outcome: str | None = None,
) -> str | None:
    """Ask the provider for a checkout page (idempotent: calling it again returns the same intent)."""
    payment = await session.scalar(
        select(Payment).where(Payment.booking_id == booking_id, Payment.kind == PaymentKind.BOOKING)
    )
    await session.commit()
    if payment is None:
        raise NotFound("Booking not found.", code="BOOKING_NOT_FOUND")
    metadata = {"booking_id": str(booking_id)}
    if test_outcome in ("succeed", "fail"):  # simulator/tests: let fakepay settle the intent by itself
        metadata["auto"] = test_outcome
    try:
        intent = await provider.create_intent(
            idempotency_key=payment.idempotency_key,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            description="EventPark parking pass",
            return_url=f"{settings.public_web_url}/book/bookings/{booking_id}",
            webhook_url=f"{settings.webhook_base_url}/webhooks/fakepay",
            metadata=metadata,
        )
    except ProviderUnavailable:
        log.warning("booking.provider_unavailable", booking_id=str(booking_id))
        return None  # the hold stays; the driver can retry checkout (POST .../checkout) within 15 minutes
    async with session.begin():
        await session.execute(update(Payment).where(Payment.id == payment.id).values(provider_ref=intent.intent_id))
    return intent.checkout_url


async def get_booking(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, booking_id: uuid.UUID
) -> BookingView:
    row = (
        await session.execute(
            select(Booking, Zone.code, Space.label, Vehicle.plate_display, Ticket.code, Payment.provider_ref)
            .join(EventZone, EventZone.id == Booking.event_zone_id)
            .join(Zone, Zone.id == EventZone.zone_id)
            .outerjoin(Space, Space.id == Booking.space_id)
            .outerjoin(Vehicle, Vehicle.id == Booking.vehicle_id)
            .outerjoin(Ticket, Ticket.booking_id == Booking.id)
            .outerjoin(Payment, (Payment.booking_id == Booking.id) & (Payment.kind == PaymentKind.BOOKING))
            .where(Booking.id == booking_id)
        )
    ).first()
    await session.commit()
    # Ownership check in the service, not just the role (docs/02 §9): another driver's booking is "not found",
    # not "forbidden", so its existence isn't even revealed.
    if row is None or row[0].user_id != user_id:
        raise NotFound("Booking not found.", code="BOOKING_NOT_FOUND")
    booking, zone_code, space_label, plate, ticket_code, intent_id = row
    checkout = None
    if booking.status is BookingStatus.PENDING_PAYMENT and intent_id:
        checkout = f"{settings.fakepay_public_url.rstrip('/')}/checkout/{intent_id}"
    return BookingView(booking, zone_code, space_label, plate, ticket_code, checkout)


async def list_bookings(session: AsyncSession, settings: Settings, user_id: uuid.UUID) -> list[BookingView]:
    ids = (
        await session.scalars(select(Booking.id).where(Booking.user_id == user_id).order_by(Booking.id.desc()))
    ).all()
    await session.commit()
    return [await get_booking(session, settings, user_id, i) for i in ids]
