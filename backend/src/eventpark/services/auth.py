"""Authentication use cases: register, login, refresh (rotation + theft detection), logout.

Tokens (docs/02-architecture.md §9):
- access token: JWT, 15 min, stateless (verified by signature, no DB lookup) -> sent as `Authorization: Bearer`
- refresh token: random 256-bit, 7 days, stored hashed, revocable -> sent as an HttpOnly cookie

Rotation: each refresh revokes the presented token and issues a new one in the same *family*. If a token that was
already rotated is presented again, two parties hold copies of it (the real user and a thief); we can't tell which
is which, so the whole family is revoked and both must log in again.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.config import Settings
from eventpark.core.errors import Conflict, Unauthorized
from eventpark.core.ids import uuid7
from eventpark.core.security import (
    create_access_token,
    hash_password,
    hash_password_async,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password_async,
)
from eventpark.db.models import RefreshToken, User
from eventpark.domain.roles import Role

log = structlog.get_logger()

# Verifying against this when the email is unknown makes "unknown email" and "wrong password" take the same time,
# so an attacker can't find out which emails have accounts by measuring response times (user enumeration).
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")
_INVALID_CREDENTIALS = "Invalid email or password."


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int  # seconds until the access token expires
    user: User


def _access_token(user: User, settings: Settings, now: datetime) -> str:
    return create_access_token(
        user_id=user.id,
        role=user.role,
        secret=settings.jwt_secret.get_secret_value(),
        ttl=settings.access_token_ttl,
        now=now,
    )


def _issue(session: AsyncSession, user: User, family_id: uuid.UUID, settings: Settings, now: datetime) -> TokenPair:
    token, token_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id, token_hash=token_hash, family_id=family_id, expires_at=now + settings.refresh_token_ttl
        )
    )
    return TokenPair(
        access_token=_access_token(user, settings, now),
        refresh_token=token,
        expires_in=settings.access_token_ttl_seconds,
        user=user,
    )


async def register_driver(
    session: AsyncSession, *, email: str, password: str, full_name: str, settings: Settings, now: datetime
) -> TokenPair:
    password_hash = await hash_password_async(password)  # before the transaction: don't hold a DB connection
    async with session.begin():
        user = User(email=email, password_hash=password_hash, full_name=full_name, role=Role.DRIVER)
        session.add(user)
        try:
            await session.flush()  # sends the INSERT now, so a duplicate email fails here (UNIQUE constraint)
        except IntegrityError as exc:
            raise Conflict("An account with this email already exists.", code="EMAIL_TAKEN") from exc
        pair = _issue(session, user, uuid7(), settings, now)
    log.info("auth.registered", user_id=str(user.id))
    return pair


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    allowed_roles: frozenset[Role],
    settings: Settings,
    now: datetime,
) -> TokenPair:
    # Look the user up, then verify the password OUTSIDE any transaction (slow on purpose: no connection held).
    async with session.begin():
        found = (await session.execute(select(User.id, User.password_hash).where(User.email == email))).first()
    if found is None:
        await verify_password_async(_DUMMY_HASH, password)  # same work as a real check (see _DUMMY_HASH)
        raise Unauthorized(_INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
    if not await verify_password_async(found.password_hash, password):
        raise Unauthorized(_INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
    upgraded = await hash_password_async(password) if password_needs_rehash(found.password_hash) else None
    async with session.begin():
        user = await session.get(User, found.id)
        # Same message for "inactive" and "wrong app" (driver on the staff login): don't reveal account details.
        if user is None or not user.is_active or user.role not in allowed_roles:
            raise Unauthorized(_INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
        if upgraded is not None:
            user.password_hash = upgraded  # silently upgrade old hashes while we know the password
        pair = _issue(session, user, uuid7(), settings, now)
    log.info("auth.login", user_id=str(user.id), role=user.role.value)
    return pair


async def refresh(session: AsyncSession, *, refresh_token: str, settings: Settings, now: datetime) -> TokenPair:
    family_to_revoke: uuid.UUID | None = None
    async with session.begin():
        # FOR UPDATE: two simultaneous refreshes with the same token are processed one after the other, so the
        # second one sees the token as already revoked (instead of both succeeding with one token).
        stored = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(refresh_token)).with_for_update()
        )
        if stored is None:
            raise Unauthorized("Invalid refresh token.", code="INVALID_REFRESH_TOKEN")
        if stored.revoked_at is not None:
            family_to_revoke = stored.family_id
        elif stored.expires_at <= now:
            raise Unauthorized("Refresh token expired.", code="REFRESH_TOKEN_EXPIRED")
        else:
            user = await session.get(User, stored.user_id)
            if user is None or not user.is_active:
                raise Unauthorized("Account disabled.", code="ACCOUNT_DISABLED")
            stored.revoked_at = now
            pair = _issue(session, user, stored.family_id, settings, now)

    if family_to_revoke is not None:
        # Reuse detected. Revoke in a separate transaction that commits, then refuse.
        async with session.begin():
            await _revoke_family(session, family_to_revoke, now)
        log.warning("auth.refresh_token_reuse", family_id=str(family_to_revoke))
        raise Unauthorized("Refresh token was already used; please log in again.", code="REFRESH_TOKEN_REUSED")
    return pair


async def logout(session: AsyncSession, *, refresh_token: str, now: datetime) -> None:
    """Revokes the whole family (this browser's session). Unknown tokens are ignored: logout always 'works'."""
    async with session.begin():
        stored = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
        )
        if stored is not None:
            await _revoke_family(session, stored.family_id, now)


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
