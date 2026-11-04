"""Reports (async generation) and event documents (browser -> S3 directly) (docs/02-architecture.md §3.5-3.6)."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound, ValidationFailed
from eventpark.db.models import Document, Event, Report
from eventpark.messaging import outbox
from eventpark.services import audit
from eventpark.storage.s3 import Storage

ALLOWED_DOCUMENT_TYPES = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "text/plain": "txt",
    "text/csv": "csv",
}
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class UploadTicket:
    document: Document
    upload_url: str
    expires_in: int


async def request_report(session: AsyncSession, *, actor: uuid.UUID, event_id: uuid.UUID, kind: str) -> Report:
    async with session.begin():
        if await session.get(Event, event_id) is None:
            raise NotFound("Event not found.", code="EVENT_NOT_FOUND")
        report = Report(id=uuid.uuid4(), event_id=event_id, kind=kind, requested_by=actor)
        session.add(report)
        await session.flush()
        # Same transaction: the report row exists if and only if the worker will hear about it.
        outbox.add(session, "report.requested", {"report_id": report.id, "kind": kind}, event_id=event_id)
        audit.record(session, actor_user_id=actor, action="report.requested", target_type="report", target_id=report.id)
    return report


async def get_report(session: AsyncSession, report_id: uuid.UUID) -> Report:
    report = await session.get(Report, report_id)
    if report is None:
        raise NotFound("Report not found.", code="REPORT_NOT_FOUND")
    return report


def report_download_url(storage: Storage, report: Report) -> str:
    if report.status != "READY" or not report.s3_key:
        raise Conflict(f"Report is {report.status}.", code="REPORT_NOT_READY")
    filename = f"eventpark-{report.event_id}-{report.id}.{report.kind.lower()}"
    return storage.presigned_get(report.s3_key, expires=300, filename=filename)


def _safe_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")[:100] or "document"


async def create_upload(
    session: AsyncSession,
    storage: Storage,
    *,
    actor: uuid.UUID,
    event_id: uuid.UUID,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> UploadTicket:
    """Validate, record a PENDING document and hand out a presigned PUT URL. The bytes go browser -> S3."""
    if content_type not in ALLOWED_DOCUMENT_TYPES:
        raise ValidationFailed(f"Type {content_type} is not allowed.", code="CONTENT_TYPE_NOT_ALLOWED")
    if not 0 < size_bytes <= MAX_DOCUMENT_BYTES:
        raise ValidationFailed("File is empty or larger than 20 MB.", code="FILE_TOO_LARGE")
    async with session.begin():
        if await session.get(Event, event_id) is None:
            raise NotFound("Event not found.", code="EVENT_NOT_FOUND")
        document_id = uuid.uuid4()
        document = Document(
            id=document_id,
            event_id=event_id,
            filename=_safe_filename(filename),
            content_type=content_type,
            size_bytes=size_bytes,
            # The key is ours, never the user's filename: no path tricks, no overwriting someone else's file.
            s3_key=f"documents/{event_id}/{document_id}.{ALLOWED_DOCUMENT_TYPES[content_type]}",
            uploaded_by=actor,
        )
        session.add(document)
    url = storage.presigned_put(document.s3_key, content_type=content_type, expires=300)
    return UploadTicket(document=document, upload_url=url, expires_in=300)


async def confirm_upload(
    session: AsyncSession, storage: Storage, *, actor: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """The browser says 'uploaded'. Don't trust it: ask S3 whether the object exists with the promised size."""
    document = await session.get(Document, document_id)
    await session.commit()
    if document is None:
        raise NotFound("Document not found.", code="DOCUMENT_NOT_FOUND")
    if document.status == "READY":
        return document
    info = await storage.head(document.s3_key)
    if info is None:
        raise Conflict("The file hasn't been uploaded yet.", code="UPLOAD_MISSING")
    if info.size != document.size_bytes:
        raise Conflict(
            f"Uploaded size {info.size} doesn't match the announced {document.size_bytes}.", code="UPLOAD_SIZE_MISMATCH"
        )
    async with session.begin():
        current = await session.get(Document, document_id, with_for_update=True)
        if current is not None:
            current.status = "READY"
            audit.record(
                session, actor_user_id=actor, action="document.uploaded", target_type="document", target_id=document_id
            )
    return current or document


async def list_documents(session: AsyncSession, event_id: uuid.UUID) -> Sequence[Document]:
    return (
        await session.scalars(
            select(Document)
            .where(Document.event_id == event_id, Document.status == "READY")
            .order_by(Document.created_at)
        )
    ).all()


def document_download_url(storage: Storage, document: Document) -> str:
    return storage.presigned_get(document.s3_key, expires=300, filename=document.filename)
