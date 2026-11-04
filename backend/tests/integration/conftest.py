"""Shared fixtures for integration tests: one real Postgres + Valkey for the whole test session.

Starting containers takes seconds, so they start once (scope="session"), the schema is migrated once with Alembic
(the same migrations production uses), and each test starts from empty tables (TRUNCATE, ~10 ms).
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from eventpark.core.config import Settings, get_settings
from eventpark.core.security import create_access_token, hash_password
from eventpark.db.models import User
from eventpark.domain.roles import Role

TEST_JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789"
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


@pytest.fixture(scope="session")
def shared_postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:17.11", driver="asyncpg") as container:
        url = container.get_connection_url()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_URL", url)
            mp.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
            mp.setenv("JWT_SECRET", TEST_JWT_SECRET)
            mp.setenv("LOG_FORMAT", "json")
            get_settings.cache_clear()
            command.upgrade(Config(str(ALEMBIC_INI)), "head")
        get_settings.cache_clear()
        yield container


@pytest.fixture(scope="session")
def shared_valkey() -> Iterator[RedisContainer]:
    with RedisContainer("valkey/valkey:8.1.10-alpine") as container:
        yield container


async def _truncate_all(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            tables = (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
            ).scalars()
            names = ", ".join(f'"{t}"' for t in tables)
            if names:
                await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
async def settings(shared_postgres: PostgresContainer, shared_valkey: RedisContainer) -> Settings:
    """Overrides tests/conftest.py: real database and Redis, emptied before each test."""
    url = shared_postgres.get_connection_url()
    await _truncate_all(url)
    host, port = shared_valkey.get_container_host_ip(), shared_valkey.get_exposed_port(6379)
    return Settings(
        database_url=url,  # type: ignore[arg-type]
        redis_url=f"redis://{host}:{port}/0",  # type: ignore[arg-type]
        jwt_secret=TEST_JWT_SECRET,  # type: ignore[arg-type]
        log_format="json",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker for arranging data / checking results directly in the database."""
    engine = create_async_engine(str(settings.database_url))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


UserFactory = Callable[..., Awaitable[User]]


@pytest.fixture
def make_user(db: async_sessionmaker[AsyncSession]) -> UserFactory:
    async def factory(
        role: Role = Role.ADMIN,
        *,
        email: str | None = None,
        password: str = "correct-horse-battery",
        is_active: bool = True,
    ) -> User:
        user = User(
            email=email or f"{role.value.lower()}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(password),
            full_name=f"Test {role.value.title()}",
            role=role,
            is_active=is_active,
        )
        async with db() as session, session.begin():
            session.add(user)
        return user

    return factory


HeadersFactory = Callable[[Role], Awaitable[dict[str, Any]]]


@pytest.fixture
def auth_headers(make_user: UserFactory, settings: Settings) -> HeadersFactory:
    """`headers = await auth_headers(Role.ADMIN)`: a fresh user with that role and a valid bearer token."""

    async def factory(role: Role) -> dict[str, Any]:
        user = await make_user(role)
        token = create_access_token(
            user_id=user.id,
            role=role,
            secret=settings.jwt_secret.get_secret_value(),
            ttl=settings.access_token_ttl,
            now=datetime.now(UTC),
        )
        return {"Authorization": f"Bearer {token}"}

    return factory
