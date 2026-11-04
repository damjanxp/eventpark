"""Application errors.

Services and domain code raise these; they know nothing about HTTP. One handler (api/errors.py) turns each into an
RFC 9457 "problem details" response: {"type", "title", "status", "detail", "code", "request_id"}.
`code` is machine-readable and stable (clients and tests match on it); `detail` is for humans.
"""

from typing import Any, ClassVar


class AppError(Exception):
    status: ClassVar[int] = 400
    title: ClassVar[str] = "Bad request"
    default_code: ClassVar[str] = "BAD_REQUEST"

    def __init__(self, detail: str, *, code: str | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code or self.default_code
        self.extra = extra or {}


class ValidationFailed(AppError):
    status = 422
    title = "Validation failed"
    default_code = "VALIDATION_FAILED"


class Unauthorized(AppError):
    """401: we don't know who you are (no/invalid/expired credentials)."""

    status = 401
    title = "Unauthorized"
    default_code = "UNAUTHORIZED"


class Forbidden(AppError):
    """403: we know who you are, but you may not do this."""

    status = 403
    title = "Forbidden"
    default_code = "FORBIDDEN"


class NotFound(AppError):
    status = 404
    title = "Not found"
    default_code = "NOT_FOUND"


class Conflict(AppError):
    """409: the request is valid but clashes with the current state (duplicate code, wrong lifecycle state...)."""

    status = 409
    title = "Conflict"
    default_code = "CONFLICT"


class RateLimited(AppError):
    """429: too many requests; `retry_after` seconds until the next one would be allowed."""

    status = 429
    title = "Too many requests"
    default_code = "RATE_LIMITED"

    def __init__(self, detail: str, *, retry_after: int) -> None:
        super().__init__(detail, extra={"retry_after": retry_after})
        self.retry_after = retry_after
