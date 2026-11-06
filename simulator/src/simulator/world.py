"""Build a fresh, isolated world for one run through the admin API (as a real admin would): a new venue with its
zones, gates and devices, and a LIVE event with pricing. A new venue per run means runs never see each other's data
(one LIVE event per venue, ADR-025), and the invariant checker can look at exactly this event.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from simulator.scenario import Scenario

ADMIN_EMAIL = "admin@eventpark.dev"
ADMIN_PASSWORD = "eventpark-demo-pass"


@dataclass
class World:
    venue_id: str
    event_id: str
    entry_keys: dict[str, str]  # gate code -> device api key
    exit_keys: dict[str, str]
    event_zones: dict[str, str]  # zone code -> event_zone id
    admin_token: str
    prebooked_codes: list[str] = field(default_factory=list)


class Admin:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(base_url=base_url, timeout=30)
        self.token = ""

    async def login(self) -> None:
        r = await self.http.post("/admin/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        r.raise_for_status()
        self.token = r.json()["access_token"]

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        r = await self.http.request(method, path, json=body, headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json() if r.content else None

    async def aclose(self) -> None:
        await self.http.aclose()


def iso(d: datetime) -> str:
    return d.isoformat()


async def build(admin: Admin, scenario: Scenario, *, live: bool = True) -> World:
    run = uuid.uuid4().hex[:6]
    venue = await admin.call(
        "POST", "/admin/v1/venues", {"name": f"sim-{scenario.name}-{run}", "timezone": "Europe/Belgrade", "address": ""}
    )
    zone_ids: dict[str, str] = {}
    for code, z in scenario.zones.items():
        zone = await admin.call(
            "POST",
            f"/admin/v1/venues/{venue['id']}/zones",
            {"code": code, "name": f"Zone {code}", "total_capacity": max(1, z.capacity)},
        )
        zone_ids[code] = zone["id"]

    async def gate(code: str, direction: str) -> str:
        g = await admin.call(
            "POST",
            f"/admin/v1/venues/{venue['id']}/gates",
            {
                "code": code,
                "name": code,
                "direction": direction,
                "has_printer": direction == "ENTRY",
                "has_terminal": direction == "EXIT",
            },
        )
        device = await admin.call("POST", f"/admin/v1/gates/{g['id']}/device", {"name": f"{code}-sim"})
        return str(device["api_key"])

    entry_keys = {f"N{i + 1}": await gate(f"N{i + 1}", "ENTRY") for i in range(scenario.entry_gates)}
    exit_keys = {f"X{i + 1}": await gate(f"X{i + 1}", "EXIT") for i in range(scenario.exit_gates)}

    now = datetime.now(UTC)
    event = await admin.call(
        "POST",
        "/admin/v1/events",
        {
            "venue_id": venue["id"],
            "name": f"{scenario.name} {run}",
            "starts_at": iso(now + timedelta(minutes=30)),
            "ends_at": iso(now + timedelta(hours=4)),
            "gates_open_at": iso(now - timedelta(minutes=1)),
            "gates_close_at": iso(now + timedelta(hours=8)),
            "booking_opens_at": iso(now - timedelta(hours=1)),
            "booking_closes_at": iso(now + timedelta(hours=3)),
        },
    )
    await admin.call(
        "PUT",
        f"/admin/v1/events/{event['id']}/pricing",
        {
            "currency": "RSD",
            "drive_up_free_minutes": scenario.free_minutes,
            "drive_up_hourly_minor": scenario.hourly_minor,
            "drive_up_daily_cap_minor": scenario.hourly_minor * 5,
            "lost_ticket_fee_minor": 200_000,
            "post_payment_exit_grace_minutes": 15,
        },
    )
    detail = await admin.call(
        "PUT",
        f"/admin/v1/events/{event['id']}/zones",
        [
            {
                "zone_id": zone_ids[code],
                "capacity": z.capacity,
                "prebook_quota": z.prebook_quota,
                "prebook_price_minor": z.price_minor,
                "allocation_priority": z.priority,
            }
            for code, z in scenario.zones.items()
        ],
    )
    event_zones = {ez["zone_code"]: ez["id"] for ez in detail}  # PUT returns the event zones
    await admin.call("POST", f"/admin/v1/events/{event['id']}/publish")
    world = World(venue["id"], event["id"], entry_keys, exit_keys, event_zones, admin.token)
    if live:
        await go_live(admin, world)
    return world


async def go_live(admin: Admin, world: World) -> None:
    await admin.call("POST", f"/admin/v1/events/{world.event_id}/go-live")
