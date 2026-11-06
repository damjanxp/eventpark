"""Phase 7: rate limiting, cache-aside, occupancy read model, realtime WebSockets across instances, Redis down."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect, WebSocketUpgradeError, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.app import create_app
from eventpark.cache import event_config, pubsub, read_model
from eventpark.cache.rate_limit import Limit, RateLimiter
from eventpark.core.config import Settings
from eventpark.domain.events import EventStatus
from eventpark.domain.roles import Role

from .conftest import HeadersFactory, UserFactory
from .gate_world import build_world

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(str(settings.redis_url), decode_responses=True)
    yield client
    await client.aclose()


def _down(settings: Settings) -> Settings:
    """Same settings, but Redis points at a closed port: 'Redis is down'."""
    return settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})


async def _client_for(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGIWebSocketTransport(manager.app), base_url="https://test") as c,
    ):
        yield c


# --- rate limiting ---


async def test_token_bucket_allows_a_burst_then_refills(redis: Redis) -> None:
    limiter, limit = RateLimiter(redis), Limit("test", capacity=3, per_seconds=1.5)  # refills 2 tokens/s

    burst = [(await limiter.hit(limit, "k")).allowed for _ in range(4)]
    await asyncio.sleep(0.6)  # ~1.2 tokens back
    later = (await limiter.hit(limit, "k")).allowed

    assert burst == [True, True, True, False]
    assert later is True


async def test_concurrent_hits_never_exceed_the_bucket(redis: Redis) -> None:
    """The Lua script is atomic: 100 simultaneous hits on a bucket of 10 -> exactly 10 allowed."""
    limiter, limit = RateLimiter(redis), Limit("race", capacity=10, per_seconds=3600)

    results = await asyncio.gather(*(limiter.hit(limit, "same") for _ in range(100)))

    assert sum(r.allowed for r in results) == 10


async def test_login_is_rate_limited_per_email_with_retry_after(client: AsyncClient, make_user: UserFactory) -> None:
    user = await make_user(Role.ADMIN)
    attempts = [
        await client.post("/admin/v1/auth/login", json={"email": user.email, "password": "wrong"}) for _ in range(6)
    ]
    other_account = await client.post("/admin/v1/auth/login", json={"email": "other@example.com", "password": "x"})

    assert [r.status_code for r in attempts] == [401] * 5 + [429]
    assert int(attempts[-1].headers["retry-after"]) >= 1
    assert attempts[-1].json()["code"] == "RATE_LIMITED"
    assert other_account.status_code == 401  # a different bucket


async def test_rate_limiter_fails_open_and_gates_work_without_redis(
    db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Redis is down: gates must still decide (from Postgres); the limiter lets requests through and logs it."""
    world = await build_world(db)
    async for client in _client_for(create_app(_down(settings))):
        issued = await client.post(
            "/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1")
        )
        ticket = await client.post("/admin/v1/ws-ticket", headers={})

    assert issued.status_code == 200 and issued.json()["decision"] == "OPEN"
    assert ticket.status_code == 401  # (no token: auth comes first; with a token it would be 503 REALTIME_UNAVAILABLE)


async def test_gates_stay_fast_when_redis_hangs(db: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """Redis unreachable (connect hangs, like a stopped container): the breaker keeps gate decisions fast.

    Before ADR-027 each decision waited for two 2 s timeouts (experiment: 2-4 s per car).
    """
    world = await build_world(db)
    hanging = settings.model_copy(
        update={"redis_url": "redis://10.255.255.1:6379/0", "redis_timeout_seconds": 0.2, "redis_breaker_seconds": 30}
    )
    timings = []
    async for client in _client_for(create_app(hanging)):
        for _ in range(5):
            start = asyncio.get_running_loop().time()
            issued = await client.post(
                "/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1")
            )
            timings.append(asyncio.get_running_loop().time() - start)
            assert issued.status_code == 200 and issued.json()["decision"] == "OPEN"
    assert max(timings[1:]) < 0.15, timings  # only the very first request may pay the timeout


# --- cache-aside ---


async def test_event_config_is_cached_and_invalidated_on_update(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], redis: Redis, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db, status=EventStatus.PUBLISHED)  # still editable (a LIVE event isn't)
    manager = await auth_headers(Role.EVENT_MANAGER)

    first = (await client.get(f"/public/v1/events/{world.event_id}")).json()
    assert await redis.exists(event_config.key(world.event_id))  # cached by the read
    await client.patch(f"/admin/v1/events/{world.event_id}", json={"name": "Renamed concert"}, headers=manager)
    gone = await redis.exists(event_config.key(world.event_id))
    second = (await client.get(f"/public/v1/events/{world.event_id}")).json()

    assert first["name"] == "Test event"
    assert gone == 0  # DEL after the commit
    assert second["name"] == "Renamed concert"  # the next reader loaded the new version


