"""Event summary for the history page, computed once when an event is archived (docs/01 §10 "Historical statistics")."""

import uuid

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.db.models import Booking, EventSummary, HourlyStat, Passage, Payment, Ticket
from eventpark.domain.payments import PaymentStatus
from eventpark.domain.tickets import PassageStatus, TicketStatus
from eventpark.domain.venues import GateDirection


async def compute_summary(session: AsyncSession, event_id: uuid.UUID) -> None:
    completed = (Passage.event_id == event_id) & (Passage.status == PassageStatus.COMPLETED)
    entries = await session.scalar(select(func.count()).where(completed, Passage.direction == GateDirection.ENTRY)) or 0
    exits = await session.scalar(select(func.count()).where(completed, Passage.direction == GateDirection.EXIT)) or 0
    total = await session.scalar(select(func.count()).select_from(Passage).where(Passage.event_id == event_id)) or 0
    rejected = (
        await session.scalar(
            select(func.count())
            .select_from(Passage)
            .where(Passage.event_id == event_id, Passage.status == PassageStatus.REJECTED)
        )
        or 0
    )
    # Peak occupancy: walk through completed passages in time order, +1 per entry, -1 per exit; take the maximum.
    peak_row = (
        await session.execute(
            text(
                """
                SELECT occupancy, completed_at FROM (
                  SELECT completed_at,
                         SUM(CASE WHEN direction = 'ENTRY' THEN 1 ELSE -1 END)
                           OVER (ORDER BY completed_at, id) AS occupancy
                    FROM passages WHERE event_id = :event_id AND status = 'COMPLETED'
                ) walk ORDER BY occupancy DESC, completed_at LIMIT 1
                """
            ),
            {"event_id": event_id},
        )
    ).first()
    avg_stay = await session.scalar(
        select(cast(func.avg(func.extract("epoch", Ticket.exited_at - Ticket.entered_at) / 60), Integer)).where(
            Ticket.event_id == event_id, Ticket.status == TicketStatus.EXITED
        )
    )
    busiest = (
        await session.execute(
            select(HourlyStat.hour_start, func.sum(HourlyStat.entries).label("n"))
            .where(HourlyStat.event_id == event_id)
            .group_by(HourlyStat.hour_start)
            .order_by(text("n DESC"))
            .limit(1)
        )
    ).first()
    lane_revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0))
        .join(Ticket, Ticket.id == Payment.ticket_id)
        .where(Ticket.event_id == event_id, Payment.status == PaymentStatus.SUCCEEDED)
    )
    booking_revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0))
        .join(Booking, Booking.id == Payment.booking_id)
        .where(Booking.event_id == event_id, Payment.status == PaymentStatus.SUCCEEDED)
    )
    values = {
        "peak_occupancy": int(peak_row[0]) if peak_row else 0,
        "peak_at": peak_row[1] if peak_row else None,
        "total_entries": int(entries),
        "total_exits": int(exits),
        "revenue_minor": int(lane_revenue or 0) + int(booking_revenue or 0),
        "avg_stay_minutes": avg_stay,
        "busiest_hour": busiest[0] if busiest else None,
        "rejection_rate": round(rejected / total, 4) if total else 0.0,
    }
    statement = insert(EventSummary).values(event_id=event_id, **values)
    await session.execute(statement.on_conflict_do_update(index_elements=[EventSummary.event_id], set_=values))
