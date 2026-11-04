import asyncio
import hashlib
import hmac
import json
import random
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager

from fakepay.app import Config, create_app

SECRET = "whsec_test"
MERCHANT = "http://merchant.test/webhooks/fakepay"


def config(**overrides: object) -> Config:
    base = {
        "webhook_secret": SECRET,
        "latency_min": 0.0,
        "latency_max": 0.0,
        "slow_seconds": 0.05,
        "webhook_delay": 0.0,
        "webhook_retry_base": 0.0,
        "duplicate_webhook_rate": 0.0,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest.fixture
async def make_client() -> AsyncIterator[object]:
    clients: list[tuple[LifespanManager, httpx.AsyncClient]] = []

    async def factory(cfg: Config) -> httpx.AsyncClient:
        app = create_app(cfg, http=httpx.AsyncClient(), rng=random.Random(7))
        manager = LifespanManager(app)
        await manager.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://fakepay")
        clients.append((manager, client))
        return client

    yield factory
    for manager, client in clients:
        await client.aclose()
        await manager.__aexit__(None, None, None)


INTENT = {
    "amount": 250_000,
    "currency": "RSD",
    "description": "VIP pass",
    "return_url": "http://site/b/1",
    "webhook_url": MERCHANT,
}


async def _wait_for(route: respx.Route, calls: int) -> None:
    for _ in range(200):
        if route.call_count >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {calls} webhook calls, got {route.call_count}")


async def test_intent_creation_is_idempotent(make_client) -> None:  # type: ignore[no-untyped-def]
    client = await make_client(config())

    first = await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "booking-1"})
    again = await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "booking-1"})
    other = await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "booking-2"})

    assert first.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert other.json()["id"] != first.json()["id"]
    assert first.json()["checkout_url"].endswith(f"/checkout/{first.json()['id']}")


@respx.mock
async def test_paying_sends_a_correctly_signed_webhook(make_client, respx_mock: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    hook = respx_mock.post(MERCHANT).mock(return_value=httpx.Response(200))
    client = await make_client(config())
    intent = (await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "k"})).json()

    redirect = await client.post(f"/checkout/{intent['id']}/pay")
    await _wait_for(hook, 1)

    assert redirect.status_code == 303 and redirect.headers["location"] == "http://site/b/1?payment=succeeded"
    request = hook.calls[0].request
    header = dict(part.split("=", 1) for part in request.headers["fakepay-signature"].split(","))
    expected = hmac.new(SECRET.encode(), f"{header['t']}.".encode() + request.content, hashlib.sha256).hexdigest()
    assert header["v1"] == expected
    event = json.loads(request.content)
    assert event["type"] == "payment_intent.succeeded" and event["data"]["object"]["id"] == intent["id"]


@respx.mock
async def test_webhook_is_retried_until_the_merchant_answers_2xx(make_client, respx_mock: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    hook = respx_mock.post(MERCHANT).mock(
        side_effect=[httpx.Response(500), httpx.ConnectError("down"), httpx.Response(200)]
    )
    client = await make_client(config())
    intent = (await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "k"})).json()

    await client.post(f"/v1/payment_intents/{intent['id']}/simulate", params={"outcome": "failed"})
    await _wait_for(hook, 3)

    bodies = {json.loads(c.request.content)["id"] for c in hook.calls}
    assert len(bodies) == 1  # every retry is the same event (same id): the merchant dedupes on it


@respx.mock
async def test_duplicate_webhooks_are_sent_on_purpose(make_client, respx_mock: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    hook = respx_mock.post(MERCHANT).mock(return_value=httpx.Response(200))
    client = await make_client(config(duplicate_webhook_rate=1.0))
    intent = (await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "k"})).json()

    await client.post(f"/v1/payment_intents/{intent['id']}/simulate", params={"outcome": "succeeded"})
    await _wait_for(hook, 2)

    assert [json.loads(c.request.content)["id"] for c in hook.calls] == [
        json.loads(hook.calls[0].request.content)["id"]
    ] * 2


async def test_settled_intent_cannot_change_outcome(make_client) -> None:  # type: ignore[no-untyped-def]
    client = await make_client(config(webhook_delay=10))
    intent = (await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "k"})).json()

    await client.post(f"/v1/payment_intents/{intent['id']}/simulate", params={"outcome": "succeeded"})
    later = await client.post(f"/v1/payment_intents/{intent['id']}/simulate", params={"outcome": "failed"})

    assert later.json()["status"] == "succeeded"


@pytest.mark.parametrize(("token", "status"), [("tok_ok", "succeeded"), ("tok_decline", "declined")])
async def test_terminal_charge_outcome_by_token(make_client, token: str, status: str) -> None:  # type: ignore[no-untyped-def]
    client = await make_client(config())

    response = await client.post(
        "/v1/terminal/charges",
        json={"amount": 120_000, "currency": "RSD", "card_token": token},
        headers={"Idempotency-Key": "pay-1"},
    )

    assert response.json()["status"] == status


async def test_terminal_charge_is_idempotent_even_while_in_progress(make_client) -> None:  # type: ignore[no-untyped-def]
    """A retry that arrives while the first (slow) attempt is still running gets the same charge, not a second one."""
    client = await make_client(config())
    body = {"amount": 120_000, "currency": "RSD", "card_token": "tok_slow"}

    first, retry = await asyncio.gather(
        client.post("/v1/terminal/charges", json=body, headers={"Idempotency-Key": "pay-1"}),
        client.post("/v1/terminal/charges", json=body, headers={"Idempotency-Key": "pay-1"}),
    )
    looked_up = await client.get("/v1/terminal/charges/pay-1")

    assert first.json()["id"] == retry.json()["id"] == looked_up.json()["id"]


async def test_unknown_charge_is_404(make_client) -> None:  # type: ignore[no-untyped-def]
    client = await make_client(config())

    assert (await client.get("/v1/terminal/charges/nope")).status_code == 404


async def test_checkout_page_renders(make_client) -> None:  # type: ignore[no-untyped-def]
    client = await make_client(config())
    intent = (await client.post("/v1/payment_intents", json=INTENT, headers={"Idempotency-Key": "k"})).json()

    page = await client.get(f"/checkout/{intent['id']}")

    assert "2,500.00 RSD" in page.text and "Pay" in page.text
