"""Health endpoints, used by Docker, the load balancer and ECS.

- `/healthz` (liveness): "is the process alive?" Checks nothing else, so a database outage never makes
  ECS restart healthy API tasks. Failure -> restart the container.
- `/readyz` (readiness): "can I serve requests right now?" Checks Postgres + Redis.
  Failure -> stop sending traffic here, but don't restart.
"""

import asyncio
from collections.abc import Awaitable
from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(tags=["health"])
log = structlog.get_logger()

# Each check must answer quickly. A load balancer calls /readyz every few seconds and has its own
# timeout; a check that hangs for 60 s (e.g. an unreachable host) is worse than a fast "fail".
CHECK_TIMEOUT_SECONDS = 2.0

CheckResult = Literal["ok", "fail"]


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    engine: AsyncEngine = request.app.state.engine
    redis: Redis = request.app.state.redis

    # Run both checks at the same time, so the worst case is one timeout, not two in a row.
    postgres, redis_ = await asyncio.gather(
        _check("postgres", _ping_postgres(engine)),
        _check("redis", _ping_redis(redis)),
    )
    checks = {"postgres": postgres, "redis": redis_}

    ready = all(result == "ok" for result in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ready else "fail", "checks": checks}


async def _ping_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _ping_redis(redis: Redis) -> None:
    await redis.ping()


async def _check(name: str, ping: Awaitable[None]) -> CheckResult:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await ping
    except Exception as exc:  # any failure means "not ready"; the details go to the log, not the response
        log.warning("readiness.check_failed", dependency=name, error=type(exc).__name__, detail=str(exc))
        return "fail"
    return "ok"
