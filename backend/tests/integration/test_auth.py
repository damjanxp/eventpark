"""Auth flows against a real database: login, refresh rotation, reuse detection, logout, register, roles."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.api.auth import REFRESH_COOKIE
from eventpark.db.models import RefreshToken, User
from eventpark.domain.roles import Role

from .conftest import UserFactory

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery"


def _use_refresh_cookie(client: AsyncClient, value: str | None) -> None:
    """Make the client send exactly this refresh cookie (as if it were a different browser holding a copy)."""
    assert value is not None
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, value)


async def _login(client: AsyncClient, email: str, password: str = PASSWORD, *, prefix: str = "/admin/v1"):  # type: ignore[no-untyped-def]
    return await client.post(f"{prefix}/auth/login", json={"email": email, "password": password})


async def test_login_returns_access_token_and_sets_refresh_cookie(client: AsyncClient, make_user: UserFactory) -> None:
    user = await make_user(Role.EVENT_MANAGER)

    response = await _login(client, user.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"]["role"] == "EVENT_MANAGER"
    cookie = response.headers["set-cookie"]
    assert f"{REFRESH_COOKIE}=" in cookie
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/admin/v1/auth"):
        assert attribute.lower() in cookie.lower()


async def test_login_email_is_case_insensitive(client: AsyncClient, make_user: UserFactory) -> None:
    await make_user(Role.ADMIN, email="Boss@Example.com")

    assert (await _login(client, "boss@example.com")).status_code == 200


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "inactive", "driver_on_staff_login"])
async def test_login_failures_all_look_the_same(client: AsyncClient, make_user: UserFactory, case: str) -> None:
    """One generic 401 for every failure: the response must not reveal whether an account exists or why."""
    if case == "wrong_password":
        user = await make_user(Role.ADMIN)
        response = await _login(client, user.email, "wrong-password")
    elif case == "unknown_email":
        response = await _login(client, "nobody@example.com")
    elif case == "inactive":
        user = await make_user(Role.ADMIN, is_active=False)
        response = await _login(client, user.email)
    else:
        user = await make_user(Role.DRIVER)
        response = await _login(client, user.email)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert response.json()["detail"] == "Invalid email or password."


async def test_me_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/admin/v1/me")

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_with_token(client: AsyncClient, make_user: UserFactory) -> None:
    user = await make_user(Role.GATE_OPERATOR)
    token = (await _login(client, user.email)).json()["access_token"]

    response = await client.get("/admin/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == user.email


async def test_garbage_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/admin/v1/me", headers={"Authorization": "Bearer not.a.jwt"})

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


async def test_refresh_rotates_the_token(
    client: AsyncClient, make_user: UserFactory, db: async_sessionmaker[AsyncSession]
) -> None:
    user = await make_user(Role.ADMIN)
    await _login(client, user.email)
    first_cookie = client.cookies.get(REFRESH_COOKIE)

    response = await client.post("/admin/v1/auth/refresh")

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert client.cookies.get(REFRESH_COOKIE) != first_cookie
    async with db() as session:
        rows = (await session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id))).all()
    assert len(rows) == 2
    assert len({r.family_id for r in rows}) == 1  # same family
    assert sum(r.revoked_at is None for r in rows) == 1  # only the newest is still valid


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(
    client: AsyncClient, make_user: UserFactory, db: async_sessionmaker[AsyncSession]
) -> None:
    """Theft detection: the old token is presented after it was rotated -> everyone in that family is logged out."""
    user = await make_user(Role.ADMIN)
    await _login(client, user.email)
    stolen = client.cookies.get(REFRESH_COOKIE)
    assert (await client.post("/admin/v1/auth/refresh")).status_code == 200  # legitimate user rotates
    current = client.cookies.get(REFRESH_COOKIE)

    _use_refresh_cookie(client, stolen)  # attacker replays the old one
    reuse = await client.post("/admin/v1/auth/refresh")
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "REFRESH_TOKEN_REUSED"

    _use_refresh_cookie(client, current)  # the real user is out too
    assert (await client.post("/admin/v1/auth/refresh")).status_code == 401
    async with db() as session:
        active = await session.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.revoked_at.is_(None))
        )
    assert active == 0


async def test_logout_revokes_and_clears_cookie(client: AsyncClient, make_user: UserFactory) -> None:
    user = await make_user(Role.ADMIN)
    await _login(client, user.email)
    token = client.cookies.get(REFRESH_COOKIE)

    response = await client.post("/admin/v1/auth/logout")

    assert response.status_code == 204
    _use_refresh_cookie(client, token)
    assert (await client.post("/admin/v1/auth/refresh")).status_code == 401


async def test_refresh_without_cookie_is_401(client: AsyncClient) -> None:
    response = await client.post("/admin/v1/auth/refresh")

    assert response.status_code == 401


async def test_driver_register_login_and_me(client: AsyncClient, db: async_sessionmaker[AsyncSession]) -> None:
    payload = {"email": "driver@example.com", "password": "a-long-password", "full_name": "Ana Driver"}

    created = await client.post("/public/v1/auth/register", json=payload)

    assert created.status_code == 201
    assert created.json()["user"]["role"] == "DRIVER"
    assert "path=/public/v1/auth" in created.headers["set-cookie"].lower()
    token = created.json()["access_token"]
    me = await client.get("/public/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == "driver@example.com"
    # drivers can't use the staff API
    assert (await client.get("/admin/v1/me", headers={"Authorization": f"Bearer {token}"})).status_code == 403
    async with db() as session:
        user = await session.scalar(select(User).where(User.email == "driver@example.com"))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")


async def test_register_duplicate_email_is_409(client: AsyncClient, make_user: UserFactory) -> None:
    await make_user(Role.DRIVER, email="taken@example.com")

    response = await client.post(
        "/public/v1/auth/register",
        json={"email": "TAKEN@example.com", "password": "a-long-password", "full_name": "X"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TAKEN"


async def test_register_validates_input_without_echoing_password(client: AsyncClient) -> None:
    response = await client.post(
        "/public/v1/auth/register", json={"email": "not-an-email", "password": "pw-Zq9", "full_name": ""}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert {tuple(e["loc"])[-1] for e in body["errors"]} == {"email", "password", "full_name"}
    assert "pw-Zq9" not in response.text
