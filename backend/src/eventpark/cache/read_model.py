"""Occupancy read model: `occ:event:{id}`, one Redis hash per event, one field per zone (JSON).

A *read model* is a copy of data shaped for reading (CQRS-lite): the dashboard and the booking site ask "how full is
each zone?" constantly; answering from Redis keeps that load off Postgres. It is:
- written only with ABSOLUTE values recomputed from Postgres (never `+1`): applying a refresh twice, or out of order,
  still ends with the truth, so duplicate/out-of-order messages can't corrupt it;
- rebuildable at any time from Postgres (the maintenance job does it every minute: self-healing);
- eventually consistent: it may lag a moment behind the database. Fine for a dashboard, never used for decisions.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.db.models import EventZone, Ticket, Zone
from eventpark.domain.tickets import TicketStatus, TicketType

log = structlog.get_logger()

TTL_SECONDS = 15 * 60  # forgotten if nobody refreshes it (event over); the next refresh recreates it


def key(event_id: uuid.UUID | str) -> str:
    return f"occ:event:{event_id}"


async def compute(session: AsyncSession, event_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """The snapshot, straight from Postgres (the source of truth)."""
    inside = func.count(case((Ticket.status == TicketStatus.INSIDE, 1)))
    entering = func.count(case((Ticket.status == TicketStatus.ENTERING, 1)))
    rows = (
        await session.execute(
            select(
                Zone.code,
                EventZone.capacity,
                EventZone.prebook_quota,
                EventZone.prebook_held,
                EventZone.prebook_inside,
                EventZone.drive_up_occupied,
                func.count(case(((Ticket.status == TicketStatus.INSIDE) & (Ticket.type == TicketType.DRIVE_UP), 1))),
                inside,
                entering,
            )
            .join(Zone, Zone.id == EventZone.zone_id)
            .outerjoin(Ticket, Ticket.event_zone_id == EventZone.id)
            .where(EventZone.event_id == event_id)
            .group_by(Zone.code, EventZone.id)
        )
    ).all()
    return {
        code: {
            "capacity": capacity,
            "prebook_quota": quota,
            "prebook_held": held,
            "prebook_inside": prebook_inside,
            "drive_up_capacity": capacity - quota,
            "drive_up_occupied": drive_up_occupied,
            "drive_up_inside": drive_up_inside,
            "inside": total_inside,
            "entering": total_entering,
            "available_drive_up": capacity - quota - drive_up_occupied,
            "available_prebook": quota - held,
        }
        for (
            code,
            capacity,
            quota,
            held,
            prebook_inside,
            drive_up_occupied,
            drive_up_inside,
            total_inside,
            total_entering,
        ) in rows
    }


async def refresh(redis: Redis, session: AsyncSession, event_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    snapshot = await compute(session, event_id)
    if not snapshot:
        return snapshot
    mapping: dict[Any, Any] = {zone: json.dumps(values) for zone, values in snapshot.items()}
    mapping["_updated_at"] = datetime.now(UTC).isoformat()
    try:
        async with redis.pipeline(transaction=True) as pipe:  # MULTI/EXEC: readers never see half a snapshot
            pipe.delete(key(event_id))
            pipe.hset(key(event_id), mapping=mapping)
            pipe.expire(key(event_id), TTL_SECONDS)
            await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning(
            "read_model.refresh_failed", event_id=str(event_id), error=type(exc).__name__, metric="RedisFallbacks"
        )
    return snapshot


async def read(redis: Redis, session: AsyncSession, event_id: uuid.UUID) -> tuple[dict[str, dict[str, Any]], str]:
    """Returns (snapshot, source): "redis" normally, "postgres" if Redis is down or the key is missing (rebuilt)."""
    try:
        raw = await redis.hgetall(key(event_id))
    except (RedisError, OSError) as exc:
        log.warning("read_model.fallback", error=type(exc).__name__, metric="RedisFallbacks")
        return await compute(session, event_id), "postgres"
    if not raw:
        return await refresh(redis, session, event_id), "postgres"
    return {str(zone): json.loads(value) for zone, value in raw.items() if not str(zone).startswith("_")}, "redis"
