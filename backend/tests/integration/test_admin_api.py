"""Admin API against a real database: permissions, venue setup, devices, events lifecycle, pagination, audit."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.security import device_api_key_matches
from eventpark.db.models import AuditLog, Device
from eventpark.domain.roles import Role

from .conftest import HeadersFactory

pytestmark = pytest.mark.integration


async def _venue(client: AsyncClient, admin: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body = {"name": "Belgrade Arena Grounds", "address": "Bulevar 1", "timezone": "Europe/Belgrade", **overrides}
    response = await client.post("/admin/v1/venues", json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


async def _zone(client: AsyncClient, admin: dict[str, Any], venue_id: str, code: str, cap: int) -> dict[str, Any]:
    response = await client.post(
        f"/admin/v1/venues/{venue_id}/zones",
        json={"code": code, "name": f"Zone {code}", "total_capacity": cap},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _event_times(start_in: timedelta = timedelta(days=7)) -> dict[str, str]:
    start = datetime.now(UTC).replace(microsecond=0) + start_in
    return {
        "starts_at": (start + timedelta(hours=3)).isoformat(),
        "ends_at": (start + timedelta(hours=6)).isoformat(),
        "gates_open_at": start.isoformat(),
        "gates_close_at": (start + timedelta(hours=4, minutes=30)).isoformat(),
        "booking_opens_at": (start - timedelta(days=6)).isoformat(),
        "booking_closes_at": (start + timedelta(hours=2)).isoformat(),
    }


PRICING = {
    "currency": "RSD",
    "drive_up_free_minutes": 15,
    "drive_up_hourly_minor": 30000,
    "drive_up_daily_cap_minor": 150000,
    "lost_ticket_fee_minor": 200000,
    "post_payment_exit_grace_minutes": 15,
}


# --- permissions (docs/01-product-and-domain.md §2) ---


@pytest.mark.parametrize(
    ("role", "expected"),
    [(None, 401), (Role.DRIVER, 403), (Role.PARKING_STAFF, 403), (Role.EVENT_MANAGER, 403), (Role.ADMIN, 201)],
)
async def test_only_admins_create_venues(
    client: AsyncClient, auth_headers: HeadersFactory, role: Role | None, expected: int
) -> None:
    headers = await auth_headers(role) if role else {}
    body = {"name": "V", "timezone": "Europe/Belgrade"}

    response = await client.post("/admin/v1/venues", json=body, headers=headers)

    assert response.status_code == expected
    if expected == 403:
        assert response.json()["code"] == "ROLE_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.DRIVER, 403), (Role.PARKING_STAFF, 200), (Role.GATE_OPERATOR, 200), (Role.EVENT_MANAGER, 200)],
)
async def test_any_staff_member_reads_venues(
    client: AsyncClient, auth_headers: HeadersFactory, role: Role, expected: int
) -> None:
    response = await client.get("/admin/v1/venues", headers=await auth_headers(role))

    assert response.status_code == expected


async def test_event_managers_manage_events_but_not_users(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    manager = await auth_headers(Role.EVENT_MANAGER)
    venue = await _venue(client, admin)

    created = await client.post(
        "/admin/v1/events", json={"venue_id": venue["id"], "name": "Concert", **_event_times()}, headers=manager
    )
    users = await client.get("/admin/v1/users", headers=manager)

    assert created.status_code == 201
    assert users.status_code == 403


# --- venue setup ---


async def test_unknown_timezone_is_rejected(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    response = await client.post(
        "/admin/v1/venues", json={"name": "V", "timezone": "Mars/Olympus"}, headers=await auth_headers(Role.ADMIN)
    )

    assert response.status_code == 422


async def test_zone_codes_are_unique_per_venue(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    first, second = await _venue(client, admin), await _venue(client, admin, name="Other")
    await _zone(client, admin, first["id"], "A", 100)

    duplicate = await client.post(
        f"/admin/v1/venues/{first['id']}/zones", json={"code": "A", "name": "x", "total_capacity": 5}, headers=admin
    )
    same_code_elsewhere = await client.post(
        f"/admin/v1/venues/{second['id']}/zones", json={"code": "A", "name": "x", "total_capacity": 5}, headers=admin
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ZONE_CODE_TAKEN"
    assert same_code_elsewhere.status_code == 201


async def test_exit_gate_cannot_have_a_printer(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    """Enforced by a CHECK constraint in the database, reported as a clear 409."""
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)

    response = await client.post(
        f"/admin/v1/venues/{venue['id']}/gates",
        json={"code": "X1", "name": "Exit 1", "direction": "EXIT", "has_printer": True},
        headers=admin,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_GATE"


async def test_bulk_create_numbered_spaces(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    vip = await _zone(client, admin, venue["id"], "VIP", 50)

    created = await client.post(
        f"/admin/v1/zones/{vip['id']}/spaces", json={"kind": "VIP", "prefix": "VIP-", "count": 50}, headers=admin
    )
    too_many = await client.post(
        f"/admin/v1/zones/{vip['id']}/spaces", json={"kind": "VIP", "labels": ["EXTRA"]}, headers=admin
    )

    assert created.status_code == 201
    labels = [s["label"] for s in created.json()]
    assert labels[0] == "VIP-01" and labels[-1] == "VIP-50" and len(labels) == 50
    assert too_many.status_code == 409
    assert too_many.json()["code"] == "TOO_MANY_SPACES"


async def test_venue_with_zones_cannot_be_deleted(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    await _zone(client, admin, venue["id"], "A", 10)

    response = await client.delete(f"/admin/v1/venues/{venue['id']}", headers=admin)

    assert response.status_code == 409
    assert response.json()["code"] == "VENUE_IN_USE"


# --- devices ---


async def test_device_key_is_shown_once_stored_hashed_and_rotatable(
    client: AsyncClient, auth_headers: HeadersFactory, db: async_sessionmaker[AsyncSession]
) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    gate = (
        await client.post(
            f"/admin/v1/venues/{venue['id']}/gates",
            json={"code": "N1", "name": "North 1", "direction": "ENTRY", "has_printer": True},
            headers=admin,
        )
    ).json()

    created = await client.post(f"/admin/v1/gates/{gate['id']}/device", json={"name": "n1-pi"}, headers=admin)
    fetched = await client.get(f"/admin/v1/gates/{gate['id']}/device", headers=admin)
    rotated = await client.post(f"/admin/v1/gates/{gate['id']}/device/rotate-key", headers=admin)

    assert created.status_code == 201
    first_key = created.json()["api_key"]
    assert first_key.startswith("epd_")
    assert "api_key" not in fetched.json()  # never shown again
    second_key = rotated.json()["api_key"]
    assert second_key != first_key
    async with db() as session:
        device = await session.scalar(select(Device).where(Device.gate_id == gate["id"]))
    assert device is not None
    assert first_key not in device.api_key_hash
    assert device_api_key_matches(second_key, device.api_key_hash)
    assert not device_api_key_matches(first_key, device.api_key_hash)  # old key is dead


async def test_disable_and_enable_device(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    gate = (
        await client.post(
            f"/admin/v1/venues/{venue['id']}/gates",
            json={"code": "X1", "name": "Exit", "direction": "EXIT"},
            headers=admin,
        )
    ).json()
    await client.post(f"/admin/v1/gates/{gate['id']}/device", json={"name": "x1-pi"}, headers=admin)

    disabled = await client.patch(f"/admin/v1/gates/{gate['id']}/device", json={"enabled": False}, headers=admin)
    enabled = await client.patch(f"/admin/v1/gates/{gate['id']}/device", json={"enabled": True}, headers=admin)

    assert disabled.json()["status"] == "DISABLED"
    assert enabled.json()["status"] == "OFFLINE"


# --- events ---


async def _event_with_zones(client: AsyncClient, admin: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    venue = await _venue(client, admin)
    zone_a = await _zone(client, admin, venue["id"], "A", 1200)
    event = (
        await client.post(
            "/admin/v1/events", json={"venue_id": venue["id"], "name": "Concert", **_event_times()}, headers=admin
        )
    ).json()
    return event, zone_a


async def test_event_lifecycle_from_draft_to_archived(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.EVENT_MANAGER)
    setup = await auth_headers(Role.ADMIN)
    event, zone_a = await _event_with_zones(client, setup)
    base = f"/admin/v1/events/{event['id']}"

    not_ready = await client.post(f"{base}/publish", headers=admin)
    assert not_ready.status_code == 409
    assert not_ready.json()["code"] == "PRICING_MISSING"

    assert (await client.put(f"{base}/pricing", json=PRICING, headers=admin)).status_code == 200
    zones = await client.put(
        f"{base}/zones",
        json=[
            {
                "zone_id": zone_a["id"],
                "capacity": 1000,
                "prebook_quota": 300,
                "prebook_price_minor": 80000,
                "allocation_priority": 1,
            }
        ],
        headers=admin,
    )
    assert zones.status_code == 200
    assert zones.json()[0]["drive_up_capacity"] == 700

    statuses = []
    for action in ("publish", "go-live", "end", "archive"):
        response = await client.post(f"{base}/{action}", headers=admin)
        assert response.status_code == 200, response.text
        statuses.append(response.json()["status"])
    assert statuses == ["PUBLISHED", "LIVE", "ENDED", "ARCHIVED"]

    assert (await client.post(f"{base}/cancel", headers=admin)).json()["code"] == "INVALID_TRANSITION"
    assert (await client.patch(base, json={"name": "Renamed"}, headers=admin)).json()["code"] == "EVENT_NOT_EDITABLE"


async def test_zone_capacity_cannot_exceed_physical_capacity(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    event, zone_a = await _event_with_zones(client, admin)

    response = await client.put(
        f"/admin/v1/events/{event['id']}/zones",
        json=[
            {
                "zone_id": zone_a["id"],
                "capacity": 5000,
                "prebook_quota": 0,
                "prebook_price_minor": 0,
                "allocation_priority": 1,
            }
        ],
        headers=admin,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "CAPACITY_EXCEEDS_ZONE"


async def test_quota_above_capacity_is_refused_by_the_database(
    client: AsyncClient, auth_headers: HeadersFactory
) -> None:
    admin = await auth_headers(Role.ADMIN)
    event, zone_a = await _event_with_zones(client, admin)

    response = await client.put(
        f"/admin/v1/events/{event['id']}/zones",
        json=[
            {
                "zone_id": zone_a["id"],
                "capacity": 100,
                "prebook_quota": 101,
                "prebook_price_minor": 0,
                "allocation_priority": 1,
            }
        ],
        headers=admin,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CAPACITY_BELOW_USAGE"
    # quota 101 > capacity 100 breaks two CHECKs at once (quota, and drive-up 0 <= -1); Postgres names the first
    assert response.json()["constraint"] in {
        "ck_event_zones_quota_within_capacity",
        "ck_event_zones_drive_up_within_capacity",
    }


async def test_event_times_must_be_ordered_and_have_a_zone(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    times = _event_times()
    backwards = {**times, "ends_at": times["starts_at"]}
    naive = {**times, "starts_at": "2026-12-01T20:00:00"}

    r1 = await client.post("/admin/v1/events", json={"venue_id": venue["id"], "name": "E", **backwards}, headers=admin)
    r2 = await client.post("/admin/v1/events", json={"venue_id": venue["id"], "name": "E", **naive}, headers=admin)

    assert r1.status_code == 422 and r1.json()["code"] == "INVALID_TIMES"
    assert r2.status_code == 422  # no time zone offset


async def test_events_are_cursor_paginated(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    for i in range(5):
        await client.post(
            "/admin/v1/events", json={"venue_id": venue["id"], "name": f"E{i}", **_event_times()}, headers=admin
        )

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/admin/v1/events", params=params, headers=admin)).json()
        seen += [e["name"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == ["E0", "E1", "E2", "E3", "E4"]  # UUIDv7 ids: creation order, no gaps, no duplicates


async def test_changes_are_audited(
    client: AsyncClient, auth_headers: HeadersFactory, db: async_sessionmaker[AsyncSession]
) -> None:
    admin = await auth_headers(Role.ADMIN)
    venue = await _venue(client, admin)
    await client.patch(f"/admin/v1/venues/{venue['id']}", json={"name": "Renamed"}, headers=admin)

    async with db() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.target_id == venue["id"]).order_by(AuditLog.id))
        ).all()

    assert [r.action for r in rows] == ["venue.created", "venue.updated"]
    assert rows[1].details == {"name": "Renamed"}
    assert rows[1].actor_user_id is not None


async def test_admin_cannot_lock_themselves_out(client: AsyncClient, auth_headers: HeadersFactory) -> None:
    admin = await auth_headers(Role.ADMIN)
    me = (await client.get("/admin/v1/me", headers=admin)).json()

    response = await client.patch(f"/admin/v1/users/{me['id']}", json={"is_active": False}, headers=admin)

    assert response.status_code == 409
    assert response.json()["code"] == "SELF_LOCKOUT"


async def test_unknown_route_is_problem_json(client: AsyncClient) -> None:
    response = await client.get("/admin/v1/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["request_id"] == response.headers["x-request-id"]
