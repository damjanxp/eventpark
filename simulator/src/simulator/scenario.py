"""Scenario files (scenarios/*.yaml) -> typed config.

Time: the simulator runs on a scaled clock. `time_scale: 60` = one simulated minute passes in one real second, so a
15-minute rush takes 15 s and a 3-hour stay takes 3 minutes. The SERVER runs on real time, so anything time-based
on the server (fees, 60 s holds) sees real durations; scenarios that want fees set `free_minutes: 0`.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Zone:
    capacity: int
    prebook_quota: int = 0
    priority: int = 1  # drive-up allocation order; 0 = no drive-up here
    price_minor: int = 80_000


@dataclass(frozen=True)
class Arrivals:
    vehicles: int
    window_minutes: float  # all arrivals happen within this many simulated minutes
    distribution: str = "poisson"  # poisson (uniform order statistics) | burst (all at t=0)


@dataclass(frozen=True)
class Payments:
    decline_rate: float = 0.0  # first card declined, driver tries another
    slow_rate: float = 0.0  # terminal takes 8 s: the API times out (5 s), the lane retries with the same id


@dataclass(frozen=True)
class Chaos:
    drop_response_rate: float = 0.0  # request reaches the API, the answer is lost: the lane retries (same id)
    extra_latency_ms: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)  # [{at_seconds: 5, do: flush_redis | restart: svc}]


@dataclass(frozen=True)
class Scenario:
    name: str
    zones: dict[str, Zone]
    entry_gates: int
    exit_gates: int
    arrivals: Arrivals
    prebooked_share: float
    stay_mean_minutes: float
    stay_stddev_minutes: float
    lane_seconds: float  # simulated seconds a car occupies a lane after OPEN (barrier up, drive through)
    time_scale: float
    free_minutes: int
    hourly_minor: int
    payments: Payments
    chaos: Chaos
    leave: bool  # simulate exits too (False: entry-only rush)
    presale_only: bool = False  # booking rush, no gate traffic
    booking_concurrency: int = 20

    def real_seconds(self, sim_minutes: float) -> float:
        return sim_minutes * 60 / self.time_scale


def load(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text())
    zones = {
        code: Zone(
            capacity=z["capacity"],
            prebook_quota=z.get("prebook_quota", 0),
            priority=z.get("priority", 1),
            price_minor=z.get("price_minor", 80_000),
        )
        for code, z in raw["zones"].items()
    }
    arrivals = raw.get("arrivals", {})
    stay = raw.get("stay_minutes", {})
    pricing = raw.get("pricing", {})
    return Scenario(
        name=raw["name"],
        zones=zones,
        entry_gates=raw.get("entry_gates", 2),
        exit_gates=raw.get("exit_gates", 1),
        arrivals=Arrivals(
            vehicles=arrivals.get("vehicles", 0),
            window_minutes=arrivals.get("window_minutes", 15),
            distribution=arrivals.get("distribution", "poisson"),
        ),
        prebooked_share=raw.get("mix", {}).get("prebooked", 0.0),
        stay_mean_minutes=stay.get("mean", 30),
        stay_stddev_minutes=stay.get("stddev", 5),
        lane_seconds=raw.get("lane_seconds", 8),
        time_scale=raw.get("time_scale", 60),
        free_minutes=pricing.get("free_minutes", 15),
        hourly_minor=pricing.get("hourly_minor", 30_000),
        payments=Payments(**raw.get("payments", {})),
        chaos=Chaos(**raw.get("chaos", {})),
        leave=raw.get("leave", True),
        presale_only=raw.get("presale_only", False),
        booking_concurrency=raw.get("booking_concurrency", 20),
    )
