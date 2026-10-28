"""Alembic runtime configuration: how migrations connect to the database.

Runs whenever you call `alembic ...`. It reads DATABASE_URL through our Settings (same as the app) and
compares against our SQLAlchemy models (`Base.metadata`) when you use `--autogenerate`.
"""

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import eventpark.db.models  # noqa: F401  (importing registers every table on Base.metadata)
from eventpark.core.config import get_settings
from eventpark.core.logging import configure_logging
from eventpark.db.base import Base

settings = get_settings()
configure_logging(settings)

target_metadata = Base.metadata

# A migration that waits for a lock held by live traffic (e.g. a gate transaction on `passages`) would itself
# block every new query behind it. Better to fail after a few seconds and retry when it's quiet.
LOCK_TIMEOUT = "5s"


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it (useful for reviewing)."""
    context.configure(
        url=str(settings.database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    # Postgres can change the schema inside a transaction: if a migration fails halfway, all of it is rolled back.
    with context.begin_transaction():
        # SET LOCAL = only for this transaction. It must run *inside* Alembic's transaction: running anything on
        # the connection before begin_transaction() opens a transaction Alembic then thinks it doesn't own, so it
        # never commits and every migration is silently rolled back (we hit exactly that).
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        context.run_migrations()


async def run_async_migrations() -> None:
    # NullPool: a migration is a one-off process, no point keeping connections around.
    engine = create_async_engine(str(settings.database_url), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
