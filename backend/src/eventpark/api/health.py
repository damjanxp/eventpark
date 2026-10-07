"""Health endpoints, used by Docker, the load balancer and ECS.

- `/healthz` (liveness): "is the process alive?" Checks nothing else, so a database outage never makes
  ECS restart healthy API tasks. Failure -> restart the container.
- `/readyz` (readiness): "can I serve requests right now?" Checks Postgres + Redis (added in step 3).
  Failure -> stop sending traffic here, but don't restart.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
