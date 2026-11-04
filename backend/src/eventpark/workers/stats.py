"""worker-entry / worker-exit: turn passage and payment messages into `hourly_stats` rows.

Hours are bucketed in the VENUE's time zone (docs/01 §10): "entries between 18:00 and 19:00" means Belgrade time,
even though everything is stored in UTC. The upsert adds to the row for (event, gate, hour). The `+ 1` here is safe:
processed_messages (same transaction) guarantees each message is counted exactly once.
"""

import uuid
from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.db.models import Event, HourlyStat, Venue
from eventpark.messaging.schemas import Envelope
from eventpark.workers.consumer import Handler

COUNTERS = (
    "entries",
    "exits",
    "rejections",
    "revenue_minor",
    "released_unpaid_minor",
    "decision_ms_sum",
    "decision_count",
)


def hour_bucket(moment: datetime, tz: str) -> datetime:
    """The start of the venue-local hour containing `moment`, as an aware datetime (stored as UTC)."""
    local = moment.astimezone(ZoneInfo(tz))
    return local.replace(minute=0, second=0, microsecond=0)


async def add_to_hourly_stats(
    session: AsyncSession, *, event_id: uuid.UUID, gate_id: uuid.UUID, hour_start: datetime, **increments: int
) -> None:
    values = {c: increments.get(c, 0) for c in COUNTERS}
    statement = insert(HourlyStat).values(event_id=event_id, gate_id=gate_id, hour_start=hour_start, **values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[HourlyStat.event_id, HourlyStat.gate_id, HourlyStat.hour_start],
            set_={c: getattr(HourlyStat, c) + statement.excluded[c] for c in COUNTERS if values[c]},
        )
        if any(values.values())
        else statement.on_conflict_do_nothing()
    )


class StatsHandler(Handler):
    name: ClassVar[str] = "stats"
    _tz_cache: ClassVar[dict[uuid.UUID, str]] = {}

    async def _venue_tz(self, session: AsyncSession, event_id: uuid.UUID) -> str:
        if event_id not in self._tz_cache:
            tz = await session.scalar(
                select(Venue.timezone).join(Event, Event.venue_id == Venue.id).where(Event.id == event_id)
            )
            self._tz_cache[event_id] = tz or "UTC"
        return self._tz_cache[event_id]

    def wants(self, msg: Envelope) -> bool:
        return msg.event_id is not None and msg.data.get("gate_id") is not None

    async def apply(self, session: AsyncSession, msg: Envelope) -> None:
        if msg.event_id is None:  # pragma: no cover (wants() filters these out)
            return
        data = msg.data
        gate_id = uuid.UUID(str(data["gate_id"]))
        tz = await self._venue_tz(session, msg.event_id)
        increments: dict[str, int] = {}
        moment = msg.occurred_at
        if msg.type == "passage.completed":
            increments["entries" if data.get("direction") == "ENTRY" else "exits"] = 1
            moment = datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else moment
        elif msg.type == "passage.rejected":
            increments["rejections"] = 1
        elif msg.type == "passage.authorized" and data.get("decision_ms") is not None:
            increments["decision_ms_sum"], increments["decision_count"] = int(data["decision_ms"]), 1
        elif msg.type == "payment.succeeded":
            increments["revenue_minor"] = int(data["amount_minor"])
        elif msg.type == "passage.released_unpaid":
            increments["released_unpaid_minor"] = int(data.get("amount_due_minor") or 0)
        if increments:
            await add_to_hourly_stats(
                session, event_id=msg.event_id, gate_id=gate_id, hour_start=hour_bucket(moment, tz), **increments
            )


class EntryStats(StatsHandler):
    name: ClassVar[str] = "worker-entry"
    queue: ClassVar[str] = "entry-events"


class ExitStats(StatsHandler):
    name: ClassVar[str] = "worker-exit"
    queue: ClassVar[str] = "exit-events"
