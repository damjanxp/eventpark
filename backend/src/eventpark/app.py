"""FastAPI application factory.

`create_app()` builds a fresh app each time it's called, instead of a module-level `app = FastAPI()`.
That lets tests build an app with their own settings, and nothing happens at import time.
Run with:  uvicorn eventpark.app:create_app --factory
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from eventpark.api import health
from eventpark.api.auth import admin_auth_router, public_auth_router
from eventpark.api.errors import install_error_handlers
from eventpark.api.middleware import RequestIdMiddleware
from eventpark.cache.client import create_redis
from eventpark.core.config import Settings, get_settings
from eventpark.core.logging import configure_logging
from eventpark.db.engine import create_engine
from eventpark.db.session import create_sessionmaker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    log = structlog.get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: create the connection pools once per process (they connect lazily, on first use,
        # so the API starts even if Postgres is still booting; /readyz reports that).
        app.state.engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        app.state.redis = create_redis(settings)
        log.info("app.started")
        yield
        # Shutdown: close the pools so Postgres/Redis don't keep half-open connections.
        await app.state.redis.aclose()
        await app.state.engine.dispose()
        log.info("app.stopped")

    app = FastAPI(
        title="EventPark API",
        version="0.1.0",
        description="Event parking platform: admin, public booking, gate devices. Errors are RFC 9457 problem+json.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(admin_auth_router)
    app.include_router(public_auth_router)
    return app
