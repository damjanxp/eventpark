"""The Redis circuit breaker (cache/client.py, ADR-027). No Docker needed: Redis is 'down' on purpose.

10.255.255.1 is a non-routable address: packets go nowhere, so connecting *hangs* until the timeout, which is what a
stopped container / dead ElastiCache node looks like (a closed local port fails instantly and would hide the problem).
"""

import asyncio
import time

import pytest
from redis.exceptions import RedisError

from eventpark.cache.client import create_redis
from eventpark.core.config import Settings

BLACKHOLE = "redis://10.255.255.1:6379/0"


def _settings(base: Settings, breaker: float) -> Settings:
    return base.model_copy(
        update={"redis_url": BLACKHOLE, "redis_timeout_seconds": 0.2, "redis_breaker_seconds": breaker}
    )


async def _timed_ping(redis: object) -> float:
    start = time.perf_counter()
    with pytest.raises(RedisError):
        await redis.ping()  # type: ignore[attr-defined]
    return time.perf_counter() - start


async def test_after_one_failure_calls_fail_fast(settings: Settings) -> None:
    redis = create_redis(_settings(settings, breaker=5))
    first = await _timed_ping(redis)
    following = [await _timed_ping(redis) for _ in range(10)]
    assert first >= 0.15  # paid the timeout once
    assert max(following) < 0.01  # then: no network at all
    await redis.aclose()


async def test_breaker_tries_again_after_the_cooldown(settings: Settings) -> None:
    redis = create_redis(_settings(settings, breaker=0.3))
    await _timed_ping(redis)
    assert await _timed_ping(redis) < 0.01
    await asyncio.sleep(0.35)
    assert await _timed_ping(redis) >= 0.15  # half-open: one real attempt
    await redis.aclose()
