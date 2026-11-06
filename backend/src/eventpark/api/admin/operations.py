"""Admin: live operations (dashboard reads + operator actions). docs/02-architecture.md §8 "Admin - operations".

Reads: any staff role. Writes (manual open, void, lost ticket): ADMIN and GATE_OPERATOR, always with a reason, always
audited. After a write commits we refresh the occupancy read model and push it to dashboards (best effort: Redis down
-> skipped; the maintenance job rebuilds the read model within a minute).
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.api.deps import OperatorDep, RedisDep, SessionDep, StaffDep, utcnow
from eventpark.api.schemas import Page
from eventpark.cache import pubsub, read_model
from eventpark.domain.tickets import PassageStatus, TicketStatus, TicketType
from eventpark.domain.venues import DeviceStatus, GateDirection
from eventpark.services import operations as ops

log = structlog.get_logger()
router = APIRouter(prefix="/admin/v1", tags=["admin: operations"])

RequestId = Field(min_length=8, max_length=64, description="client-generated (UUID); a retry returns the first result")
Reason = Field(min_length=3, max_length=500)


# --- reads -----------------------------------------------------------------------------------------------------------


class InsideOut(BaseModel):
    ticket_id: uuid.UUID
    code: str
    type: TicketType
    status: TicketStatus
    zone: str
    space: str | None
    plate: str | None
    entered_at: datetime | None


@router.get("/events/{event_id}/inside")
async def cars_inside(
    event_id: uuid.UUID,
    _: StaffDep,
    session: SessionDep,
    zone: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[InsideOut]:
    rows, next_cursor = await ops.cars_inside(session, event_id, zone=zone, cursor=cursor, limit=limit)
    return Page(items=[InsideOut(**r.__dict__) for r in rows], next_cursor=next_cursor)


class PassageRowOut(BaseModel):
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


@router.get("/events/{event_id}/passages")
async def list_passages(
    event_id: uuid.UUID,
    _: StaffDep,
    session: SessionDep,
    gate_id: uuid.UUID | None = None,
    status: PassageStatus | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[PassageRowOut]:
    """Newest first."""
    rows, next_cursor = await ops.passages(
        session, event_id, gate_id=gate_id, status=status, cursor=cursor, limit=limit
    )
    return Page(items=[PassageRowOut(**r.__dict__) for r in rows], next_cursor=next_cursor)


class HourOut(BaseModel):
    hour_start: datetime
    entries: int
    exits: int
    rejections: int
    revenue_minor: int
    released_unpaid_minor: int
    avg_decision_ms: float | None


class HourlyStatsOut(BaseModel):
    hours: list[HourOut]
    total_entries: int
    total_exits: int
    total_rejections: int
    total_revenue_minor: int
    total_released_unpaid_minor: int


@router.get("/events/{event_id}/stats/hourly")
async def hourly_stats(event_id: uuid.UUID, _: StaffDep, session: SessionDep) -> HourlyStatsOut:
    hours = [HourOut(**h.__dict__) for h in await ops.hourly_stats(session, event_id)]
    return HourlyStatsOut(
        hours=hours,
        total_entries=sum(h.entries for h in hours),
        total_exits=sum(h.exits for h in hours),
        total_rejections=sum(h.rejections for h in hours),
        total_revenue_minor=sum(h.revenue_minor for h in hours),
        total_released_unpaid_minor=sum(h.released_unpaid_minor for h in hours),
    )


class GateActivityOut(BaseModel):
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


@router.get("/events/{event_id}/gates/activity")
async def gate_activity(event_id: uuid.UUID, _: StaffDep, session: SessionDep) -> list[GateActivityOut]:
    return [GateActivityOut(**g.__dict__) for g in await ops.gate_activity(session, event_id, utcnow())]


class LookupPassageOut(BaseModel):
    id: uuid.UUID
    gate_code: str
    direction: GateDirection
    status: PassageStatus
    manual: bool
    authorized_at: datetime
    completed_at: datetime | None


class LookupOut(BaseModel):
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
    passages: list[LookupPassageOut]


@router.get("/tickets/lookup")
async def lookup_tickets(
    _: StaffDep,
    session: SessionDep,
    code: Annotated[str | None, Query(max_length=64)] = None,
    plate: Annotated[str | None, Query(max_length=32)] = None,
) -> list[LookupOut]:
    results = await ops.lookup(session, code=code, plate=plate)
    return [
        LookupOut(**{**r.__dict__, "passages": [LookupPassageOut(**p.__dict__) for p in r.passages]}) for r in results
    ]


# --- writes ----------------------------------------------------------------------------------------------------------


class OperatorResultOut(BaseModel):
    passage_id: uuid.UUID
    command_id: uuid.UUID | None
    ticket_id: uuid.UUID | None
    ticket_status: TicketStatus | None
    amount_due_minor: int
    amount_paid_minor: int
    currency: str
    replayed: bool


async def _push_occupancy(redis: Redis, session: AsyncSession, event_id: uuid.UUID | None) -> None:
    """After commit: absolute values from Postgres -> Redis -> dashboards. Fails soft."""
    if event_id is None:
        return
    try:
        zones = await read_model.refresh(redis, session, event_id)
        await pubsub.publish(redis, event_id, {"type": "occupancy", "event_id": str(event_id), "zones": zones})
    except Exception:  # never fail an operator action that already committed
        log.warning("operations.push_failed", event_id=str(event_id))


class ManualOpenIn(BaseModel):
    request_id: str = RequestId
    reason: str = Reason
    ticket_code: str | None = Field(
        default=None, max_length=64, description="exit lanes: let this car out (fee due is recorded as released unpaid)"
    )


@router.post("/gates/{gate_id}/manual-open")
async def manual_open(
    gate_id: uuid.UUID, body: ManualOpenIn, principal: OperatorDep, session: SessionDep, redis: RedisDep
) -> OperatorResultOut:
    result = await ops.manual_open(
        session,
        gate_id=gate_id,
        actor=principal.user_id,
        reason=body.reason,
        request_id=body.request_id,
        ticket_code=body.ticket_code,
        now=utcnow(),
    )
    if result.ticket_id is not None and not result.replayed:
        await _push_occupancy(redis, session, await ops.event_of_ticket(session, result.ticket_id))
    return OperatorResultOut(**result.__dict__)


class VoidIn(BaseModel):
    reason: str = Reason


class VoidOut(BaseModel):
    ticket_id: uuid.UUID
    status: TicketStatus


@router.post("/tickets/{ticket_id}/void")
async def void_ticket(
    ticket_id: uuid.UUID, body: VoidIn, principal: OperatorDep, session: SessionDep, redis: RedisDep
) -> VoidOut:
    ticket, event_id = await ops.void_ticket(
        session, ticket_id=ticket_id, actor=principal.user_id, reason=body.reason, now=utcnow()
    )
    await _push_occupancy(redis, session, event_id)
    return VoidOut(ticket_id=ticket.id, status=ticket.status)


class LostTicketIn(BaseModel):
    request_id: str = RequestId
    gate_id: uuid.UUID = Field(description="the exit gate where the driver is waiting")
    ticket_code: str | None = Field(default=None, max_length=64)
    plate: str | None = Field(default=None, max_length=32, description="to find the ticket if the code is lost")


@router.post("/tickets/lost")
async def lost_ticket(
    body: LostTicketIn, principal: OperatorDep, session: SessionDep, redis: RedisDep
) -> OperatorResultOut:
    result = await ops.lost_ticket(
        session,
        gate_id=body.gate_id,
        actor=principal.user_id,
        request_id=body.request_id,
        ticket_code=body.ticket_code,
        plate=body.plate,
        now=utcnow(),
    )
    if result.ticket_id is not None and not result.replayed:
        await _push_occupancy(redis, session, await ops.event_of_ticket(session, result.ticket_id))
    return OperatorResultOut(**result.__dict__)
