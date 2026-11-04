import json
import random

import httpx
import pytest
import respx

from gatectl.client import GateApiError, GateApiUnavailable, GateClient, RetryPolicy

BASE = "http://api.test"


def client() -> GateClient:
    # zero delays: tests shouldn't sleep; same retry logic otherwise
    return GateClient(BASE, "epd_0123456789ab_secret", retry=RetryPolicy(base_delay=0.0, rng=random.Random(1)))


@respx.mock
async def test_retries_reuse_the_same_scan_id() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/scan").mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(503, json={"code": "UNAVAILABLE"}),
            httpx.Response(200, json={"decision": "OPEN", "passage_id": "p1", "display_text": "Welcome!"}),
        ]
    )

    result = await client().entry_scan("scan-123", "EP1-CODE")

    assert result["decision"] == "OPEN"
    sent = [json.loads(call.request.content)["scan_id"] for call in route.calls]
    assert sent == ["scan-123", "scan-123", "scan-123"]  # one action, one key, every attempt


@respx.mock
async def test_gives_up_after_all_attempts() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/issue").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(GateApiUnavailable):
        await client().entry_issue("req-1")
    assert route.call_count == 4  # 1 try + 3 retries


@respx.mock
async def test_client_errors_are_not_retried() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/issue").mock(
        return_value=httpx.Response(401, json={"code": "DEVICE_DISABLED"})
    )

    with pytest.raises(GateApiError) as info:
        await client().entry_issue("req-1")
    assert info.value.status == 401 and info.value.code == "DEVICE_DISABLED"
    assert route.call_count == 1  # retrying a 401 can't help


@respx.mock
async def test_sends_device_authorization() -> None:
    route = respx.post(f"{BASE}/gate/v1/heartbeat").mock(return_value=httpx.Response(200, json={"commands": []}))

    await client().heartbeat("1.0", "OK")

    assert route.calls[0].request.headers["authorization"] == "Device epd_0123456789ab_secret"


def test_backoff_grows_exponentially_with_jitter() -> None:
    policy = RetryPolicy(base_delay=0.2, jitter=0.5, rng=random.Random(42))
    delays = [policy.delay(n) for n in range(3)]

    for n, delay in enumerate(delays):
        expected = 0.2 * 2**n
        assert expected * 0.5 <= delay <= expected * 1.5  # 0.2, 0.4, 0.8 s +- 50 %
    assert len({round(d, 6) for d in delays}) == 3
