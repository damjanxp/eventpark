"""The Redis (Valkey) client: one per process, with its own connection pool.

Like the DB engine, creating it doesn't connect. Redis is only a cache/limiter here, so when it is slow or gone we
want to fail FAST and fall back (rate limiter fails open, cache misses go to Postgres). Two things make that fast:

1. Short timeouts (REDIS_TIMEOUT_SECONDS, default 0.25 s). A healthy Redis answers in well under 1 ms (same host
   locally, same VPC on AWS); a quarter second without an answer means it's not coming.
2. A circuit breaker (ADR-027). Without it, *every* call during an outage would wait for the timeout again: in the
   Phase 7 experiment a gate decision took 2-4 s with Redis stopped (two Redis calls x 2 s timeout). After one
   connection failure the breaker "opens" for REDIS_BREAKER_SECONDS (default 5 s): calls fail immediately with
   ConnectionError (a RedisError, which every caller already handles), so the fallback costs microseconds. When the
   time is up the next call tries Redis again ("half-open"); success closes the breaker.

    ELI5: if the shop next door was closed when you tried, you don't walk over again for every customer;
    you check again in five minutes.
"""

import time
from typing import Any

import structlog
from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from eventpark.core.config import Settings

log = structlog.get_logger()


class BreakerRedis(Redis):
    """redis.asyncio.Redis whose commands fail fast for a while after a connection failure."""

    breaker_seconds: float = 5.0
    _open_until: float = 0.0

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        if time.monotonic() < self._open_until:
            raise RedisConnectionError("circuit open: Redis failed recently, not trying again yet")
        try:
            result = await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]
        except (RedisConnectionError, RedisTimeoutError, OSError):
            if self._open_until == 0.0 or time.monotonic() >= self._open_until:
                log.warning("redis.circuit_open", seconds=self.breaker_seconds, metric="RedisFallbacks")
            self._open_until = time.monotonic() + self.breaker_seconds
            raise
        if self._open_until:
            self._open_until = 0.0
            log.info("redis.circuit_closed")
        return result


def create_redis(settings: Settings) -> BreakerRedis:
    pool = ConnectionPool.from_url(
        str(settings.redis_url),
        socket_connect_timeout=settings.redis_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
        decode_responses=True,  # return str instead of bytes
    )
    client = BreakerRedis(connection_pool=pool, auto_close_connection_pool=True)
    client.breaker_seconds = settings.redis_breaker_seconds
    return client


def create_pubsub_redis(settings: Settings) -> Redis:
    """For the realtime subscriber only: a subscription *waits* for messages, so no read timeout and no breaker
    (it has its own reconnect backoff). Its own pool, so a blocked subscription never holds a command connection.
    Not on any request path, so a relaxed connect timeout: a busy host shouldn't make it miss messages while it
    reconnects."""
    return Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=2,
        socket_timeout=None,
        decode_responses=True,
    )
