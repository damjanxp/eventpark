"""Phase 9 experiment: admission in Redis (atomic Lua) vs Postgres (conditional UPDATE). docs/experiments/local-*.md

    uv run pytest -m experiment -s tests/integration/test_experiment_admission.py

500 concurrent "may this car have a place?" requests for 100 free places, 50 in flight at a time:
  A. Postgres: UPDATE event_zones SET drive_up_occupied = drive_up_occupied + 1
               WHERE id = :zone AND drive_up_occupied < capacity - prebook_quota RETURNING ...   (one transaction each)
  B. Redis:    a Lua script: if GET counter < limit then INCR (atomic: Redis runs one script at a time)
  C. Redis, then FLUSHALL halfway: the counter is forgotten and the script admits again -> oversold
Both A and B are *correct* under concurrency (exactly 100). The question is what you give up for B's speed.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eventpark.core.config import Settings

from .gate_world import build_world

pytestmark = [pytest.mark.integration, pytest.mark.experiment]

REQUESTS, PLACES, IN_FLIGHT = 500, 100, 50

ADMIT = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used < tonumber(ARGV[1]) then
  return redis.call('INCR', KEYS[1])
end
return 0
"""


@dataclass
class Result:
    variant: str
    admitted: int
    p50_ms: float
    p95_ms: float
    total_s: float


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * p / 100) - 1)]


async def _run(variant: str, attempt: Callable[[], Awaitable[object]]) -> Result:
    semaphore = asyncio.Semaphore(IN_FLIGHT)
    timings: list[float] = []
    admitted = 0

    async def one() -> None:
        nonlocal admitted
        async with semaphore:
            started = time.perf_counter()
            ok = await attempt()
            timings.append((time.perf_counter() - started) * 1000)
            admitted += bool(ok)

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(REQUESTS)))
    return Result(variant, admitted, _pct(timings, 50), _pct(timings, 95), time.perf_counter() - started)


async def test_admission_postgres_vs_redis(db: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    world = await build_world(db, zones={"A": (PLACES, 0, 1)})
    zone_id = world.zone_ids["A"]
    engine = create_async_engine(str(settings.database_url), pool_size=IN_FLIGHT, max_overflow=0)
    sessions = async_sessionmaker(engine)
    redis = Redis.from_url(str(settings.redis_url))
    admit = redis.register_script(ADMIT)

    async def postgres() -> object:
        async with sessions() as session, session.begin():
            return (
                await session.execute(
                    text(
                        "UPDATE event_zones SET drive_up_occupied = drive_up_occupied + 1 "
                        "WHERE id = :id AND drive_up_occupied < capacity - prebook_quota RETURNING drive_up_occupied"
                    ),
                    {"id": zone_id},
                )
            ).first()

    async def redis_lua() -> object:
        return await admit(keys=["exp:admit"], args=[PLACES])

    flushed = False
    calls = 0

    async def redis_with_flush() -> object:
        nonlocal flushed, calls
        calls += 1
        if calls == REQUESTS // 2 and not flushed:
            flushed = True
            await redis.flushall()  # Redis restarted without persistence / evicted the key / someone ran FLUSHALL
        return await admit(keys=["exp:admit2"], args=[PLACES])

    results = [
        await _run("A  Postgres conditional UPDATE", postgres),
        await _run("B  Redis Lua script", redis_lua),
        await _run("C  Redis Lua, FLUSHALL halfway", redis_with_flush),
    ]
    await redis.aclose()
    await engine.dispose()

    print(f"\n{REQUESTS} requests for {PLACES} places, {IN_FLIGHT} in flight")
    print(f"{'variant':34} {'admitted':>8} {'p50 ms':>8} {'p95 ms':>8} {'total s':>8}")
    for r in results:
        print(f"{r.variant:34} {r.admitted:>8} {r.p50_ms:>8.2f} {r.p95_ms:>8.2f} {r.total_s:>8.2f}")

    assert results[0].admitted == PLACES  # Postgres: exactly the capacity
    assert results[1].admitted == PLACES  # Redis: also exact while Redis keeps its data...
    assert results[2].admitted > PLACES  # ...but a lost counter means places are sold twice
