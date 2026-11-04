"""worker-notifications: QR codes, emails, staff alerts.

booking.confirmed -> QR PNG (just the ticket code: opaque, no data in it) -> S3 qr/{ticket_id}.png (deterministic key:
writing it twice is harmless) -> email with the QR to the driver -> tickets.qr_s3_key set.
"""

import uuid
from typing import ClassVar

import segno
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.mail import Attachment, Mail, Mailer
from eventpark.db.models import Booking, Event, EventZone, Ticket, User, Zone
from eventpark.messaging.schemas import Envelope
from eventpark.storage.s3 import Storage
from eventpark.workers.consumer import Handler

log = structlog.get_logger()


def qr_png(content: str) -> bytes:
    import io

    buffer = io.BytesIO()
    segno.make(content, error="m").save(buffer, kind="png", scale=8, border=4)
    return buffer.getvalue()


def qr_key(ticket_id: uuid.UUID | str) -> str:
    return f"qr/{ticket_id}.png"


class Notifications(Handler):
    name: ClassVar[str] = "worker-notifications"
    queue: ClassVar[str] = "notifications"

    def __init__(
        self, *args: object, storage: Storage | None = None, mailer: Mailer | None = None, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.storage = storage or Storage(self.settings)
        self.mailer = mailer or Mailer(self.settings.smtp_url, self.settings.mail_from)

    async def side_effects(self, msg: Envelope) -> None:
        if msg.type == "booking.confirmed":
            await self._booking_confirmed(msg)
        elif msg.type == "booking.expired":
            await self._booking_expired(msg)
        elif msg.type.startswith("alert."):
            log.warning(
                "alert", alert=msg.type, **{k: str(v) for k, v in msg.data.items()}
            )  # Phase 7: live to dashboards

    async def apply(self, session: AsyncSession, msg: Envelope) -> None:
        if msg.type == "booking.confirmed":
            ticket_id = msg.data["ticket_id"]
            await session.execute(
                update(Ticket).where(Ticket.id == uuid.UUID(ticket_id)).values(qr_s3_key=qr_key(ticket_id))
            )

    async def _booking_confirmed(self, msg: Envelope) -> None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(Ticket.code, User.email, User.full_name, Event.name, Event.gates_open_at, Zone.code)
                    .join(Booking, Booking.id == Ticket.booking_id)
                    .join(User, User.id == Booking.user_id)
                    .join(Event, Event.id == Ticket.event_id)
                    .join(EventZone, EventZone.id == Ticket.event_zone_id)
                    .join(Zone, Zone.id == EventZone.zone_id)
                    .where(Ticket.id == uuid.UUID(msg.data["ticket_id"]))
                )
            ).one()
        code, email, full_name, event_name, gates_open_at, zone_code = row
        png = qr_png(code)
        await self.storage.put(qr_key(msg.data["ticket_id"]), png, "image/png")
        await self.mailer.send(
            Mail(
                to=email,
                subject=f"Your parking pass for {event_name}",
                text=(
                    f"Hi {full_name},\n\nyour parking pass for {event_name} (zone {zone_code}) is confirmed.\n"
                    f"Gates open {gates_open_at:%Y-%m-%d %H:%M} UTC. Show the attached QR code at any entry lane.\n\n"
                    f"Pass code: {code}\n\nEventPark"
                ),
                attachments=[Attachment("parking-pass.png", png)],
            )
        )

    async def _booking_expired(self, msg: Envelope) -> None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(User.email, Event.name)
                    .join(Booking, Booking.user_id == User.id)
                    .join(Event, Event.id == Booking.event_id)
                    .where(Booking.id == uuid.UUID(msg.data["booking_id"]))
                )
            ).first()
        if row is None:
            return
        refund = (
            " Your payment arrived after the place was released: it will be refunded."
            if msg.data.get("refund_required")
            else ""
        )
        await self.mailer.send(
            Mail(
                to=row[0],
                subject=f"Booking for {row[1]} expired",
                text=f"Your booking was not paid in time and has expired.{refund}",
            )
        )
