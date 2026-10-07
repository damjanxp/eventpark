import time

from httpx import AsyncClient


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_ignores_dependencies(client: AsyncClient) -> None:
    """The default test settings point at a Postgres/Redis that don't exist: liveness must not care."""
    response = await client.get("/healthz")

    assert response.status_code == 200


async def test_readyz_fails_fast_when_dependencies_are_down(client: AsyncClient) -> None:
    started = time.perf_counter()
    response = await client.get("/readyz")
    elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.json() == {"status": "fail", "checks": {"postgres": "fail", "redis": "fail"}}
    assert elapsed < 3, "checks run in parallel with a 2 s timeout each"
