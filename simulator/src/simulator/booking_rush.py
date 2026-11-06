"""Online bookings: many drivers at once (the `presale` scenario) and the pre-booked share of a gate rush.

Each virtual driver registers, books one place and pays at fakepay. `test_outcome: succeed` lets fakepay settle the
payment by itself and send the signed webhook (incl. its deliberate duplicates), so this exercises the whole async
path: booking hold -> payment intent -> webhook -> confirmation -> ticket.
"""

import asyncio
import random
import time
import uuid

import httpx

from simulator.metrics import Metrics
from simulator.scenario import Scenario
from simulator.world import World

PASSWORD = "simulated-driver-pass"


async def _register(http: httpx.AsyncClient, metrics: Metrics) -> str | None:
    """Sign up one driver; returns its access token. Happens BEFORE the sale opens (like real users)."""
    email = f"sim-{uuid.uuid4().hex[:12]}@sim.eventpark.dev"
    t = time.perf_counter()
    reg = await http.post(
        "/public/v1/auth/register", json={"email": email, "password": PASSWORD, "full_name": "Sim Driver"}
    )
    metrics.record("POST /public/v1/auth/register", (time.perf_counter() - t) * 1000)
    if reg.status_code != 201:
        metrics.errors[f"register {reg.status_code}"] += 1
        return None
    return str(reg.json()["access_token"])


async def _book(
    http: httpx.AsyncClient, world: World, zone_id: str, token: str, metrics: Metrics, rng: random.Random
) -> str | None:
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid.uuid4().hex}
    body = {
        "event_id": world.event_id,
        "event_zone_id": zone_id,
        "plate": f"BG{rng.randint(100, 999)}SIM",
        "test_outcome": "succeed",
    }
    t = time.perf_counter()
    booked = await http.post("/public/v1/bookings", json=body, headers=headers)
    metrics.record("POST /public/v1/bookings", (time.perf_counter() - t) * 1000)
    if booked.status_code != 201:
        reason = booked.json().get("code", booked.status_code) if booked.content else booked.status_code
        metrics.errors[f"booking {reason}"] += 1
        metrics.bookings_failed += 1
        return None
    booking_id = booked.json()["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:  # the signed webhook usually confirms within a second
        await asyncio.sleep(0.5)
        r = await http.get(f"/public/v1/bookings/{booking_id}", headers={"Authorization": headers["Authorization"]})
        data = r.json()
        if data["status"] == "CONFIRMED":
            metrics.bookings_confirmed += 1
            metrics.record("booking confirmed (webhook)", (time.perf_counter() - t) * 1000)
            return str(data["ticket_code"])
        if data["status"] in ("CANCELLED", "EXPIRED"):
            break
    metrics.bookings_failed += 1
    metrics.errors["booking not confirmed within 60 s"] += 1
    return None


async def book(base_url: str, world: World, scenario: Scenario, count: int, metrics: Metrics, seed: int) -> list[str]:
    """Book `count` places spread over the zones with a pre-book quota; returns the confirmed ticket codes."""
    rng = random.Random(seed)
    zones = [(world.event_zones[c], z.prebook_quota) for c, z in scenario.zones.items() if z.prebook_quota > 0]
    if not zones or count <= 0:
        return []
    weights = [q for _, q in zones]
    semaphore = asyncio.Semaphore(scenario.booking_concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:

        async def register() -> str | None:
            async with semaphore:
                return await _register(http, metrics)

        # 1. accounts exist before the sale (argon2 is slow by design: keep it out of the booking measurement)
        tokens = [t for t in await asyncio.gather(*(register() for _ in range(count))) if t]

        async def book_one(token: str) -> str | None:
            zone_id = rng.choices([z for z, _ in zones], weights)[0]
            async with semaphore:
                return await _book(http, world, zone_id, token, metrics, rng)

        # 2. the sale opens: everyone clicks "book" at once
        codes = await asyncio.gather(*(book_one(t) for t in tokens))
    return [c for c in codes if c]
