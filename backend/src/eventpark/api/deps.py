"""FastAPI dependencies shared by routers: settings, DB session, current user, role checks.

A dependency is a function FastAPI calls before your endpoint; its return value is injected as a parameter.
`Depends(require_roles(Role.ADMIN))` therefore runs the auth check before the endpoint body even starts.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.cache.rate_limit import Limit, RateLimiter
from eventpark.core.config import Settings
from eventpark.core.errors import Forbidden, RateLimited, Unauthorized
from eventpark.core.security import InvalidTokenError, decode_access_token
from eventpark.domain.roles import STAFF_ROLES, Role


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed afterwards (returns its connection to the pool)."""
    async with request.app.state.sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.limiter
    return limiter


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RedisDep = Annotated[Redis, Depends(get_redis)]
LimiterDep = Annotated[RateLimiter, Depends(get_limiter)]


async def enforce(limiter: RateLimiter, limit: Limit, key: str) -> None:
    decision = await limiter.hit(limit, key)
    if not decision.allowed:
        raise RateLimited("Too many requests, slow down.", retry_after=decision.retry_after_seconds)


def client_ip(request: Request) -> str:
    """The caller's IP, for per-IP rate limits.

    X-Forwarded-For is a list every proxy APPENDS to: "<what the client claimed>, <client as seen by proxy 1>, ...".
    The leftmost entries are typed by the client and can be faked; only entries added by OUR proxies are trustworthy.
    With N trusted proxies in front of us (TRUSTED_PROXY_HOPS: 0 locally, CloudFront + ALB on AWS), the real client
    is the N-th entry from the right.
    """
    hops: int = request.app.state.settings.trusted_proxy_hops
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if hops and len(forwarded) >= hops:
        return forwarded[-hops]
    return request.client.host if request.client else "unknown"


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def utcnow() -> datetime:
    return datetime.now(UTC)


# auto_error=False: we raise our own 401 problem response; it also makes Swagger UI show an "Authorize" button.
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /admin/v1/auth/login")


@dataclass(frozen=True)
class Principal:
    """Who is calling, as proven by the access token. No DB lookup: that's the point of a signed token."""

    user_id: uuid.UUID
    role: Role


async def get_principal(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise Unauthorized("Missing bearer token.", code="NOT_AUTHENTICATED")
    try:
        claims = decode_access_token(
            credentials.credentials, secret=settings.jwt_secret.get_secret_value(), now=utcnow()
        )
    except InvalidTokenError as exc:
        raise Unauthorized("Invalid or expired access token.", code="INVALID_TOKEN") from exc
    return Principal(user_id=claims.user_id, role=claims.role)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_roles(*roles: Role) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory: `Depends(require_roles(Role.ADMIN, Role.EVENT_MANAGER))`.

    No/invalid token -> 401 ("who are you?"); valid token with another role -> 403 ("I know you, but no").
    """
    allowed = frozenset(roles)

    async def check(principal: PrincipalDep) -> Principal:
        if principal.role not in allowed:
            raise Forbidden(
                f"This action requires one of: {', '.join(sorted(r.value for r in allowed))}.",
                code="ROLE_NOT_ALLOWED",
            )
        return principal

    return check


# The permission matrix (docs/01-product-and-domain.md §2) as reusable dependencies.
AdminDep = Annotated[Principal, Depends(require_roles(Role.ADMIN))]
EventManagerDep = Annotated[Principal, Depends(require_roles(Role.ADMIN, Role.EVENT_MANAGER))]
StaffDep = Annotated[Principal, Depends(require_roles(*STAFF_ROLES))]
DriverDep = Annotated[Principal, Depends(require_roles(Role.DRIVER))]
