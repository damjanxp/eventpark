"""What the run measured: latency per endpoint, decisions, lane waits. Percentiles, not averages (Phase 9)."""

import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least p% of the data at or below it."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class Metrics:
    started: float = field(default_factory=time.monotonic)
    latency_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    decisions: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    queue_wait_s: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))  # entry|exit, sim seconds
    # What the simulated cars believe happened. The invariant checker compares these with the database.
    entered: int = 0
    exited: int = 0
    turned_away: int = 0
    payments_ok: int = 0
    stuck: int = 0  # gave up (no usable answer): needs staff
    bookings_confirmed: int = 0
    bookings_failed: int = 0
    dropped_responses: int = 0

    def record(self, endpoint: str, ms: float) -> None:
        self.latency_ms[endpoint].append(ms)

    def elapsed(self) -> float:
        return time.monotonic() - self.started
