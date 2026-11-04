"""Admin: events, pricing, per-zone capacity, lifecycle. Writes: ADMIN/EVENT_MANAGER. Reads: any staff role."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from eventpark.api.deps import EventManagerDep, SessionDep, StaffDep, utcnow
from eventpark.api.schemas import Page
from eventpark.db.models import EventZone, Zone
from eventpark.domain.events import EventAction, EventStatus
from eventpark.services import events
from eventpark.services.events import EventDetail, EventZoneConfig

router = APIRouter(prefix="/admin/v1/events", tags=["admin: events"])


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EventTimes(BaseModel):
    # AwareDatetime: a time without a zone offset ("20:00" - in which time zone?) is rejected with a 422.
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    gates_open_at: AwareDatetime
    gates_close_at: AwareDatetime
    booking_opens_at: AwareDatetime
    booking_closes_at: AwareDatetime


class EventCreate(EventTimes):
    venue_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class EventPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    gates_open_at: AwareDatetime | None = None
    gates_close_at: AwareDatetime | None = None
    booking_opens_at: AwareDatetime | None = None
    booking_closes_at: AwareDatetime | None = None


class PricingIn(BaseModel):
    currency: str = Field(default="RSD", pattern=r"^[A-Z]{3}$")
    drive_up_free_minutes: int = Field(ge=0, le=24 * 60, examples=[15])
    drive_up_hourly_minor: int = Field(ge=0, examples=[30000], description="per started hour, minor units (para)")
    drive_up_daily_cap_minor: int = Field(ge=0, examples=[150000])
    lost_ticket_fee_minor: int = Field(ge=0, examples=[200000])
    post_payment_exit_grace_minutes: int = Field(ge=0, le=24 * 60, examples=[15])


class PricingOut(Orm, PricingIn):
    pass


class EventZoneIn(BaseModel):
    zone_id: uuid.UUID
    capacity: int = Field(ge=0)
    prebook_quota: int = Field(ge=0)
    prebook_price_minor: int = Field(ge=0)
    allocation_priority: int = Field(ge=0, description="lower = filled first by drive-up; 0 = no drive-up in this zone")


class EventZoneOut(BaseModel):
    id: uuid.UUID
    zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    capacity: int
    prebook_quota: int
    drive_up_capacity: int
    prebook_price_minor: int
    allocation_priority: int
    prebook_held: int
    prebook_inside: int
    drive_up_occupied: int


class EventOut(Orm):
    id: uuid.UUID
    venue_id: uuid.UUID
    name: str
    status: EventStatus
    starts_at: datetime
    ends_at: datetime
    gates_open_at: datetime
    gates_close_at: datetime
    booking_opens_at: datetime
    booking_closes_at: datetime
    created_at: datetime


class EventDetailOut(EventOut):
    pricing: PricingOut | None
    zones: list[EventZoneOut]


def _zones_out(rows: Sequence[tuple[EventZone, Zone]]) -> list[EventZoneOut]:
    return [
        EventZoneOut(
            id=ez.id,
            zone_id=z.id,
            zone_code=z.code,
            zone_name=z.name,
            capacity=ez.capacity,
            prebook_quota=ez.prebook_quota,
            drive_up_capacity=ez.capacity - ez.prebook_quota,
            prebook_price_minor=ez.prebook_price_minor,
            allocation_priority=ez.allocation_priority,
            prebook_held=ez.prebook_held,
            prebook_inside=ez.prebook_inside,
            drive_up_occupied=ez.drive_up_occupied,
        )
        for ez, z in rows
    ]


def _detail_out(detail: EventDetail) -> EventDetailOut:
    return EventDetailOut(
        **EventOut.model_validate(detail.event).model_dump(),
        pricing=PricingOut.model_validate(detail.pricing) if detail.pricing else None,
        zones=_zones_out(detail.zones),
    )


@router.get("")
async def list_events(
    _: StaffDep,
    session: SessionDep,
    status_filter: Annotated[EventStatus | None, Query(alias="status")] = None,
    venue_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[EventOut]:
    items, next_cursor = await events.list_events(
        session, status=status_filter, venue_id=venue_id, cursor=cursor, limit=limit
    )
    return Page(items=[EventOut.model_validate(e) for e in items], next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(body: EventCreate, principal: EventManagerDep, session: SessionDep) -> EventDetailOut:
    times = body.model_dump(include=set(EventTimes.model_fields))
    event = await events.create_event(
        session, actor=principal.user_id, venue_id=body.venue_id, name=body.name, times=times
    )
    return _detail_out(EventDetail(event=event, pricing=None, zones=[]))


@router.get("/{event_id}")
async def get_event(event_id: uuid.UUID, _: StaffDep, session: SessionDep) -> EventDetailOut:
    return _detail_out(await events.get_event_detail(session, event_id))


@router.patch("/{event_id}")
async def update_event(
    event_id: uuid.UUID, body: EventPatch, principal: EventManagerDep, session: SessionDep
) -> EventDetailOut:
    await events.update_event(
        session,
        actor=principal.user_id,
        event_id=event_id,
        name=body.name,
        times=body.model_dump(exclude={"name"}),
    )
    return _detail_out(await events.get_event_detail(session, event_id))


@router.put("/{event_id}/pricing")
async def set_pricing(
    event_id: uuid.UUID, body: PricingIn, principal: EventManagerDep, session: SessionDep
) -> PricingOut:
    pricing = await events.set_pricing(session, actor=principal.user_id, event_id=event_id, values=body.model_dump())
    return PricingOut.model_validate(pricing)


@router.put("/{event_id}/zones")
async def set_zones(
    event_id: uuid.UUID, body: list[EventZoneIn], principal: EventManagerDep, session: SessionDep
) -> list[EventZoneOut]:
    rows = await events.set_zones(
        session,
        actor=principal.user_id,
        event_id=event_id,
        configs=[EventZoneConfig(**z.model_dump()) for z in body],
    )
    return _zones_out(rows)


@router.post("/{event_id}/{action}")
async def apply_action(
    event_id: uuid.UUID, action: EventAction, principal: EventManagerDep, session: SessionDep
) -> EventDetailOut:
    """Lifecycle: publish | go-live | end | archive | cancel (docs/01-product-and-domain.md §4)."""
    await events.apply_action(session, actor=principal.user_id, event_id=event_id, action=action, now=utcnow())
    return _detail_out(await events.get_event_detail(session, event_id))
