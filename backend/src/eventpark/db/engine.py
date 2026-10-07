"""The SQLAlchemy async engine: one per process, holding a pool of Postgres connections.

Creating the engine does NOT connect. Connections are opened on first use and then reused from the
pool, because opening a Postgres connection (TCP + TLS + auth) costs several milliseconds and the gate
hot path can't pay that on every scan. Pool sizing is tuned in Phase 14 against RDS `max_connections`.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from eventpark.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.database_url))
