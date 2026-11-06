"""User management (ADMIN only)."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound
from eventpark.core.security import hash_password_async
from eventpark.db.models import RefreshToken, User
from eventpark.db.pagination import paginate
from eventpark.domain.roles import Role
from eventpark.services import audit


async def list_users(
    session: AsyncSession, *, role: Role | None, cursor: str | None, limit: int
) -> tuple[Sequence[User], str | None]:
    query = select(User)
    if role is not None:
        query = query.where(User.role == role)
    return await paginate(session, query, User.id, cursor=cursor, limit=limit)


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User not found.", code="USER_NOT_FOUND")
    return user


async def create_user(
    session: AsyncSession, *, actor: uuid.UUID, email: str, password: str, full_name: str, role: Role
) -> User:
    password_hash = await hash_password_async(password)  # slow on purpose: off the event loop, before the tx
    async with session.begin():
        user = User(email=email, password_hash=password_hash, full_name=full_name, role=role)
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise Conflict("An account with this email already exists.", code="EMAIL_TAKEN") from exc
        audit.record(
            session,
            actor_user_id=actor,
            action="user.created",
            target_type="user",
            target_id=user.id,
            details={"role": role.value},
        )
    return user


async def update_user(
    session: AsyncSession,
    *,
    actor: uuid.UUID,
    user_id: uuid.UUID,
    full_name: str | None,
    role: Role | None,
    is_active: bool | None,
    password: str | None,
    now: datetime,
) -> User:
    async with session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None:
            raise NotFound("User not found.", code="USER_NOT_FOUND")
        # Guard against locking yourself out (and with that, possibly the last admin).
        if user_id == actor and (is_active is False or (role is not None and role is not Role.ADMIN)):
            raise Conflict("You can't deactivate or demote your own account.", code="SELF_LOCKOUT")

        changes: dict[str, object] = {}
        if full_name is not None:
            user.full_name = full_name
            changes["full_name"] = full_name
        if role is not None and role is not user.role:
            changes["role"] = {"from": user.role.value, "to": role.value}
            user.role = role
        if is_active is not None and is_active != user.is_active:
            user.is_active = is_active
            changes["is_active"] = is_active
        if password is not None:
            user.password_hash = await hash_password_async(password)
            changes["password"] = "changed"  # noqa: S105 (a marker, never the value)

        # Deactivation, role change or new password: end all sessions, so the change takes effect within one
        # access-token lifetime (15 min) instead of up to 7 days.
        if {"is_active", "role", "password"} & changes.keys():
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        if changes:
            audit.record(
                session,
                actor_user_id=actor,
                action="user.updated",
                target_type="user",
                target_id=user.id,
                details=changes,
            )
    return user
