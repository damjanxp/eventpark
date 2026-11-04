"""Turns every error into an RFC 9457 problem+json response, in one place (CLAUDE.md §8)."""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from eventpark.core.errors import AppError, RateLimited, Unauthorized

PROBLEM_JSON = "application/problem+json"
log = structlog.get_logger()


def problem(*, status: int, title: str, code: str, detail: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    body: dict[str, Any] = {
        "type": f"https://eventpark.dev/problems/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, Unauthorized) else None
        if isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after)}  # standard header: when to try again
        if exc.status >= 500:
            log.error("request.app_error", code=exc.code, detail=exc.detail)
        return JSONResponse(
            problem(status=exc.status, title=exc.title, code=exc.code, detail=exc.detail, extra=exc.extra),
            status_code=exc.status,
            media_type=PROBLEM_JSON,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Keep FastAPI's per-field list (where + what), drop the echoed input (it may contain a password).
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(
            problem(
                status=422,
                title="Validation failed",
                code="VALIDATION_FAILED",
                detail="The request body or parameters are invalid.",
                extra={"errors": errors},
            ),
            status_code=422,
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Framework errors such as 404 for an unknown route or 405 for a wrong method.
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            problem(status=exc.status_code, title=str(exc.detail), code=code, detail=str(exc.detail)),
            status_code=exc.status_code,
            media_type=PROBLEM_JSON,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        # A bug. Log it with the stack trace; never send internals (SQL, paths) to the client.
        log.exception("request.unhandled_error", error=type(exc).__name__)
        return JSONResponse(
            problem(
                status=500,
                title="Internal server error",
                code="INTERNAL_ERROR",
                detail="Something went wrong on our side. Quote the request_id if you report it.",
            ),
            status_code=500,
            media_type=PROBLEM_JSON,
        )
