"""Admin: venues, zones, spaces, gates, devices. Writes: ADMIN. Reads: any staff role."""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventpark.api.deps import AdminDep, SessionDep, StaffDep
from eventpark.domain.venues import DeviceStatus, GateDirection, PrinterStatus, SpaceKind
from eventpark.services import venues

router = APIRouter(prefix="/admin/v1", tags=["admin: venues"])

Code = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
Name = Field(min_length=1, max_length=200)


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- schemas ---


def _known_timezone(value: str | None) -> str | None:
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown IANA time zone, e.g. 'Europe/Belgrade'") from exc
    return value


class VenueIn(BaseModel):
    name: str = Name
    address: str = Field(default="", max_length=500)
    timezone: str = Field(examples=["Europe/Belgrade"])

    _tz = field_validator("timezone")(_known_timezone)


class VenuePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    timezone: str | None = None

    _tz = field_validator("timezone")(_known_timezone)


class VenueOut(Orm):
    id: uuid.UUID
    name: str
    address: str
    timezone: str
    created_at: datetime


class ZoneIn(BaseModel):
    code: str = Code
    name: str = Name
    total_capacity: int = Field(gt=0, le=1_000_000)


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    total_capacity: int | None = Field(default=None, gt=0, le=1_000_000)


class ZoneOut(Orm):
    id: uuid.UUID
    venue_id: uuid.UUID
    code: str
    name: str
    total_capacity: int


class SpacesIn(BaseModel):
    """Either explicit `labels`, or a numbered range: prefix="VIP-", count=50 -> VIP-01 ... VIP-50."""

    kind: SpaceKind
    labels: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    prefix: str | None = Field(default=None, max_length=16)
    count: int | None = Field(default=None, ge=1, le=1000)
    start: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _one_way(self) -> "SpacesIn":
        if (self.labels is None) == (self.count is None):
            raise ValueError("give either `labels` or `prefix` + `count`")
        return self

    def resolved_labels(self) -> list[str]:
        if self.labels is not None:
            return self.labels
        count = self.count or 0
        width = max(2, len(str(self.start + count - 1)))
        return [f"{self.prefix or ''}{n:0{width}d}" for n in range(self.start, self.start + count)]


class SpaceOut(Orm):
    id: uuid.UUID
    zone_id: uuid.UUID
    label: str
    kind: SpaceKind


class GateIn(BaseModel):
    code: str = Code
    name: str = Name
    direction: GateDirection
    has_printer: bool = False
    has_terminal: bool = False


class GatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    has_printer: bool | None = None
    has_terminal: bool | None = None


class GateOut(Orm):
    id: uuid.UUID
    venue_id: uuid.UUID
    code: str
    name: str
    direction: GateDirection
    has_printer: bool
    has_terminal: bool


class DeviceIn(BaseModel):
    name: str = Name


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = Field(default=None, description="false = DISABLED (key refused); true = re-enable")


class DeviceOut(Orm):
    id: uuid.UUID
    gate_id: uuid.UUID
    name: str
    api_key_prefix: str
    status: DeviceStatus
    last_heartbeat_at: datetime | None
    firmware_version: str | None
    printer_status: PrinterStatus | None
    created_at: datetime


class DeviceWithKeyOut(BaseModel):
    device: DeviceOut
    api_key: str = Field(description="Shown ONCE. Store it on the device; the server keeps only a hash.")


def _with_key(result: venues.DeviceWithKey) -> DeviceWithKeyOut:
    return DeviceWithKeyOut(device=DeviceOut.model_validate(result.device), api_key=result.api_key)


# --- venues ---


@router.get("/venues")
async def list_venues(_: StaffDep, session: SessionDep) -> list[VenueOut]:
    return [VenueOut.model_validate(v) for v in await venues.list_venues(session)]


@router.post("/venues", status_code=status.HTTP_201_CREATED)
async def create_venue(body: VenueIn, principal: AdminDep, session: SessionDep) -> VenueOut:
    venue = await venues.create_venue(session, actor=principal.user_id, **body.model_dump())
    return VenueOut.model_validate(venue)


@router.get("/venues/{venue_id}")
async def get_venue(venue_id: uuid.UUID, _: StaffDep, session: SessionDep) -> VenueOut:
    return VenueOut.model_validate(await venues.get_venue(session, venue_id))


@router.patch("/venues/{venue_id}")
async def update_venue(venue_id: uuid.UUID, body: VenuePatch, principal: AdminDep, session: SessionDep) -> VenueOut:
    venue = await venues.update_venue(session, actor=principal.user_id, venue_id=venue_id, **body.model_dump())
    return VenueOut.model_validate(venue)


