"""Shared test fixtures.

pytest finds fixtures here automatically. A test asks for one by naming it as a parameter
(`async def test_x(client): ...`), and a test module can override a fixture by defining one with the same name.
"""

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from eventpark.app import create_app
from eventpark.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings pointing at a Postgres and Redis that don't exist (port 1 refuses connections at once).

    Most tests never touch the database. Tests that need real ones override this fixture
    (see tests/integration/).
    """
    return Settings(
        database_url="postgresql+asyncpg://eventpark:unused@127.0.0.1:1/eventpark",
        redis_url="redis://127.0.0.1:1/0",
        jwt_secret="test-jwt-secret-0123456789abcdef0123456789",
        log_format="json",
        log_level="INFO",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client that talks to the app in memory (no real network, no uvicorn).

    LifespanManager runs the app's startup/shutdown code, which ASGITransport alone skips.
    https:// so the client keeps and sends `Secure` cookies (the refresh token), like a browser would.
    """
    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="https://test") as client,
    ):
        yield client
