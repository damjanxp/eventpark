"""Realtime fan-out with Redis pub/sub (docs/02-architecture.md §3.7, ADR-021).

    worker ──PUBLISH realtime:event:{id} {json}──▶ Redis ──▶ EVERY API instance (each subscribed)
                                                              └─▶ its own connected WebSockets for that event

With several API instances behind a load balancer, each browser is connected to ONE of them, and the worker that
produced the update doesn't know which. Pub/sub delivers every message to every subscriber, so every instance hears it
and forwards it to the sockets it holds. Fire-and-forget: a message published while an instance is reconnecting is
lost; the dashboard re-fetches the full state on reconnect (and the read model is always there to read).
"""

import asyncio
import json
import uuid
from collections import defaultdict
from typing import Any

import structlog
from fastapi import WebSocket
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger()

CHANNEL_PATTERN = "realtime:event:*"


def channel(event_id: uuid.UUID | str) -> str:
    return f"realtime:event:{event_id}"


async def publish(redis: Redis, event_id: uuid.UUID | str, message: dict[str, Any]) -> None:
    try:
        await redis.publish(channel(event_id), json.dumps(message, default=str))
    except (RedisError, OSError) as exc:
        log.warning("realtime.publish_failed", error=type(exc).__name__, metric="RedisFallbacks")


class ConnectionManager:
    """The WebSockets connected to THIS API instance, grouped by event."""

    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)

    def add(self, event_id: str, ws: WebSocket) -> None:
        self._sockets[event_id].add(ws)

    def remove(self, event_id: str, ws: WebSocket) -> None:
        self._sockets[event_id].discard(ws)
        if not self._sockets[event_id]:
            del self._sockets[event_id]

    def count(self) -> int:
        return sum(len(s) for s in self._sockets.values())

    async def broadcast(self, event_id: str, text: str) -> None:
        for ws in list(self._sockets.get(event_id, ())):
            try:
                await ws.send_text(text)
            except Exception:  # client went away mid-send: forget it
                self.remove(event_id, ws)


async def subscriber_loop(redis: Redis, manager: ConnectionManager, *, stop: asyncio.Event) -> None:
    """Runs inside every API instance (started in the app lifespan). Reconnects with backoff if Redis goes away."""
    backoff = 0.5
    while not stop.is_set():
        try:
            pubsub = redis.pubsub()
            await pubsub.psubscribe(CHANNEL_PATTERN)
            log.info("realtime.subscribed", pattern=CHANNEL_PATTERN)
            backoff = 0.5
            while not stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                event_id = str(message["channel"]).rsplit(":", 1)[-1]
                await manager.broadcast(event_id, str(message["data"]))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        except (RedisError, OSError) as exc:
            log.warning("realtime.subscriber_reconnecting", error=type(exc).__name__, in_seconds=backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 10)
