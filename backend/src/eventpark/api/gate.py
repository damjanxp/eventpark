"""Gate device API (/gate/v1). Authenticated with `Authorization: Device <api_key>`, never with a user token.

Every endpoint is safe to retry: scan/issue carry the device's scan_id/request_id (stored as the passage's
idempotency key), complete/abort are idempotent by state. A device that times out simply sends the same request again.
"""

import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from eventpark.api.deps import LimiterDep, SessionDep, SettingsDep, enforce, utcnow
from eventpark.api.public import ProviderDep
from eventpark.cache.rate_limit import GATE_PER_DEVICE
from eventpark.core.errors import Unauthorized
from eventpark.domain.tickets import PassageStatus, TicketStatus
from eventpark.domain.venues import PrinterStatus
from eventpark.services import exit_payment, gate
from eventpark.services.gate import DeviceContext, GateDecision, GateSettings

router = APIRouter(prefix="/gate/v1", tags=["gate devices"])

_device_header = APIKeyHeader(
    name="Authorization",
    auto_error=False,
    description="`Device epd_<prefix>_<secret>` (from POST /admin/v1/gates/{id}/device)",
)

# Idempotency keys: what the device generated (a UUID per action, reused for every retry of that action).
IdempotencyKey = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


async def get_device(
    session: SessionDep, limiter: LimiterDep, header: Annotated[str | None, Depends(_device_header)]
) -> DeviceContext:
    if not header or not header.startswith("Device "):
        raise Unauthorized("Missing device credentials.", code="NOT_AUTHENTICATED")
    device = await gate.authenticate_device(session, header.removeprefix("Device ").strip())
    # Per device: a lane stuck in a retry loop (bug, broken scanner) can't flood the API for everyone else.
    await enforce(limiter, GATE_PER_DEVICE, str(device.device_id))
    return device


DeviceDep = Annotated[DeviceContext, Depends(get_device)]


def _cfg(settings: SettingsDep) -> GateSettings:
    return GateSettings(
        hold=timedelta(seconds=settings.gate_hold_seconds),
        payment_window=timedelta(seconds=settings.payment_pending_seconds),
    )


GateCfg = Annotated[GateSettings, Depends(_cfg)]


class ScanRequest(BaseModel):
    scan_id: str = IdempotencyKey
    ticket_code: str = Field(min_length=4, max_length=64)


class IssueRequest(BaseModel):
    request_id: str = IdempotencyKey


class PayRequest(BaseModel):
    request_id: str = IdempotencyKey
    passage_id: uuid.UUID
    card_token: str = Field(
        min_length=3, max_length=64, description="from the card terminal (fake: tok_ok, tok_decline, tok_slow)"
    )


class PassageEventRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=64)


class PassageOut(BaseModel):
    passage_id: uuid.UUID
    status: PassageStatus
    ticket_status: TicketStatus | None


class HeartbeatRequest(BaseModel):
    firmware_version: str | None = Field(default=None, max_length=64)
    printer_status: PrinterStatus | None = None


class CommandOut(BaseModel):
    id: uuid.UUID
    command: str
    reason: str
    created_at: datetime


class HeartbeatResponse(BaseModel):
    server_time: datetime
    commands: list[CommandOut]


@router.post("/entry/scan")
async def entry_scan(body: ScanRequest, device: DeviceDep, session: SessionDep, cfg: GateCfg) -> GateDecision:
    """Pre-booked QR at an entry lane -> OPEN (+ zone/space) or DENY with a reason."""
    return await gate.entry_scan(
        session, device, scan_id=body.scan_id, ticket_code=body.ticket_code, now=utcnow(), cfg=cfg
    )


@router.post("/entry/issue")
async def entry_issue(body: IssueRequest, device: DeviceDep, session: SessionDep, cfg: GateCfg) -> GateDecision:
    """Ticket button pressed -> pick a zone, take a place atomically, print a drive-up ticket (or DENY: full)."""
    return await gate.entry_issue(session, device, request_id=body.request_id, now=utcnow(), cfg=cfg)


@router.post("/exit/scan")
async def exit_scan(body: ScanRequest, device: DeviceDep, session: SessionDep, cfg: GateCfg) -> GateDecision:
    """Ticket at an exit lane -> OPEN, PAYMENT_REQUIRED (drive-up with a fee due) or DENY."""
    return await gate.exit_scan(
        session, device, scan_id=body.scan_id, ticket_code=body.ticket_code, now=utcnow(), cfg=cfg
    )


@router.post("/exit/pay")
async def exit_pay(
    body: PayRequest, device: DeviceDep, session: SessionDep, cfg: GateCfg, provider: ProviderDep
) -> GateDecision:
    """Card tapped at an exit lane -> OPEN, DECLINED or PAYMENT_PENDING (unknown yet: retry, SAME request_id)."""
    return await exit_payment.exit_pay(
        session,
        device,
        provider,
        request_id=body.request_id,
        passage_id=body.passage_id,
        card_token=body.card_token,
        now=utcnow(),
        cfg=cfg,
    )


@router.post("/passages/{passage_id}/complete")
async def complete(
    passage_id: uuid.UUID, device: DeviceDep, session: SessionDep, _: PassageEventRequest | None = None
) -> PassageOut:
    """Loop detector: the car has passed the barrier. Safe to repeat."""
    result = await gate.complete_passage(session, device, passage_id=passage_id, now=utcnow())
    return PassageOut(passage_id=result.passage_id, status=result.status, ticket_status=result.ticket_status)


@router.post("/passages/{passage_id}/abort")
async def abort(
    passage_id: uuid.UUID, device: DeviceDep, session: SessionDep, _: PassageEventRequest | None = None
) -> PassageOut:
    """The car reversed away without passing: release the hold. Safe to repeat."""
    result = await gate.abort_passage(session, device, passage_id=passage_id, now=utcnow())
    return PassageOut(passage_id=result.passage_id, status=result.status, ticket_status=result.ticket_status)


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, request: Request, device: DeviceDep, session: SessionDep
) -> HeartbeatResponse:
    now = utcnow()
    commands = await gate.heartbeat(
        session,
        device,
        now=now,
        firmware_version=body.firmware_version,
        printer_status=body.printer_status,
        ip=request.client.host if request.client else None,
    )
    return HeartbeatResponse(
        server_time=now,
        commands=[CommandOut(id=c.id, command=c.command, reason=c.reason, created_at=c.created_at) for c in commands],
    )


@router.post("/commands/{command_id}/ack", status_code=204)
async def ack_command(command_id: uuid.UUID, device: DeviceDep, session: SessionDep) -> None:
    await gate.ack_command(session, device, command_id=command_id, now=utcnow())
