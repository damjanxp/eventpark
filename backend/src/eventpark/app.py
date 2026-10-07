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
from eventpark.core.config import Settings, get_settings
from eventpark.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    log = structlog.get_logger()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup: later this opens the DB engine and Redis pool.
        log.info("app.started")
        yield
        # Shutdown: later this closes them, so connections aren't left hanging.
        log.info("app.stopped")

    app = FastAPI(title="EventPark API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    return app
