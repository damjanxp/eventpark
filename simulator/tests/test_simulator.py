"""Pure parts of the simulator (no stack needed): scenario loading, arrivals, percentiles, Little's Law text."""

from pathlib import Path

import pytest

from simulator.drivers import make_cars
from simulator.invariants import Check
from simulator.metrics import Metrics, percentile
from simulator.report import render
from simulator.scenario import load

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


@pytest.mark.parametrize("path", sorted(SCENARIOS.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_scenario_loads(path: Path) -> None:
    scenario = load(path)
    assert scenario.name == path.stem
    assert scenario.zones


def test_nearest_rank_percentiles() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([7.0], 99) == 7
    assert percentile([], 50) != percentile([], 50)  # NaN


def test_cars_arrive_inside_the_window_in_order_with_the_booked_codes() -> None:
    scenario = load(SCENARIOS / "smoke.yaml")
    codes = [f"EP1-{i}" for i in range(10)]
    cars = make_cars(scenario, codes, seed=1)
    window = scenario.real_seconds(scenario.arrivals.window_minutes)
    assert len(cars) == scenario.arrivals.vehicles
    assert all(0 <= c.arrive_after_s <= window for c in cars)
    assert [c.arrive_after_s for c in cars] == sorted(c.arrive_after_s for c in cars)
    assert sorted(c.prebooked_code for c in cars if c.prebooked_code) == sorted(codes)  # every booking is used once
    assert make_cars(scenario, codes, seed=1)[5].stay_s == cars[5].stay_s  # same seed, same run


def test_report_flags_failed_invariants_and_lane_bottlenecks() -> None:
    scenario = load(SCENARIOS / "rush-2000-in-15.yaml")
    metrics = Metrics()
    metrics.record("entry/issue", 20)
    text = render(scenario, metrics, [Check("capacity", "x", False, "A over")], duration_s=10, chaos_log=[])
    assert "Invariants: 0/1 passed" in text and "❌" in text
    assert "lanes are the bottleneck" in text  # 2.2 cars/s x 8 s = 17.8 busy lanes > 6
