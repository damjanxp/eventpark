"""Migrations against a real, throwaway Postgres.

- upgrade -> downgrade -> upgrade: every migration can be applied and undone (and re-applied).
- `alembic check`: the models in db/models.py and the migrations describe the same schema. If someone
  changes a model and forgets the migration, this test fails in CI.
- The database itself enforces our rules (unique case-insensitive email, valid role), not just Python.

These tests are plain (non-async) functions on purpose: Alembic's env.py starts its own event loop with
asyncio.run(), which isn't allowed inside an already running one (async tests run inside one).
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from eventpark.core.config import get_settings

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17.11", driver="asyncpg") as postgres, pytest.MonkeyPatch.context() as mp:
        url = postgres.get_connection_url()
        # env.py reads settings from the environment, exactly like in production.
        mp.setenv("DATABASE_URL", url)
        mp.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # required by Settings, unused by migrations
        mp.setenv("JWT_SECRET", "test-jwt-secret-0123456789abcdef0123456789")
        mp.setenv("LOG_FORMAT", "json")
        get_settings.cache_clear()
        yield url
    get_settings.cache_clear()


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    return Config(str(ALEMBIC_INI))


def _query(database_url: str, sql: str) -> list[tuple[object, ...]]:
    async def run() -> list[tuple[object, ...]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(sa.text(sql))
                return [tuple(row) for row in result.all()] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _tables(database_url: str) -> set[str]:
    rows = _query(database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {str(row[0]) for row in rows}


def test_upgrade_downgrade_upgrade(alembic_config: Config, database_url: str) -> None:
    command.upgrade(alembic_config, "head")
    assert "users" in _tables(database_url)

    command.downgrade(alembic_config, "base")
    assert "users" not in _tables(database_url)

    command.upgrade(alembic_config, "head")
    assert "users" in _tables(database_url)


def test_models_match_migrations(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    command.check(alembic_config)  # raises if autogenerate would produce a new migration


INSERT_USER = """
    INSERT INTO users (id, email, password_hash, full_name, role)
    VALUES (gen_random_uuid(), '{email}', 'x', 'Test User', '{role}')
"""


def test_email_is_unique_ignoring_case(alembic_config: Config, database_url: str) -> None:
    command.upgrade(alembic_config, "head")
    _query(database_url, "DELETE FROM users")
    _query(database_url, INSERT_USER.format(email="Damjan@Example.com", role="ADMIN"))

    with pytest.raises(IntegrityError, match="uq_users_email"):
        _query(database_url, INSERT_USER.format(email="damjan@example.com", role="DRIVER"))


def test_unknown_role_is_rejected_by_the_database(alembic_config: Config, database_url: str) -> None:
    command.upgrade(alembic_config, "head")

    with pytest.raises(IntegrityError, match="ck_users_role"):
        _query(database_url, INSERT_USER.format(email="someone@example.com", role="SUPERUSER"))
