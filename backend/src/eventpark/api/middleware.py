"""Request-ID middleware.

Every request gets an ID that appears in every log line written while handling it and in the
`X-Request-ID` response header. A gate controller can send its own ID (e.g. the scan's request_id) so
one ID follows a scan from gate -> API -> outbox -> queue -> worker (docs/02-architecture.md §15).

Written as a plain ASGI middleware (a callable wrapping the app) instead of Starlette's
`BaseHTTPMiddleware`: it adds less overhead per request on the gate hot path and doesn't run the
app in a separate task, which avoids known problems with contextvars and streaming responses.
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
# The header comes from outside, so only accept short, boring values: no spaces, quotes or
# newlines that could forge fields in a log line, and nothing long enough to flood the logs.
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

log = structlog.get_logger()


def _request_id_from(scope: Scope) -> str:
    incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
    if _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return str(uuid.uuid4())


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan (startup/shutdown) and, later, WebSocket messages pass straight through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        # Start from an empty log context, then bind the ID: everything logged until this request
        # finishes carries it. Clearing first guarantees nothing from another request leaks in.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500  # if the app crashes before sending a response, that's what the client gets
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # One summary line per request (replaces uvicorn's plain-text access log).
            # `route` is the path template (/tickets/{ticket_id}), never the raw path, so IDs or codes
            # in the URL don't end up in logs and lines group nicely per endpoint.
            route = scope.get("route")
            log.info(
                "request.finished",
                method=scope["method"],
                route=getattr(route, "path", "unmatched"),
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
