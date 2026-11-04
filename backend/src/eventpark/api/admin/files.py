"""Admin: reports (async), event documents (presigned upload), event history. ADMIN / EVENT_MANAGER."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from eventpark.api.deps import EventManagerDep, SessionDep, StaffDep
from eventpark.db.models import Event, EventSummary
from eventpark.services import files
from eventpark.storage.s3 import Storage

router = APIRouter(prefix="/admin/v1", tags=["admin: reports & documents"])


def get_storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


StorageDep = Annotated[Storage, Depends(get_storage)]


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ReportIn(BaseModel):
    kind: Literal["CSV", "PDF"]


class ReportOut(Orm):
    id: uuid.UUID
    event_id: uuid.UUID
    kind: str
    status: str
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class DownloadOut(BaseModel):
    url: str = Field(description="Presigned S3 URL, valid for 5 minutes")
    expires_in: int = 300


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str
    size_bytes: int = Field(gt=0)


class DocumentOut(Orm):
    id: uuid.UUID
    event_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    created_at: datetime


class UploadOut(BaseModel):
    document: DocumentOut
    upload_url: str = Field(description="PUT the file here with the same Content-Type, then call /confirm")
    expires_in: int


class DocumentWithUrl(DocumentOut):
    download_url: str


class HistoryOut(BaseModel):
    event_id: uuid.UUID
    name: str
    starts_at: datetime
    peak_occupancy: int
    total_entries: int
    total_exits: int
    revenue_minor: int
    avg_stay_minutes: int | None
    busiest_hour: datetime | None
    rejection_rate: float


@router.post("/events/{event_id}/reports", status_code=status.HTTP_202_ACCEPTED)
async def request_report(
    event_id: uuid.UUID, body: ReportIn, principal: EventManagerDep, session: SessionDep
) -> ReportOut:
    """202 Accepted: the report is QUEUED; poll GET /reports/{id} until READY (or watch the dashboard notification)."""
    report = await files.request_report(session, actor=principal.user_id, event_id=event_id, kind=body.kind)
    return ReportOut.model_validate(report)


@router.get("/reports/{report_id}")
async def get_report(report_id: uuid.UUID, _: EventManagerDep, session: SessionDep) -> ReportOut:
    return ReportOut.model_validate(await files.get_report(session, report_id))


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: uuid.UUID, _: EventManagerDep, session: SessionDep, storage: StorageDep
) -> DownloadOut:
    return DownloadOut(url=files.report_download_url(storage, await files.get_report(session, report_id)))


@router.post("/events/{event_id}/documents/upload-url", status_code=status.HTTP_201_CREATED)
async def document_upload_url(
    event_id: uuid.UUID, body: UploadIn, principal: EventManagerDep, session: SessionDep, storage: StorageDep
) -> UploadOut:
    ticket = await files.create_upload(
        session,
        storage,
        actor=principal.user_id,
        event_id=event_id,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
    )
    return UploadOut(
        document=DocumentOut.model_validate(ticket.document), upload_url=ticket.upload_url, expires_in=ticket.expires_in
    )


@router.post("/documents/{document_id}/confirm")
async def confirm_document(
    document_id: uuid.UUID, principal: EventManagerDep, session: SessionDep, storage: StorageDep
) -> DocumentOut:
    return DocumentOut.model_validate(
        await files.confirm_upload(session, storage, actor=principal.user_id, document_id=document_id)
    )


@router.get("/events/{event_id}/documents")
async def list_documents(
    event_id: uuid.UUID, _: EventManagerDep, session: SessionDep, storage: StorageDep
) -> list[DocumentWithUrl]:
    return [
        DocumentWithUrl(
            **DocumentOut.model_validate(d).model_dump(), download_url=files.document_download_url(storage, d)
        )
        for d in await files.list_documents(session, event_id)
    ]


@router.get("/history/events")
async def history(_: StaffDep, session: SessionDep) -> list[HistoryOut]:
    rows = (
        await session.execute(
            select(EventSummary, Event.name, Event.starts_at)
            .join(Event, Event.id == EventSummary.event_id)
            .order_by(Event.starts_at.desc())
        )
    ).all()
    return [
        HistoryOut(
            event_id=s.event_id,
            name=name,
            starts_at=starts_at,
            peak_occupancy=s.peak_occupancy,
            total_entries=s.total_entries,
            total_exits=s.total_exits,
            revenue_minor=s.revenue_minor,
            avg_stay_minutes=s.avg_stay_minutes,
            busiest_hour=s.busiest_hour,
            rejection_rate=s.rejection_rate,
        )
        for s, name, starts_at in rows
    ]
