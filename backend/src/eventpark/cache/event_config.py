"""Cache-aside for event configuration (event, pricing, zones), used by the public booking site.

    read:  GET cache:event:{id} -> hit: use it | miss: load from Postgres, SET with TTL
    write: update Postgres (commit) -> DEL cache:event:{id}

Why DELETE on write instead of writing the new value into the cache: two concurrent updates could write their values
in the opposite order to the cache than to the database, leaving a stale value that never goes away. Deleting is
order-independent: the next reader loads whatever the database says now.
TTL (60 s + jitter) bounds staleness even if an invalidation is ever missed. The jitter spreads expiries out so that
many keys don't expire in the same second and hammer Postgres together (a cache stampede).

Never used for gate decisions: those must read the source of truth (CLAUDE.md §3).
"""

import json
import random
import uuid
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.db.models import Event, EventPricing, EventZone, Space, Zone

log = structlog.get_logger()

TTL_SECONDS = 60


def key(event_id: uuid.UUID | str) -> str:
    return f"cache:event:{event_id}"


async def load_from_db(session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any] | None:
    event = await session.get(Event, event_id)
    if event is None:
        return None
    pricing = await session.get(EventPricing, event_id)
    has_spaces = exists().where(Space.zone_id == Zone.id)
    zones = (
        await session.execute(
            select(EventZone, Zone, has_spaces)
            .join(Zone, Zone.id == EventZone.zone_id)
            .where(EventZone.event_id == event_id)
            .order_by(Zone.code)
        )
    ).all()
    return {
        "id": str(event.id),
        "venue_id": str(event.venue_id),
        "name": event.name,
        "status": event.status.value,
        **{
            f: getattr(event, f).isoformat()
            for f in (
                "starts_at",
                "ends_at",
                "gates_open_at",
                "gates_close_at",
                "booking_opens_at",
                "booking_closes_at",
            )
        },
        "pricing": None
        if pricing is None
        else {
            "currency": pricing.currency,
            "drive_up_free_minutes": pricing.drive_up_free_minutes,
            "drive_up_hourly_minor": pricing.drive_up_hourly_minor,
            "drive_up_daily_cap_minor": pricing.drive_up_daily_cap_minor,
        },
        "zones": [
            {
                "event_zone_id": str(ez.id),
                "zone_code": z.code,
                "zone_name": z.name,
                "capacity": ez.capacity,
                "prebook_quota": ez.prebook_quota,
                "prebook_price_minor": ez.prebook_price_minor,
                "has_spaces": bool(spaces),
            }
            for ez, z, spaces in zones
        ],
    }


async def get_event_config(redis: Redis, session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any] | None:
    try:
        cached = await redis.get(key(event_id))
        if cached is not None:
            config: dict[str, Any] = json.loads(cached)
            return config
    except (RedisError, OSError) as exc:  # Redis down: just go to Postgres (slower, still correct)
        log.warning("cache.fallback", error=type(exc).__name__, metric="RedisFallbacks")
        return await load_from_db(session, event_id)
    config_or_none = await load_from_db(session, event_id)
    if config_or_none is not None:
        try:
            await redis.set(key(event_id), json.dumps(config_or_none), ex=TTL_SECONDS + random.randint(0, 10))  # noqa: S311
        except (RedisError, OSError):
            pass
    return config_or_none


async def invalidate(redis: Redis, event_id: uuid.UUID) -> None:
    """Call AFTER the database change committed."""
    try:
        await redis.delete(key(event_id))
    except (RedisError, OSError) as exc:
        # Can't delete: the TTL will expire it within ~70 s. Log it so a persistent problem is visible.
        log.warning("cache.invalidate_failed", event_id=str(event_id), error=type(exc).__name__)
