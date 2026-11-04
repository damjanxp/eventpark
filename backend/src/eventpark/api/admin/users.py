"""Admin: users. ADMIN only (docs/01-product-and-domain.md §2 permission matrix)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, EmailStr, Field

from eventpark.api.auth import UserOut
from eventpark.api.deps import AdminDep, SessionDep, utcnow
from eventpark.api.schemas import Page
from eventpark.domain.roles import Role
from eventpark.services import users

router = APIRouter(prefix="/admin/v1/users", tags=["admin: users"])


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    role: Role


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


@router.get("")
async def list_users(
    _: AdminDep,
    session: SessionDep,
    role: Role | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[UserOut]:
    items, next_cursor = await users.list_users(session, role=role, cursor=cursor, limit=limit)
    return Page(items=[UserOut.model_validate(u) for u in items], next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, principal: AdminDep, session: SessionDep) -> UserOut:
    user = await users.create_user(
        session,
        actor=principal.user_id,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        role=body.role,
    )
    return UserOut.model_validate(user)


@router.get("/{user_id}")
async def get_user(user_id: uuid.UUID, _: AdminDep, session: SessionDep) -> UserOut:
    return UserOut.model_validate(await users.get_user(session, user_id))


@router.patch("/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserUpdate, principal: AdminDep, session: SessionDep) -> UserOut:
    user = await users.update_user(
        session,
        actor=principal.user_id,
        user_id=user_id,
        full_name=body.full_name,
        role=body.role,
        is_active=body.is_active,
        password=body.password,
        now=utcnow(),
    )
    return UserOut.model_validate(user)
