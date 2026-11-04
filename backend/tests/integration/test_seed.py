"""The seed command builds the demo world and can be run again without duplicating anything."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.config import Settings, get_settings
from eventpark.db.models import Device, Event, EventZone, Gate, Space, User, Zone
from eventpark.domain.events import EventStatus
from eventpark.seed import seed

pytestmark = pytest.mark.integration


async def test_seed_is_idempotent(
    settings: Settings, db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", str(settings.database_url))
    monkeypatch.setenv("REDIS_URL", str(settings.redis_url))
    monkeypatch.setenv("JWT_SECRET", settings.jwt_secret.get_secret_value())
    monkeypatch.setenv("FAKEPAY_WEBHOOK_SECRET", settings.fakepay_webhook_secret.get_secret_value())
    get_settings.cache_clear()
    try:
        first = await seed()
        second = await seed()
    finally:
        get_settings.cache_clear()

    async with db() as session:
        counts = {
            model.__name__: await session.scalar(select(func.count()).select_from(model))
            for model in (User, Zone, Space, Gate, Device, Event, EventZone)
        }
        live = await session.get(Event, first["live_event_id"])
    assert counts == {"User": 5, "Zone": 3, "Space": 50, "Gate": 8, "Device": 8, "Event": 2, "EventZone": 6}
    assert live is not None and live.status is EventStatus.LIVE
    assert first["live_event_id"] == second["live_event_id"]
    # keys are rotated on every run (only hashes are stored, so old keys can't be shown again)
    assert first["devices"][0]["api_key"] != second["devices"][0]["api_key"]  # type: ignore[index]
