"""`python -m simulator run scenarios/smoke.yaml [--api http://localhost:8000]` (stack running, after `make seed`).

Exit code 0 only if every invariant holds, so CI can run the smoke scenario.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from simulator import booking_rush, chaos, invariants, report, world
from simulator.drivers import drive, make_cars, make_lanes
from simulator.metrics import Metrics
from simulator.scenario import Scenario, load


async def run(scenario: Scenario, api: str, seed: int, out: Path) -> bool:
    metrics = Metrics()
    admin = world.Admin(api)
    await admin.login()
    prebook = round(scenario.arrivals.vehicles * scenario.prebooked_share)
    if scenario.presale_only:
        prebook = scenario.arrivals.vehicles
    w = await world.build(admin, scenario, live=False)
    print(f"world: event {w.event_id} · {len(w.entry_keys)} entry / {len(w.exit_keys)} exit lanes")
    started = time.monotonic()

    if prebook:
        print(f"booking {prebook} places online...")
        w.prebooked_codes = await booking_rush.book(api, w, scenario, prebook, metrics, seed)
        print(f"  {len(w.prebooked_codes)} confirmed")
    chaos_log: list[str] = []
    if not scenario.presale_only:
        await world.go_live(admin, w)
        entries = make_lanes(api, w.entry_keys, "ENTRY", scenario, metrics, seed)
        exits = make_lanes(api, w.exit_keys, "EXIT", scenario, metrics, seed + 100)
        stop = asyncio.Event()
        workers = [asyncio.create_task(lane.run()) for lane in entries + exits]
        beats = [asyncio.create_task(lane.heartbeats(stop)) for lane in entries + exits]
        cars = make_cars(scenario, w.prebooked_codes, seed)
        gate_start = time.monotonic()

        async def chaos_actions() -> None:
            for action in sorted(scenario.chaos.actions, key=lambda a: float(a.get("at_seconds", 0))):
                await asyncio.sleep(max(0.0, gate_start + float(action.get("at_seconds", 0)) - time.monotonic()))
                line = await asyncio.to_thread(chaos.run_action, action)
                chaos_log.append(f"t={time.monotonic() - gate_start:.1f}s {line}")
                print(f"chaos: {chaos_log[-1]}")

        chaos_task = asyncio.create_task(chaos_actions())
        print(f"driving {len(cars)} cars...")
        await asyncio.gather(*(drive(c, entries, exits, scenario, gate_start) for c in cars))
        await chaos_task
        stop.set()
        for lane in entries + exits:
            await lane.queue.put(None)
        await asyncio.gather(*workers, *beats)
        for lane in entries + exits:
            await lane.client.aclose()
    await admin.aclose()
    duration = time.monotonic() - started
    print("checking invariants (waiting for the async side to settle)...")
    checks = await invariants.check(w.event_id, metrics)
    text = report.render(scenario, metrics, checks, duration_s=duration, chaos_log=chaos_log)
    path = report.write(text, scenario, out)
    failed = [c for c in checks if not c.ok]
    print(f"report: {path}")
    print(
        f"entered {metrics.entered}, exited {metrics.exited}, turned away {metrics.turned_away}, stuck {metrics.stuck}"
    )
    print(
        "invariants: "
        + ("all passed" if not failed else "FAILED: " + "; ".join(f"{c.name} ({c.detail})" for c in failed))
    )
    return not failed


def main() -> None:
    parser = argparse.ArgumentParser(prog="simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run a scenario, check invariants, write a report")
    r.add_argument("scenario")
    r.add_argument("--api", default="http://localhost:8000")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "reports"))
    c = sub.add_parser("check", help="only check the invariants of an existing event")
    c.add_argument("event_id")
    args = parser.parse_args()
    if args.command == "check":
        checks = asyncio.run(invariants.check(args.event_id, None, settle_seconds=5))
        for chk in checks:
            print(f"{'PASS' if chk.ok else 'FAIL'}  {chk.group:9} {chk.name}  {chk.detail}")
        sys.exit(0 if all(chk.ok for chk in checks) else 1)
    ok = asyncio.run(run(load(args.scenario), args.api, args.seed, Path(args.out)))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
