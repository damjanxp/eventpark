"""worker-reports: generate CSV / PDF reports and put them in S3.

Report generation can take long for a big event, which is exactly why it's asynchronous: the API answers "QUEUED"
in milliseconds and the worker takes its time. The consumer keeps extending the message's visibility timeout while
we work, so SQS doesn't think we died and hand the job to a second worker.

CSV rows are streamed from Postgres with a server-side cursor (`session.stream`), so memory stays flat however many
passages the event has.
"""

import csv
import io
import uuid
from datetime import UTC, datetime
from typing import ClassVar

import structlog
from fpdf import FPDF
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.config import Settings
from eventpark.db.models import Event, Gate, HourlyStat, Passage, Payment, Report, Ticket
from eventpark.domain.payments import PaymentStatus
from eventpark.messaging import outbox
from eventpark.messaging.schemas import Envelope
from eventpark.storage.s3 import Storage
from eventpark.workers.consumer import Handler

log = structlog.get_logger()

CSV_COLUMNS = [
    "passage_id",
    "gate",
    "direction",
    "status",
    "reject_reason",
    "ticket_type",
    "authorized_at",
    "completed_at",
    "decision_ms",
    "amount_due_minor",
    "paid_minor",
]


def report_key(event_id: uuid.UUID | str, report_id: uuid.UUID | str, kind: str) -> str:
    return f"reports/{event_id}/{report_id}.{kind.lower()}"


async def build_csv(session: AsyncSession, event_id: uuid.UUID) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    query = (
        select(
            Passage.id,
            Gate.code,
            Passage.direction,
            Passage.status,
            Passage.reject_reason,
            Ticket.type,
            Passage.authorized_at,
            Passage.completed_at,
            Passage.decision_ms,
            Passage.amount_due_minor,
            Ticket.paid_minor,
        )
        .join(Gate, Gate.id == Passage.gate_id)
        .outerjoin(Ticket, Ticket.id == Passage.ticket_id)
        .where(Passage.event_id == event_id)
        .order_by(Passage.id)
    )
    result = await session.stream(query.execution_options(yield_per=1000))
    async for row in result:
        writer.writerow(["" if v is None else getattr(v, "value", v) for v in row])
    return buffer.getvalue().encode()


async def build_pdf(session: AsyncSession, event_id: uuid.UUID) -> bytes:
    event = await session.get(Event, event_id)
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(HourlyStat.entries), 0),
                func.coalesce(func.sum(HourlyStat.exits), 0),
                func.coalesce(func.sum(HourlyStat.rejections), 0),
            ).where(HourlyStat.event_id == event_id)
        )
    ).one()
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0))
        .outerjoin(Ticket, Ticket.id == Payment.ticket_id)
        .where(Payment.status == PaymentStatus.SUCCEEDED, Ticket.event_id == event_id)
    )
    hours = (
        await session.execute(
            select(HourlyStat.hour_start, func.sum(HourlyStat.entries), func.sum(HourlyStat.exits))
            .where(HourlyStat.event_id == event_id)
            .group_by(HourlyStat.hour_start)
            .order_by(HourlyStat.hour_start)
        )
    ).all()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, f"EventPark report: {event.name if event else event_id}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 8, f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    for label, value in (
        ("Entries", totals[0]),
        ("Exits", totals[1]),
        ("Rejections", totals[2]),
        ("Lane revenue", f"{(revenue or 0) // 100:,}.{(revenue or 0) % 100:02d} RSD"),
    ):
        pdf.cell(60, 8, label)
        pdf.cell(0, 8, str(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Entries per hour", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    peak = max((int(h[1] or 0) for h in hours), default=0) or 1
    for hour_start, entries, exits in hours:
        pdf.cell(32, 6, f"{hour_start:%m-%d %H:00}")
        width = 120 * int(entries or 0) / peak
        if width:
            pdf.rect(pdf.get_x(), pdf.get_y() + 1, width, 4, style="F")  # a simple bar chart
        pdf.set_x(pdf.get_x() + 125)
        pdf.cell(0, 6, f"in {entries}  out {exits}", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


class Reports(Handler):
    name: ClassVar[str] = "worker-reports"
    queue: ClassVar[str] = "reports"
    extend_visibility_seconds: ClassVar[int | None] = 300

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: Settings, *, storage: Storage | None = None
    ) -> None:
        super().__init__(sessions, settings)
        self.storage = storage or Storage(self.settings)

    def wants(self, msg: Envelope) -> bool:
        return msg.type == "report.requested"

    async def side_effects(self, msg: Envelope) -> None:
        report_id = uuid.UUID(msg.data["report_id"])
        async with self.sessions() as session:
            report = await session.get(Report, report_id)
            if report is None or report.status == "READY":
                return
            await session.commit()  # end the read transaction, then mark RUNNING in its own one
            async with session.begin():
                await session.execute(update(Report).where(Report.id == report_id).values(status="RUNNING"))
            try:
                if report.kind == "CSV":
                    body, content_type = await build_csv(session, report.event_id), "text/csv"
                else:
                    body, content_type = await build_pdf(session, report.event_id), "application/pdf"
                await session.commit()
            except Exception as exc:
                async with session.begin():
                    await session.execute(
                        update(Report).where(Report.id == report_id).values(status="FAILED", error=str(exc)[:500])
                    )
                raise
        # Deterministic key: if we crash after this and the message comes back, we overwrite the same object.
        await self.storage.put(report_key(report.event_id, report_id, report.kind), body, content_type)
        log.info("report.generated", report_id=str(report_id), kind=report.kind, bytes=len(body))

    async def apply(self, session: AsyncSession, msg: Envelope) -> None:
        report_id = uuid.UUID(msg.data["report_id"])
        report = await session.get(Report, report_id, with_for_update=True)
        if report is None or report.status == "READY":
            return
        report.status, report.s3_key = "READY", report_key(report.event_id, report_id, report.kind)
        report.completed_at, report.error = datetime.now(UTC), None
        outbox.add(
            session,
            "report.ready",
            {"report_id": report_id, "requested_by": report.requested_by},
            event_id=report.event_id,
        )
