"""The SQLAlchemy async engine: one per process, holding a pool of Postgres connections.

Creating the engine does NOT connect. Connections are opened on first use and then reused from the
pool, because opening a Postgres connection (TCP + TLS + auth) costs several milliseconds and the gate
hot path can't pay that on every scan. Pool sizing is tuned in Phase 14 against RDS `max_connections`.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from eventpark.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # A request that finds the pool empty WAITS (up to pool_timeout) for a connection: under a burst, too small a
    # pool turns into queueing that no CPU or index can fix (Phase 9). Too big a pool, times the number of tasks,
    # exceeds the database's max_connections. The budget is per process: total = processes x (size + overflow).
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,  # a connection the DB closed (restart, failover) is replaced instead of failing a request
    )
