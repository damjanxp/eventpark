"""FastAPI application factory.

`create_app()` builds a fresh app each time it's called, instead of a module-level `app = FastAPI()`.
That lets tests build an app with their own settings, and nothing happens at import time.
Run with:  uvicorn eventpark.app:create_app --factory
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from eventpark.api import gate as gate_api
from eventpark.api import health, public, webhooks, ws
from eventpark.api.admin import events as admin_events
from eventpark.api.admin import files as admin_files
from eventpark.api.admin import operations as admin_operations
from eventpark.api.admin import users as admin_users
from eventpark.api.admin import venues as admin_venues
from eventpark.api.auth import admin_auth_router, public_auth_router
from eventpark.api.errors import install_error_handlers
from eventpark.api.middleware import RequestIdMiddleware
from eventpark.cache.client import create_pubsub_redis, create_redis
from eventpark.cache.pubsub import ConnectionManager, subscriber_loop
from eventpark.cache.rate_limit import RateLimiter
from eventpark.core.config import Settings, get_settings
from eventpark.core.logging import configure_logging
from eventpark.db.engine import create_engine
from eventpark.db.session import create_sessionmaker
from eventpark.services.provider import PaymentProvider
from eventpark.storage.s3 import Storage


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
        app.state.limiter = RateLimiter(app.state.redis)
        app.state.connections = ConnectionManager()
        # Every instance subscribes to realtime updates and forwards them to its own WebSockets (cache/pubsub.py).
        stop_subscriber = asyncio.Event()
        app.state.pubsub_redis = create_pubsub_redis(settings)
        subscriber = asyncio.create_task(
            subscriber_loop(app.state.pubsub_redis, app.state.connections, stop=stop_subscriber)
        )
        app.state.storage = Storage(settings)
        app.state.provider = PaymentProvider(settings.fakepay_url, terminal_timeout=settings.terminal_timeout_seconds)
        log.info("app.started")
        yield
        # Shutdown: stop the subscriber, close the pools so Postgres/Redis don't keep half-open connections.
        stop_subscriber.set()
        subscriber.cancel()  # don't wait for its 1 s poll to time out: shut down promptly
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber
        await app.state.provider.aclose()
        await app.state.redis.aclose()
        await app.state.pubsub_redis.aclose()
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
    app.include_router(admin_users.router)
    app.include_router(admin_venues.router)
    app.include_router(admin_events.router)
    app.include_router(admin_operations.router)
    app.include_router(admin_files.router)
    app.include_router(gate_api.router)
    app.include_router(public.router)
    app.include_router(webhooks.router)
    app.include_router(ws.router)
    return app
