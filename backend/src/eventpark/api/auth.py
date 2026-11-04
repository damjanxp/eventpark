"""Auth endpoints for staff (/admin/v1/auth, /admin/v1/me) and drivers (/public/v1/auth, /public/v1/me).

Both use the same mechanism (services/auth.py); they differ in who may log in and in the cookie path. The refresh
token travels only in an HttpOnly cookie:
- HttpOnly: JavaScript can't read it, so an XSS bug can't steal it;
- Secure: only sent over HTTPS (browsers treat http://localhost as secure);
- SameSite=Strict: not sent when another site triggers the request, so CSRF can't use it;
- Path=/admin/v1/auth: only sent to the auth endpoints, not with every API call.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from eventpark.api.deps import (
    LimiterDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    client_ip,
    enforce,
    require_roles,
    utcnow,
)
from eventpark.cache.rate_limit import LOGIN_PER_EMAIL, LOGIN_PER_IP
from eventpark.core.config import Settings
from eventpark.core.errors import NotFound, Unauthorized
from eventpark.db.models import User
from eventpark.domain.roles import STAFF_ROLES, Role
from eventpark.services import auth

REFRESH_COOKIE = "ep_refresh"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: EmailStr
    # Length beats complexity rules (NIST SP 800-63B). Upper bound: argon2 work shouldn't be abusable with 1 MB inputs.
    password: str = Field(min_length=10, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 (OAuth2 token type, not a secret)
    expires_in: int
    user: UserOut


def _token_response(response: Response, pair: auth.TokenPair, settings: Settings, cookie_path: str) -> TokenResponse:
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=int(settings.refresh_token_ttl.total_seconds()),
        path=cookie_path,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
    )
    return TokenResponse(
        access_token=pair.access_token, expires_in=pair.expires_in, user=UserOut.model_validate(pair.user)
    )


def build_auth_router(*, prefix: str, tag: str, allowed_roles: frozenset[Role], allow_register: bool) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])
    cookie_path = f"{prefix}/auth"
    me_dependency = require_roles(*allowed_roles)

    if allow_register:

        @router.post("/auth/register", status_code=status.HTTP_201_CREATED)
        async def register(
            body: RegisterRequest, response: Response, session: SessionDep, settings: SettingsDep
        ) -> TokenResponse:
            pair = await auth.register_driver(
                session,
                email=body.email,
                password=body.password,
                full_name=body.full_name,
                settings=settings,
                now=utcnow(),
            )
            return _token_response(response, pair, settings, cookie_path)

    @router.post("/auth/login")
    async def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        session: SessionDep,
        settings: SettingsDep,
        limiter: LimiterDep,
    ) -> TokenResponse:
        # Two buckets: per IP (one machine trying many accounts) and per email (many machines trying one account).
        await enforce(limiter, LOGIN_PER_IP, client_ip(request))
        await enforce(limiter, LOGIN_PER_EMAIL, body.email.lower())
        pair = await auth.login(
            session,
            email=body.email,
            password=body.password,
            allowed_roles=allowed_roles,
            settings=settings,
            now=utcnow(),
        )
        return _token_response(response, pair, settings, cookie_path)

    @router.post("/auth/refresh")
    async def refresh(
        response: Response,
        session: SessionDep,
        settings: SettingsDep,
        ep_refresh: Annotated[str | None, Cookie()] = None,
    ) -> TokenResponse:
        if not ep_refresh:
            raise Unauthorized("No refresh token cookie.", code="INVALID_REFRESH_TOKEN")
        pair = await auth.refresh(session, refresh_token=ep_refresh, settings=settings, now=utcnow())
        if pair.user.role not in allowed_roles:
            raise Unauthorized("Invalid refresh token.", code="INVALID_REFRESH_TOKEN")
        return _token_response(response, pair, settings, cookie_path)

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        response: Response, session: SessionDep, ep_refresh: Annotated[str | None, Cookie()] = None
    ) -> Response:
        if ep_refresh:
            await auth.logout(session, refresh_token=ep_refresh, now=utcnow())
        response.delete_cookie(REFRESH_COOKIE, path=cookie_path)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/me")
    async def me(principal: PrincipalDep, session: SessionDep) -> UserOut:
        await me_dependency(principal)
        user = await session.get(User, principal.user_id)
        if user is None:
            raise NotFound("User not found.")
        return UserOut.model_validate(user)

    return router


admin_auth_router = build_auth_router(
    prefix="/admin/v1", tag="auth (staff)", allowed_roles=STAFF_ROLES, allow_register=False
)
public_auth_router = build_auth_router(
    prefix="/public/v1", tag="auth (drivers)", allowed_roles=frozenset({Role.DRIVER}), allow_register=True
)
