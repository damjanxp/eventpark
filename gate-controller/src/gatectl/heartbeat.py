"""Heartbeat: every 5 s tell the server "I'm alive" (+ printer status), and pick up commands waiting for this lane.

The server marks the device offline after 60 s without a heartbeat. Commands (e.g. an operator's OPEN_BARRIER) ride on
the heartbeat *response*: a simple pull-based channel that works through any firewall/NAT, because the device always
starts the connection. Each command is acknowledged after it was carried out.
"""

import asyncio
import logging

from gatectl.client import GateApiError, GateApiUnavailable, GateClient
from gatectl.machine import Ev, Lane, LaneEvent

log = logging.getLogger("gatectl.heartbeat")


async def beat_once(lane: Lane, api: GateClient, firmware_version: str) -> None:
    printer = lane.hw.printer
    response = await api.heartbeat(firmware_version, printer.status.value if printer else None)
    for command in response.get("commands", []):
        if command.get("command") == "OPEN_BARRIER":
            lane.post(LaneEvent(Ev.MANUAL_OPEN, command.get("reason", "")))
        await api.ack(str(command["id"]))  # handed to the lane: the server won't deliver it again
    await lane.flush_unsent()  # we're online: send completions that failed earlier


async def heartbeat_loop(lane: Lane, api: GateClient, *, firmware_version: str, interval: float = 5.0) -> None:
    while True:
        try:
            await beat_once(lane, api, firmware_version)
        except (GateApiUnavailable, GateApiError) as exc:
            log.warning("heartbeat.failed %r", exc)  # keep trying; the lane keeps working for whatever it can
        await asyncio.sleep(interval)
