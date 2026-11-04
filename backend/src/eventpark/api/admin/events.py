"""Admin: events, pricing, per-zone capacity, lifecycle. Writes: ADMIN/EVENT_MANAGER. Reads: any staff role."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from eventpark.api.deps import EventManagerDep, RedisDep, SessionDep, StaffDep, utcnow
from eventpark.api.schemas import Page
from eventpark.cache import event_config, read_model
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
    event_id: uuid.UUID, body: EventPatch, principal: EventManagerDep, session: SessionDep, redis: RedisDep
) -> EventDetailOut:
    await events.update_event(
        session,
        actor=principal.user_id,
        event_id=event_id,
        name=body.name,
        times=body.model_dump(exclude={"name"}),
    )
    await event_config.invalidate(redis, event_id)  # after the commit: the next reader loads the new version
    return _detail_out(await events.get_event_detail(session, event_id))


@router.put("/{event_id}/pricing")
async def set_pricing(
    event_id: uuid.UUID, body: PricingIn, principal: EventManagerDep, session: SessionDep, redis: RedisDep
) -> PricingOut:
    pricing = await events.set_pricing(session, actor=principal.user_id, event_id=event_id, values=body.model_dump())
    await event_config.invalidate(redis, event_id)
    return PricingOut.model_validate(pricing)


@router.put("/{event_id}/zones")
async def set_zones(
    event_id: uuid.UUID, body: list[EventZoneIn], principal: EventManagerDep, session: SessionDep, redis: RedisDep
) -> list[EventZoneOut]:
    rows = await events.set_zones(
        session,
        actor=principal.user_id,
        event_id=event_id,
        configs=[EventZoneConfig(**z.model_dump()) for z in body],
    )
    await event_config.invalidate(redis, event_id)
    async with session.begin():
        await read_model.refresh(redis, session, event_id)  # capacities changed: the read model must follow
    return _zones_out(rows)


def _register_action(action: EventAction) -> None:
    """One explicit route per lifecycle action (POST /events/{id}/publish, .../go-live, ...).

    Explicit routes instead of a catch-all `/{event_id}/{action}`: a catch-all would also swallow other routes like
    POST /events/{id}/reports and answer them with a confusing 422.
    """

    async def endpoint(
        event_id: uuid.UUID, principal: EventManagerDep, session: SessionDep, redis: RedisDep
    ) -> EventDetailOut:
        await events.apply_action(session, actor=principal.user_id, event_id=event_id, action=action, now=utcnow())
        await event_config.invalidate(redis, event_id)
        return _detail_out(await events.get_event_detail(session, event_id))

    endpoint.__name__ = f"event_{action.value.replace('-', '_')}"
    router.add_api_route(
        f"/{{event_id}}/{action.value}",
        endpoint,
        methods=["POST"],
        summary=f"Lifecycle: {action.value}",
        description="See docs/01-product-and-domain.md §4 for the allowed transitions.",
    )


for _action in EventAction:
    _register_action(_action)


class OccupancyOut(BaseModel):
    event_id: uuid.UUID
    source: str = Field(description="redis (read model) or postgres (fallback / rebuild)")
    zones: dict[str, dict[str, int]]
    totals: dict[str, int]


@router.get("/{event_id}/occupancy")
async def occupancy(event_id: uuid.UUID, _: StaffDep, session: SessionDep, redis: RedisDep) -> OccupancyOut:
    """Live occupancy per zone from the Redis read model (Postgres if Redis is unavailable)."""
    zones, source = await read_model.read(redis, session, event_id)
    keys = (
        "capacity",
        "inside",
        "entering",
        "drive_up_occupied",
        "prebook_held",
        "available_drive_up",
        "available_prebook",
    )
    totals = {k: sum(int(z.get(k, 0)) for z in zones.values()) for k in keys}
    return OccupancyOut(event_id=event_id, source=source, zones=zones, totals=totals)
