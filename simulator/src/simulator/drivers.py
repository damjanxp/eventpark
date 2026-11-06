"""Virtual cars and lanes.

Each lane is a queue served by one worker, like a real barrier: one car at a time. A car arrives, joins the shortest
entry queue, gets served (scan its QR or press for a ticket -> decision -> barrier up for `lane_seconds` -> loop
detector -> complete), stays, then joins the shortest exit queue (scan -> maybe pay -> barrier -> complete).

The lane talks to the API with gatectl's GateClient: the same 2 s timeout, retries with backoff and idempotency keys
as the Raspberry Pi. So what we measure is what a driver at the barrier would experience, retries included.

Little's Law shows up here: if cars arrive faster than lanes x (1 / time per car), the queues grow, however fast the
API answers. The report shows the queue waits next to the API latencies.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from gatectl.client import GateApiError, GateApiUnavailable, GateClient, new_action_id

from simulator.chaos import ChaosTransport
from simulator.metrics import Metrics
from simulator.scenario import Scenario


@dataclass
class Car:
    n: int
    arrive_after_s: float  # real seconds after the start
    stay_s: float  # real seconds
    prebooked_code: str | None = None
    ticket_code: str | None = None
    card_tokens: list[str] = field(default_factory=lambda: ["tok_ok"])


@dataclass
class Job:
    car: Car
    queued_at: float
    done: asyncio.Future[bool]


class Lane:
    def __init__(
        self,
        code: str,
        direction: str,
        client: GateClient,
        scenario: Scenario,
        metrics: Metrics,
    ) -> None:
        self.code, self.direction, self.client = code, direction, client
        self.scenario, self.metrics = scenario, metrics
        self.queue: asyncio.Queue[Job | None] = asyncio.Queue()
        self.busy = False

    def load(self) -> int:
        return self.queue.qsize() + int(self.busy)

    async def submit(self, car: Car) -> bool:
        job = Job(car, time.monotonic(), asyncio.get_running_loop().create_future())
        await self.queue.put(job)
        return await job.done

    async def run(self) -> None:
        while (job := await self.queue.get()) is not None:
            self.busy = True
            # Queue time in SIMULATED seconds (what a driver would feel on the road).
            waited = (time.monotonic() - job.queued_at) * self.scenario.time_scale
            self.metrics.queue_wait_s[self.direction.lower()].append(waited)
            try:
                ok = await (self._enter(job.car) if self.direction == "ENTRY" else self._exit(job.car))
            except (GateApiUnavailable, GateApiError) as exc:
                self.metrics.errors[f"{self.direction.lower()}: {type(exc).__name__}"] += 1
                self.metrics.stuck += 1
                ok = False
            finally:
                self.busy = False
            job.done.set_result(ok)

    async def _timed(self, name: str, call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return await call
        finally:
            self.metrics.record(name, (time.perf_counter() - started) * 1000)

    def _decision(self, d: dict[str, Any]) -> str:
        self.metrics.decisions[d["decision"]] += 1
        if d.get("reason"):
            self.metrics.reasons[d["reason"]] += 1
        return str(d["decision"])

    async def _drive_through(self, passage_id: str) -> None:
        await asyncio.sleep(self.scenario.lane_seconds / self.scenario.time_scale)  # barrier up, car rolls through
        await self._timed("complete", self.client.complete(passage_id))

    async def _enter(self, car: Car) -> bool:
        if car.prebooked_code:
            d = await self._timed("entry/scan", self.client.entry_scan(new_action_id(), car.prebooked_code))
        else:
            d = await self._timed("entry/issue", self.client.entry_issue(new_action_id()))
        if self._decision(d) != "OPEN":
            self.metrics.turned_away += 1
            return False
        await self._drive_through(d["passage_id"])
        car.ticket_code = car.prebooked_code or d["print"]["ticket_code"]
        self.metrics.entered += 1
        return True

    async def _exit(self, car: Car) -> bool:
        assert car.ticket_code is not None
        d = await self._timed("exit/scan", self.client.exit_scan(new_action_id(), car.ticket_code))
        decision = self._decision(d)
        if decision == "PAYMENT_REQUIRED":
            d = await self._pay(car, d["passage_id"])
            decision = self._decision(d)
        if decision != "OPEN":
            self.metrics.stuck += 1  # in real life: press the help button, staff opens manually
            return False
        await self._drive_through(d["passage_id"])
        self.metrics.exited += 1
        return True

    async def _pay(self, car: Car, passage_id: str) -> dict[str, Any]:
        d: dict[str, Any] = {"decision": "DECLINED"}
        for token in car.card_tokens:  # declined -> the driver tries the next card
            request_id = new_action_id()  # one id per card: retries of THIS payment reuse it
            for _attempt in range(8):
                try:
                    d = await self._timed("exit/pay", self.client.exit_pay(request_id, passage_id, token))
                except GateApiUnavailable:
                    d = {"decision": "PAYMENT_PENDING"}
                if d["decision"] != "PAYMENT_PENDING":
                    if d["decision"] == "DECLINED":
                        self.metrics.decisions["DECLINED"] += 1
                    break
                # Outcome unknown (slow terminal): ask again with the SAME id, the provider charges at most once.
                self.metrics.decisions["PAYMENT_PENDING"] += 1
                await asyncio.sleep(1.5)
            if d["decision"] == "OPEN":
                self.metrics.payments_ok += 1
                return d
        return d

    async def heartbeats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.client.heartbeat("sim-1.0", "OK" if self.direction == "ENTRY" else None)
            except (GateApiUnavailable, GateApiError):
                self.metrics.errors["heartbeat failed"] += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)


def make_lanes(
    base_url: str, keys: dict[str, str], direction: str, scenario: Scenario, metrics: Metrics, seed: int
) -> list[Lane]:
    lanes = []
    for i, (code, key) in enumerate(keys.items()):
        transport = ChaosTransport(
            metrics,
            drop_rate=scenario.chaos.drop_response_rate,
            extra_latency_ms=scenario.chaos.extra_latency_ms,
            seed=seed + i,
        )
        lanes.append(Lane(code, direction, GateClient(base_url, key, transport=transport), scenario, metrics))
    return lanes


def make_cars(scenario: Scenario, prebooked_codes: list[str], seed: int) -> list[Car]:
    """Arrival times: a Poisson process with N arrivals in a window = N uniform random times, sorted."""
    rng = random.Random(seed)
    window = scenario.real_seconds(scenario.arrivals.window_minutes)
    n = scenario.arrivals.vehicles
    times = sorted(0.0 if scenario.arrivals.distribution == "burst" else rng.uniform(0, window) for _ in range(n))
    codes = list(prebooked_codes)
    prebooked = dict(zip(rng.sample(range(n), min(len(codes), n)), codes, strict=True))  # which cars booked
    cars = []
    for i, t in enumerate(times):
        stay_min = max(1.0, rng.gauss(scenario.stay_mean_minutes, scenario.stay_stddev_minutes))
        car = Car(n=i, arrive_after_s=t, stay_s=scenario.real_seconds(stay_min), prebooked_code=prebooked.get(i))
        r = rng.random()
        if r < scenario.payments.decline_rate:
            car.card_tokens = ["tok_decline", "tok_ok"]
        elif r < scenario.payments.decline_rate + scenario.payments.slow_rate:
            car.card_tokens = ["tok_slow"]
        cars.append(car)
    return cars


async def drive(car: Car, entries: list[Lane], exits: list[Lane], scenario: Scenario, start: float) -> None:
    await asyncio.sleep(max(0.0, start + car.arrive_after_s - time.monotonic()))
    lane = min(entries, key=Lane.load)  # drivers pick the shortest queue
    if not await lane.submit(car) or not scenario.leave or not exits:
        return
    await asyncio.sleep(car.stay_s)
    await min(exits, key=Lane.load).submit(car)