async def test_public_event_page_works_without_redis(db: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    world = await build_world(db)
    async for client in _client_for(create_app(_down(settings))):
        response = await client.get(f"/public/v1/events/{world.event_id}")

    assert response.status_code == 200 and response.json()["zones"]


# --- read model ---


async def test_read_model_is_rebuilt_after_a_flush(
    client: AsyncClient, db: async_sessionmaker[AsyncSession], redis: Redis, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    issued = (
        await client.post("/gate/v1/entry/issue", json={"request_id": str(uuid.uuid4())}, headers=world.headers("N1"))
    ).json()
    await client.post(f"/gate/v1/passages/{issued['passage_id']}/complete", headers=world.headers("N1"))
    staff = await auth_headers(Role.PARKING_STAFF)
    async with db() as session:
        await read_model.refresh(redis, session, world.event_id)

    cached = (await client.get(f"/admin/v1/events/{world.event_id}/occupancy", headers=staff)).json()
    await redis.flushdb()  # experiment: flush Redis during traffic
    rebuilt = (await client.get(f"/admin/v1/events/{world.event_id}/occupancy", headers=staff)).json()
    again = (await client.get(f"/admin/v1/events/{world.event_id}/occupancy", headers=staff)).json()

    assert cached["source"] == "redis" and cached["zones"]["A"]["inside"] == 1
    assert rebuilt["source"] == "postgres" and rebuilt["zones"] == cached["zones"]  # same truth, from the source
    assert again["source"] == "redis"  # and cached again


# --- realtime over WebSockets ---


async def _ticket(client: AsyncClient, headers: dict[str, Any]) -> str:
    return str((await client.post("/admin/v1/ws-ticket", headers=headers)).json()["ticket"])


async def test_websocket_needs_a_valid_one_time_ticket(
    db: async_sessionmaker[AsyncSession], settings: Settings, auth_headers: HeadersFactory
) -> None:
    world = await build_world(db)
    staff = await auth_headers(Role.PARKING_STAFF)
    async for client in _client_for(create_app(settings)):
        # Refused handshake: a real server answers HTTP 403; the in-memory ASGI transport reports a close (1008).
        with pytest.raises((WebSocketUpgradeError, WebSocketDisconnect)):
            async with aconnect_ws(f"/ws/v1/events/{world.event_id}?ticket=forged", client):
                pass
        ticket = await _ticket(client, staff)
        async with aconnect_ws(f"/ws/v1/events/{world.event_id}?ticket={ticket}", client) as ws:
            hello = json.loads(await ws.receive_text())
        with pytest.raises((WebSocketUpgradeError, WebSocketDisconnect)):  # second use of the same ticket
            async with aconnect_ws(f"/ws/v1/events/{world.event_id}?ticket={ticket}", client):
                pass

    assert hello["type"] == "occupancy" and "A" in hello["zones"]


async def test_update_reaches_sockets_on_every_api_instance(
    db: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis, auth_headers: HeadersFactory
) -> None:
    """Two API instances (A and B), a dashboard on each. A worker publishes once -> both dashboards get it."""
    world = await build_world(db)
    staff = await auth_headers(Role.PARKING_STAFF)
    received: dict[str, dict[str, Any]] = {}

    async def dashboard(name: str, ready: asyncio.Event) -> None:
        async for client in _client_for(create_app(settings)):
            ticket = await _ticket(client, staff)
            async with aconnect_ws(f"/ws/v1/events/{world.event_id}?ticket={ticket}", client) as ws:
                await ws.receive_text()  # initial snapshot
                ready.set()
                while True:
                    message = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
                    if message["type"] == "passage":
                        received[name] = message
                        return

    ready_a, ready_b = asyncio.Event(), asyncio.Event()
    tasks = [asyncio.create_task(dashboard("A", ready_a)), asyncio.create_task(dashboard("B", ready_b))]
    await asyncio.wait_for(asyncio.gather(ready_a.wait(), ready_b.wait()), timeout=10)
    # Pub/sub is fire-and-forget: publish only once both instances' subscribers are really registered in Redis.
    for _ in range(100):
        clients = await redis.client_list()
        if sum(1 for c in clients if c.get("psub") == "1") >= 2:  # (PUBSUB NUMPAT counts patterns, not clients)
            break
        await asyncio.sleep(0.05)

    await pubsub.publish(redis, world.event_id, {"type": "passage", "event": "passage.completed", "gate_code": "N1"})
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    assert received["A"]["gate_code"] == received["B"]["gate_code"] == "N1"


async def test_ws_ticket_is_503_without_redis(
    db: async_sessionmaker[AsyncSession], settings: Settings, auth_headers: HeadersFactory
) -> None:
    staff = await auth_headers(Role.PARKING_STAFF)
    async for client in _client_for(create_app(_down(settings))):
        response = await client.post("/admin/v1/ws-ticket", headers=staff)

    assert response.status_code == 503 and response.json()["code"] == "REALTIME_UNAVAILABLE"


async def test_gate_lane_flooding_is_limited(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    world = await build_world(db, zones={"A": (500, 0, 1)}, status=EventStatus.LIVE)

    responses = await asyncio.gather(
        *(client.post("/gate/v1/heartbeat", json={}, headers=world.headers("N1")) for _ in range(150))
    )
    statuses = [r.status_code for r in responses]

    # A bucket of 40 that refills 20/s: a burst of 150 at once gets ~40 + refill through, the rest 429.
    assert statuses.count(429) >= 50 and set(statuses) == {200, 429}
    limited = next(r for r in responses if r.status_code == 429)
    assert limited.headers["retry-after"] == "1"
