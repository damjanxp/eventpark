"""Public booking API (/public/v1): events and availability for anyone, bookings for signed-in drivers."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

from eventpark.api.deps import DriverDep, LimiterDep, RedisDep, SessionDep, SettingsDep, enforce, utcnow
from eventpark.cache import event_config, read_model
from eventpark.cache.rate_limit import BOOKING_PER_USER
from eventpark.core.errors import NotFound
from eventpark.db.models import Event
from eventpark.domain.events import EventStatus
from eventpark.domain.payments import BookingStatus
from eventpark.services import bookings
from eventpark.services.bookings import BookingView
from eventpark.services.provider import PaymentProvider

router = APIRouter(prefix="/public/v1", tags=["public: booking"])


def get_provider(request: Request) -> PaymentProvider:
    provider: PaymentProvider = request.app.state.provider
    return provider


ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]


class PublicEvent(BaseModel):
    id: uuid.UUID
    name: str
    status: EventStatus
    starts_at: datetime
    ends_at: datetime
    gates_open_at: datetime
    booking_opens_at: datetime
    booking_closes_at: datetime
    bookable_now: bool


class ZoneOut(BaseModel):
    event_zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    price_minor: int
    currency: str = "RSD"
    available: int
    has_spaces: bool


class PublicEventDetail(PublicEvent):
    zones: list[ZoneOut]


class SpaceOut(BaseModel):
    id: uuid.UUID
    label: str
    kind: str


class BookingIn(BaseModel):
    event_id: uuid.UUID
    event_zone_id: uuid.UUID
    space_id: uuid.UUID | None = None
    plate: str = Field(default="", max_length=20, description="Licence plate (optional)")
    # Simulator/tests only: let fakepay settle the payment by itself ("succeed" | "fail").
    test_outcome: str | None = Field(default=None, pattern=r"^(succeed|fail)$")


class BookingOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    status: BookingStatus
    zone_code: str
    space_label: str | None
    plate: str | None
    price_minor: int
    currency: str
    expires_at: datetime
    created_at: datetime
    checkout_url: str | None = Field(description="Send the driver here to pay (while PENDING_PAYMENT)")
    ticket_code: str | None = Field(description="The QR code content, once CONFIRMED")


def _event_out(event: Event, now: datetime) -> PublicEvent:
    return PublicEvent(
        id=event.id,
        name=event.name,
        status=event.status,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        gates_open_at=event.gates_open_at,
        booking_opens_at=event.booking_opens_at,
        booking_closes_at=event.booking_closes_at,
        bookable_now=bookings.is_bookable(event, now),
    )


def _booking_out(view: BookingView) -> BookingOut:
    b = view.booking
    return BookingOut(
        id=b.id,
        event_id=b.event_id,
        status=b.status,
        zone_code=view.zone_code,
        space_label=view.space_label,
        plate=view.plate,
        price_minor=b.price_minor,
        currency=b.currency,
        expires_at=b.expires_at,
        created_at=b.created_at,
        checkout_url=view.checkout_url,
        ticket_code=view.ticket_code,
    )


@router.get("/events")
async def list_events(session: SessionDep) -> list[PublicEvent]:
    now = utcnow()
    return [_event_out(e, now) for e in await bookings.list_events(session, now)]


@router.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID, session: SessionDep, redis: RedisDep) -> PublicEventDetail:
    """Event + zones from the config cache, live availability from the occupancy read model (both Redis, with a
    Postgres fallback): this page is hit by every driver browsing, so it must not cost a database round trip each."""
    now = utcnow()
    config = await event_config.get_event_config(redis, session, event_id)
    if config is None or config["status"] not in ("PUBLISHED", "LIVE"):
        raise NotFound("Event not found.", code="EVENT_NOT_FOUND")
    occupancy, _source = await read_model.read(redis, session, event_id)
    zones = [
        ZoneOut(
            event_zone_id=z["event_zone_id"],
            zone_code=z["zone_code"],
            zone_name=z["zone_name"],
            price_minor=z["prebook_price_minor"],
            available=occupancy.get(z["zone_code"], {}).get("available_prebook", z["prebook_quota"]),
            has_spaces=z["has_spaces"],
        )
        for z in config["zones"]
        if z["prebook_quota"] > 0
    ]
    event = PublicEvent(
        id=config["id"],
        name=config["name"],
        status=config["status"],
        starts_at=config["starts_at"],
        ends_at=config["ends_at"],
        gates_open_at=config["gates_open_at"],
        booking_opens_at=config["booking_opens_at"],
        booking_closes_at=config["booking_closes_at"],
        bookable_now=datetime.fromisoformat(config["booking_opens_at"])
        <= now
        < datetime.fromisoformat(config["booking_closes_at"]),
    )
    return PublicEventDetail(**event.model_dump(), zones=zones)


@router.get("/events/{event_id}/zones/{event_zone_id}/spaces")
async def free_spaces(event_id: uuid.UUID, event_zone_id: uuid.UUID, session: SessionDep) -> list[SpaceOut]:
    await bookings.get_event(session, event_id)
    spaces = await bookings.free_spaces(session, event_id, event_zone_id, utcnow())
    return [SpaceOut(id=s.id, label=s.label, kind=s.kind.value) for s in spaces]


@router.post("/bookings", status_code=status.HTTP_201_CREATED)
async def create_booking(
    body: BookingIn,
    principal: DriverDep,
    session: SessionDep,
    provider: ProviderDep,
    settings: SettingsDep,
    limiter: LimiterDep,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> BookingOut:
    """Hold a place (and VIP space) for 15 minutes and start checkout. Pay at `checkout_url`."""
    await enforce(limiter, BOOKING_PER_USER, str(principal.user_id))
    view = await bookings.create_booking(
        session,
        provider,
        settings,
        user_id=principal.user_id,
        event_id=body.event_id,
        event_zone_id=body.event_zone_id,
        space_id=body.space_id,
        plate=body.plate,
        idempotency_key=idempotency_key,
        now=utcnow(),
        test_outcome=body.test_outcome,
    )
    return _booking_out(view)


@router.post("/bookings/{booking_id}/checkout")
async def retry_checkout(
    booking_id: uuid.UUID, principal: DriverDep, session: SessionDep, provider: ProviderDep, settings: SettingsDep
) -> BookingOut:
    """If the provider was down when booking: ask for the checkout page again (same intent, idempotent)."""
    await bookings.get_booking(session, settings, principal.user_id, booking_id)  # ownership check
    await bookings.start_checkout(session, provider, settings, booking_id=booking_id)
    return _booking_out(await bookings.get_booking(session, settings, principal.user_id, booking_id))


@router.get("/bookings")
async def my_bookings(principal: DriverDep, session: SessionDep, settings: SettingsDep) -> list[BookingOut]:
    return [_booking_out(v) for v in await bookings.list_bookings(session, settings, principal.user_id)]


@router.get("/bookings/{booking_id}")
async def get_booking(
    booking_id: uuid.UUID, principal: DriverDep, session: SessionDep, settings: SettingsDep
) -> BookingOut:
    return _booking_out(await bookings.get_booking(session, settings, principal.user_id, booking_id))
