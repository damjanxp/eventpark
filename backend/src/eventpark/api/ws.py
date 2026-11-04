"""Live dashboard updates over WebSocket (docs/02-architecture.md §3.7, §7).

1. The dashboard calls POST /admin/v1/ws-ticket (normal bearer auth) and gets a one-time ticket valid for 30 s.
   Why a ticket: browsers can't set an Authorization header on a WebSocket, and putting the long-lived access token in
   the URL would leak it into proxy/access logs. The ticket is single-use and dies in 30 s.
2. It connects to WS /ws/v1/events/{id}?ticket=...; the server redeems the ticket (GETDEL: only once) and keeps the
   socket in this instance's ConnectionManager.
3. Workers PUBLISH updates to Redis; every API instance's subscriber forwards them to its own sockets (cache/pubsub.py).
4. The server pings every 20 s so idle proxies/load balancers don't cut the connection (ALB idle timeout: 60 s).
"""

import asyncio
import json
import secrets
import uuid

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from redis.exceptions import RedisError

from eventpark.api.deps import RedisDep, StaffDep
from eventpark.cache import read_model
from eventpark.cache.pubsub import ConnectionManager
from eventpark.core.errors import AppError

log = structlog.get_logger()
router = APIRouter(tags=["realtime"])

TICKET_TTL_SECONDS = 30
PING_SECONDS = 20


class WsTicket(BaseModel):
    ticket: str
    expires_in: int = TICKET_TTL_SECONDS


class RealtimeUnavailable(AppError):
    status = 503
    title = "Realtime unavailable"
    default_code = "REALTIME_UNAVAILABLE"


@router.post("/admin/v1/ws-ticket")
async def ws_ticket(principal: StaffDep, redis: RedisDep) -> WsTicket:
    ticket = secrets.token_urlsafe(24)
    try:
        await redis.set(
            f"wsticket:{ticket}",
            json.dumps({"user_id": str(principal.user_id), "role": principal.role.value}),
            ex=TICKET_TTL_SECONDS,
        )
    except (RedisError, OSError) as exc:
        # No Redis = no live updates; the dashboard falls back to polling the REST endpoints.
        raise RealtimeUnavailable("Live updates are unavailable right now; refresh periodically.") from exc
    return WsTicket(ticket=ticket)


@router.websocket("/ws/v1/events/{event_id}")
async def event_stream(websocket: WebSocket, event_id: uuid.UUID, ticket: str = Query(...)) -> None:
    redis = websocket.app.state.redis
    manager: ConnectionManager = websocket.app.state.connections
    try:
        claims = await redis.getdel(f"wsticket:{ticket}")  # redeem: a ticket works exactly once
    except (RedisError, OSError):
        claims = None
    if not claims:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid or expired ticket")
        return
    await websocket.accept()
    key = str(event_id)
    manager.add(key, websocket)
    log.info("ws.connected", event_id=key, connections=manager.count())
    try:
        # First message: the full current state, so the client never has to guess what it missed.
        async with websocket.app.state.sessionmaker() as session:
            zones, source = await read_model.read(redis, session, event_id)
        await websocket.send_text(json.dumps({"type": "occupancy", "event_id": key, "zones": zones, "source": source}))
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=PING_SECONDS)
            except TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(key, websocket)
        log.info("ws.disconnected", event_id=key, connections=manager.count())
