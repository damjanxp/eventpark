"""Run one lane: `uv run gatectl` (config from env), driven from the keyboard.

    GATE_API_URL   http://localhost:8000
    GATE_API_KEY   epd_... (from `make seed` -> .devices.local.json, or POST /admin/v1/gates/{id}/device)
    GATE_LANE      entry | exit
    GATE_FIRMWARE  version string reported in heartbeats (default: the package version)

Keys (type, then Enter):
    c          car arrives on the arming loop        l          car leaves without passing
    s CODE     scan a ticket code                     b          press the ticket button (entry lanes)
    t          driver pulls the printed ticket        p          car passes the barrier
    r          car reverses away                      k TOKEN    tap a card (tok_ok, tok_decline, tok_slow)
    q          quit
"""

import asyncio
import logging
import os
import sys

from gatectl.client import GateClient
from gatectl.hal.simulated import SimDisplay, SimHardware, SimPrinter
from gatectl.heartbeat import heartbeat_loop
from gatectl.machine import Ev, Lane, LaneConfig, LaneEvent, LaneKind

KEYS = {
    "c": Ev.CAR_ARRIVED,
    "l": Ev.CAR_LEFT,
    "s": Ev.SCAN,
    "b": Ev.BUTTON,
    "t": Ev.TICKET_TAKEN,
    "p": Ev.CAR_PASSED,
    "r": Ev.CAR_REVERSED,
    "k": Ev.CARD,
}


def parse_key(line: str) -> LaneEvent | None:
    parts = line.strip().split(maxsplit=1)
    if not parts or parts[0] not in KEYS:
        return None
    value = parts[1] if len(parts) > 1 else None
    return LaneEvent(KEYS[parts[0]], value)


async def interactive(lane: Lane) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)  # stdin is blocking: read it in a thread
        if not line or line.strip() == "q":
            return
        event = parse_key(line)
        if event is None:
            print(__doc__.split("Keys")[1])
            continue
        lane.post(event)


async def amain() -> None:
    logging.basicConfig(level=os.environ.get("GATE_LOG_LEVEL", "WARNING"), format="%(asctime)s %(name)s %(message)s")
    kind = LaneKind(os.environ.get("GATE_LANE", "entry"))
    api = GateClient(os.environ.get("GATE_API_URL", "http://localhost:8000"), os.environ["GATE_API_KEY"])
    display = SimDisplay(on_show=lambda text: print(f"  ┃ DISPLAY: {text}"))
    hw = SimHardware(display=display, printer=SimPrinter() if kind is LaneKind.ENTRY else None)
    lane = Lane(LaneConfig(kind=kind), hw, api)
    print(f"{kind.value} lane ready. Keys: c s b t p r l k q (any other key: help)")
    tasks = [
        asyncio.create_task(lane.run()),
        asyncio.create_task(
            heartbeat_loop(lane, api, firmware_version=os.environ.get("GATE_FIRMWARE", "gatectl-0.1.0"))
        ),
    ]
    try:
        await interactive(lane)
    finally:
        for task in tasks:
            task.cancel()
        await api.aclose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
