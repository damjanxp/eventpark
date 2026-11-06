"""Markdown report: throughput, latency percentiles per endpoint, decisions, lane queues, invariants."""

from datetime import UTC, datetime
from pathlib import Path

from simulator.invariants import Check
from simulator.metrics import Metrics, percentile
from simulator.scenario import Scenario


def _ms(v: float) -> str:
    return "-" if v != v else f"{v:.0f}"  # NaN check without math import


def render(
    scenario: Scenario, metrics: Metrics, checks: list[Check], *, duration_s: float, chaos_log: list[str]
) -> str:
    lines = [
        f"# Simulation report: `{scenario.name}`",
        "",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M UTC} · {duration_s:.1f} s real · time scale {scenario.time_scale:g}x "
        f"({scenario.entry_gates} entry / {scenario.exit_gates} exit lanes, "
        f"lane time {scenario.lane_seconds:g} s simulated)",
        "",
        "## Outcome",
        "",
        "| | |",
        "|---|---|",
        f"| vehicles | {scenario.arrivals.vehicles} in {scenario.arrivals.window_minutes:g} simulated min |",
        f"| entered / turned away | {metrics.entered} / {metrics.turned_away} |",
        f"| exited / paid at exit | {metrics.exited} / {metrics.payments_ok} |",
        f"| stuck (needed staff) | {metrics.stuck} |",
        f"| bookings confirmed / failed | {metrics.bookings_confirmed} / {metrics.bookings_failed} |",
        f"| responses dropped by chaos | {metrics.dropped_responses} |",
        "",
        "## Latency per endpoint (ms, as the lane saw it: retries included)",
        "",
        "| endpoint | n | p50 | p95 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, values in sorted(metrics.latency_ms.items()):
        lines.append(
            f"| {name} | {len(values)} | {_ms(percentile(values, 50))} | {_ms(percentile(values, 95))} | "
            f"{_ms(percentile(values, 99))} | {_ms(max(values))} |"
        )
    total_calls = sum(len(v) for v in metrics.latency_ms.values())
    lines += ["", f"Throughput: {total_calls / duration_s:.1f} requests/s over the run.", ""]

    lines += [
        "## Lane queues (simulated seconds a car waited before its turn)",
        "",
        "API time is real time, so at a time scale of N each real second spent waiting for the API counts as N "
        "simulated seconds here: slow endpoints (card payments) dominate the queues.",
        "",
        "| direction | n | p50 | p95 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for direction, waits in sorted(metrics.queue_wait_s.items()):
        if waits:
            lines.append(
                f"| {direction} | {len(waits)} | {percentile(waits, 50):.0f} | {percentile(waits, 95):.0f} | "
                f"{percentile(waits, 99):.0f} | {max(waits):.0f} |"
            )
    arrival_rate = scenario.arrivals.vehicles / max(1e-9, scenario.arrivals.window_minutes * 60)
    busy = arrival_rate * scenario.lane_seconds
    verdict = "queues must grow: the lanes are the bottleneck" if busy > scenario.entry_gates else "enough lanes"
    lines += [
        "",
        f"Little's Law: {arrival_rate:.2f} cars/s arriving x ~{scenario.lane_seconds:g} s per car "
        f"≈ {busy:.1f} lanes busy on average, with {scenario.entry_gates} entry lanes ({verdict}).",
        "",
        "## Decisions",
        "",
        ", ".join(f"{k}: {v}" for k, v in metrics.decisions.most_common()) or "-",
        "",
    ]
    if metrics.reasons:
        lines += ["Rejections: " + ", ".join(f"{k}: {v}" for k, v in metrics.reasons.most_common()), ""]
    if metrics.errors:
        lines += ["Errors: " + ", ".join(f"{k}: {v}" for k, v in metrics.errors.most_common()), ""]
    if chaos_log:
        lines += ["## Chaos actions", "", *[f"- {c}" for c in chaos_log], ""]
    passed = sum(c.ok for c in checks)
    lines += [
        f"## Invariants: {passed}/{len(checks)} passed",
        "",
        "| group | check | result | detail |",
        "|---|---|---|---|",
    ]
    for c in checks:
        lines.append(f"| {c.group} | {c.name} | {'✅' if c.ok else '❌'} | {c.detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def write(text: str, scenario: Scenario, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario.name}-{datetime.now(UTC):%Y%m%d-%H%M%S}.md"
    path.write_text(text)
    return path
