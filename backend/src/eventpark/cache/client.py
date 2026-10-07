"""The Redis (Valkey) client: one per process, with its own connection pool.

Like the DB engine, creating it doesn't connect. Short timeouts: Redis is only a cache, so when it's slow
we'd rather fail fast and fall back to Postgres (Phase 7) than make a gate wait.
"""

from redis.asyncio import Redis

from eventpark.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,  # return str instead of bytes
    )