@router.delete("/venues/{venue_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_venue(venue_id: uuid.UUID, principal: AdminDep, session: SessionDep) -> Response:
    await venues.delete_venue(session, actor=principal.user_id, venue_id=venue_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- zones ---


@router.get("/venues/{venue_id}/zones")
async def list_zones(venue_id: uuid.UUID, _: StaffDep, session: SessionDep) -> list[ZoneOut]:
    return [ZoneOut.model_validate(z) for z in await venues.list_zones(session, venue_id)]


@router.post("/venues/{venue_id}/zones", status_code=status.HTTP_201_CREATED)
async def create_zone(venue_id: uuid.UUID, body: ZoneIn, principal: AdminDep, session: SessionDep) -> ZoneOut:
    zone = await venues.create_zone(session, actor=principal.user_id, venue_id=venue_id, **body.model_dump())
    return ZoneOut.model_validate(zone)


@router.patch("/zones/{zone_id}")
async def update_zone(zone_id: uuid.UUID, body: ZonePatch, principal: AdminDep, session: SessionDep) -> ZoneOut:
    zone = await venues.update_zone(session, actor=principal.user_id, zone_id=zone_id, **body.model_dump())
    return ZoneOut.model_validate(zone)


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_zone(zone_id: uuid.UUID, principal: AdminDep, session: SessionDep) -> Response:
    await venues.delete_zone(session, actor=principal.user_id, zone_id=zone_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- spaces ---


@router.get("/zones/{zone_id}/spaces")
async def list_spaces(zone_id: uuid.UUID, _: StaffDep, session: SessionDep) -> list[SpaceOut]:
    return [SpaceOut.model_validate(s) for s in await venues.list_spaces(session, zone_id)]


@router.post("/zones/{zone_id}/spaces", status_code=status.HTTP_201_CREATED)
async def create_spaces(zone_id: uuid.UUID, body: SpacesIn, principal: AdminDep, session: SessionDep) -> list[SpaceOut]:
    spaces = await venues.create_spaces(
        session, actor=principal.user_id, zone_id=zone_id, labels=body.resolved_labels(), kind=body.kind
    )
    return [SpaceOut.model_validate(s) for s in spaces]


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(space_id: uuid.UUID, principal: AdminDep, session: SessionDep) -> Response:
    await venues.delete_space(session, actor=principal.user_id, space_id=space_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- gates ---


@router.get("/venues/{venue_id}/gates")
async def list_gates(venue_id: uuid.UUID, _: StaffDep, session: SessionDep) -> list[GateOut]:
    return [GateOut.model_validate(g) for g in await venues.list_gates(session, venue_id)]


@router.post("/venues/{venue_id}/gates", status_code=status.HTTP_201_CREATED)
async def create_gate(venue_id: uuid.UUID, body: GateIn, principal: AdminDep, session: SessionDep) -> GateOut:
    gate = await venues.create_gate(session, actor=principal.user_id, venue_id=venue_id, **body.model_dump())
    return GateOut.model_validate(gate)


@router.patch("/gates/{gate_id}")
async def update_gate(gate_id: uuid.UUID, body: GatePatch, principal: AdminDep, session: SessionDep) -> GateOut:
    gate = await venues.update_gate(session, actor=principal.user_id, gate_id=gate_id, **body.model_dump())
    return GateOut.model_validate(gate)


@router.delete("/gates/{gate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gate(gate_id: uuid.UUID, principal: AdminDep, session: SessionDep) -> Response:
    await venues.delete_gate(session, actor=principal.user_id, gate_id=gate_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- device (one per gate) ---


@router.get("/gates/{gate_id}/device")
async def get_device(gate_id: uuid.UUID, _: StaffDep, session: SessionDep) -> DeviceOut:
    return DeviceOut.model_validate(await venues.get_device(session, gate_id))


@router.post("/gates/{gate_id}/device", status_code=status.HTTP_201_CREATED)
async def create_device(
    gate_id: uuid.UUID, body: DeviceIn, principal: AdminDep, session: SessionDep
) -> DeviceWithKeyOut:
    return _with_key(await venues.create_device(session, actor=principal.user_id, gate_id=gate_id, name=body.name))


@router.post("/gates/{gate_id}/device/rotate-key")
async def rotate_device_key(gate_id: uuid.UUID, principal: AdminDep, session: SessionDep) -> DeviceWithKeyOut:
    return _with_key(await venues.rotate_device_key(session, actor=principal.user_id, gate_id=gate_id))


@router.patch("/gates/{gate_id}/device")
async def update_device(gate_id: uuid.UUID, body: DevicePatch, principal: AdminDep, session: SessionDep) -> DeviceOut:
    device = await venues.update_device(
        session, actor=principal.user_id, gate_id=gate_id, name=body.name, enabled=body.enabled
    )
    return DeviceOut.model_validate(device)
