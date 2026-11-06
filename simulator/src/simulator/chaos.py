"""Fault injection.

ChaosTransport sits under the gate client's httpx client. With `drop_response_rate` it sends the request (the API
does the work and commits!) and then throws the answer away, raising a timeout like a flaky mobile network would.
The lane retries with the same scan_id/request_id, and the API must answer with the stored decision instead of
taking a second place. Out-of-band actions (flush Redis, restart a worker) run via `docker compose`.
"""

import asyncio
import random
import subprocess

import httpx

from simulator.metrics import Metrics


class ChaosTransport(httpx.AsyncBaseTransport):
    def __init__(self, metrics: Metrics, *, drop_rate: float, extra_latency_ms: int, seed: int | None = None) -> None:
        self._inner = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=50))
        self._metrics = metrics
        self._drop_rate = drop_rate
        self._latency = extra_latency_ms / 1000
        self._random = random.Random(seed)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._latency:
            await asyncio.sleep(self._latency)
        response = await self._inner.handle_async_request(request)
        if self._drop_rate and self._random.random() < self._drop_rate:
            await response.aread()
            await response.aclose()
            self._metrics.dropped_responses += 1
            raise httpx.ReadTimeout("chaos: response dropped", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def run_action(action: dict[str, str | int]) -> str:
    """One out-of-band fault. Returns a line for the report."""
    if action.get("do") == "flush_redis":
        cmd = ["docker", "compose", "exec", "-T", "valkey", "valkey-cli", "FLUSHALL"]
    elif "restart" in action:
        cmd = ["docker", "compose", "restart", str(action["restart"])]
    elif "stop" in action:
        cmd = ["docker", "compose", "stop", str(action["stop"])]
    elif "start" in action:
        cmd = ["docker", "compose", "start", str(action["start"])]
    else:
        return f"unknown chaos action {action}"
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return f"{' '.join(cmd[2:])} -> exit {result.returncode}"
