"""/readyz against a real Postgres and a real Valkey, started by testcontainers.

testcontainers starts throwaway Docker containers for the test run (on random free ports) and removes
them afterwards, so tests never depend on whatever happens to be running on your machine.
"""

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from eventpark.core.config import Settings

pytestmark = pytest.mark.integration


# scope="module": start the containers once for all tests in this file, not once per test (~seconds each).
@pytest.fixture(scope="module")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:17.11", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def valkey() -> Iterator[RedisContainer]:
    with RedisContainer("valkey/valkey:8.1.10-alpine") as container:
        yield container


def _redis_url(container: RedisContainer) -> str:
    return f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture
def settings(request: pytest.FixtureRequest, postgres: PostgresContainer, valkey: RedisContainer) -> Settings:
    """Overrides conftest's settings: real containers, unless a test asks for Redis to be "down"."""
    redis_down = request.node.get_closest_marker("redis_down") is not None
    return Settings(
        database_url=postgres.get_connection_url(),
        redis_url="redis://127.0.0.1:1/0" if redis_down else _redis_url(valkey),
        jwt_secret="test-jwt-secret-0123456789abcdef0123456789",
        fakepay_webhook_secret="whsec_test_0123456789",
        log_format="json",
    )


async def test_ready_when_postgres_and_redis_are_up(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


@pytest.mark.redis_down
async def test_not_ready_when_only_redis_is_down(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "fail", "checks": {"postgres": "ok", "redis": "fail"}}
