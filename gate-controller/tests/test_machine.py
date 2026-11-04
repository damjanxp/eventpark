"""Lane state machine with a mocked API: the behaviour a driver experiences."""

import json
import random
from typing import Any

import httpx
import pytest
import respx

from gatectl.client import GateClient, RetryPolicy
from gatectl.hal.simulated import SimHardware, SimPrinter
from gatectl.heartbeat import beat_once
from gatectl.machine import TEXT, Ev, Lane, LaneConfig, LaneEvent, LaneKind, State

BASE = "http://api.test"
OPEN = {"decision": "OPEN", "passage_id": "p-1", "display_text": "Welcome! Zone A", "zone": "A"}
DENY = {
    "decision": "DENY",
    "reason": "ALREADY_INSIDE",
    "passage_id": "p-x",
    "display_text": "This ticket is already inside",
}


def lane(kind: LaneKind = LaneKind.ENTRY, printer: SimPrinter | None = None) -> tuple[Lane, SimHardware]:
    hw = SimHardware(printer=printer if printer is not None else (SimPrinter() if kind is LaneKind.ENTRY else None))
    api = GateClient(BASE, "epd_0123456789ab_secret", retry=RetryPolicy(base_delay=0.0, rng=random.Random(1)))
    return Lane(LaneConfig(kind=kind, payment_poll_delay=0.0), hw, api), hw


async def ev(machine: Lane, kind: Ev, value: Any = None) -> None:
    await machine.handle(LaneEvent(kind, value))


# --- entry with a QR code ---


@respx.mock
async def test_entry_scan_open_then_car_passes_completes() -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=OPEN))
    complete = respx.post(f"{BASE}/gate/v1/passages/p-1/complete").mock(return_value=httpx.Response(200, json={}))
    machine, hw = lane()

    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")
    assert hw.barrier.is_open and machine.state is State.BARRIER_OPEN
    assert hw.display.current == "Welcome! Zone A"

    await ev(machine, Ev.CAR_PASSED)
    assert not hw.barrier.is_open and machine.state is State.IDLE
    assert complete.called


@respx.mock
async def test_deny_never_opens_the_barrier() -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=DENY))
    machine, hw = lane()

    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")

    assert hw.barrier.opened == 0
    assert hw.display.current == "This ticket is already inside"
    assert machine.state is State.WAITING_FOR_INPUT  # driver may try another ticket


@respx.mock
async def test_input_without_a_car_is_ignored_and_never_reaches_the_server() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=OPEN))
    issue = respx.post(f"{BASE}/gate/v1/entry/issue").mock(return_value=httpx.Response(200, json=OPEN))
    machine, hw = lane()

    await ev(machine, Ev.SCAN, "EP1-AAA")  # pedestrian waves a ticket
    await ev(machine, Ev.BUTTON)  # pedestrian presses the button

    assert not route.called and not issue.called and hw.barrier.opened == 0


@respx.mock
async def test_api_down_fails_closed_and_the_retry_reuses_the_scan_id() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/scan").mock(
        side_effect=[httpx.ConnectError("down")] * 4 + [httpx.Response(200, json=OPEN)]
    )
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.SCAN, "EP1-AAA")
    assert hw.barrier.opened == 0  # fail closed
    assert hw.display.current == TEXT["call_staff"]
    assert machine.state is State.WAITING_FOR_INPUT

    await ev(machine, Ev.SCAN, "EP1-AAA")  # driver tries again once the API is back
    assert hw.barrier.is_open
    ids = {json.loads(c.request.content)["scan_id"] for c in route.calls}
    assert len(ids) == 1  # all 5 requests (4 failed + 1 ok) carried the same scan_id


@respx.mock
async def test_a_different_ticket_gets_a_new_scan_id() -> None:
    route = respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=DENY))
    machine, _ = lane()
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.SCAN, "EP1-AAA")
    await ev(machine, Ev.SCAN, "EP1-BBB")

    ids = [json.loads(c.request.content)["scan_id"] for c in route.calls]
    assert ids[0] != ids[1]


@respx.mock
async def test_barrier_open_but_no_car_times_out_and_aborts() -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=OPEN))
    abort = respx.post(f"{BASE}/gate/v1/passages/p-1/abort").mock(return_value=httpx.Response(200, json={}))
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")

    await ev(machine, Ev.TIMEOUT, machine.timer_generation - 1)  # a stale timer is ignored
    assert hw.barrier.is_open
    await ev(machine, Ev.TIMEOUT, machine.timer_generation)

    assert abort.called and not hw.barrier.is_open and machine.state is State.IDLE


@respx.mock
async def test_car_reverses_aborts() -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=OPEN))
    abort = respx.post(f"{BASE}/gate/v1/passages/p-1/abort").mock(return_value=httpx.Response(200, json={}))
    machine, _ = lane()
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")

    await ev(machine, Ev.CAR_REVERSED)

    assert abort.called and machine.state is State.IDLE


async def test_car_leaves_before_scanning() -> None:
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.CAR_LEFT)

    assert machine.state is State.IDLE and hw.display.current == TEXT["idle"]


# --- drive-up ticket ---


@respx.mock
async def test_drive_up_prints_and_opens_only_when_ticket_taken() -> None:
    issued = {
        **OPEN,
        "display_text": "Please take your ticket",
        "print": {"ticket_code": "EP1-T", "zone": "A", "entered_at": "now"},
    }
    respx.post(f"{BASE}/gate/v1/entry/issue").mock(return_value=httpx.Response(200, json=issued))
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.BUTTON)
    assert hw.printer is not None and hw.printer.printed == [("EP1-T", "A", "now")]
    assert machine.state is State.PRINTING and hw.barrier.opened == 0  # not yet!

    await ev(machine, Ev.TICKET_TAKEN)
    assert hw.barrier.is_open and machine.state is State.BARRIER_OPEN


@respx.mock
async def test_paper_out_refuses_locally() -> None:
    issue = respx.post(f"{BASE}/gate/v1/entry/issue").mock(return_value=httpx.Response(200, json=OPEN))
    machine, hw = lane(printer=SimPrinter(paper_left=0))
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.BUTTON)

    assert not issue.called
    assert hw.display.current == TEXT["no_paper"]


@respx.mock
async def test_unclaimed_printed_ticket_times_out_and_aborts() -> None:
    issued = {**OPEN, "print": {"ticket_code": "EP1-T", "zone": "A", "entered_at": "now"}}
    respx.post(f"{BASE}/gate/v1/entry/issue").mock(return_value=httpx.Response(200, json=issued))
    abort = respx.post(f"{BASE}/gate/v1/passages/p-1/abort").mock(return_value=httpx.Response(200, json={}))
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.BUTTON)

    await ev(machine, Ev.TIMEOUT, machine.timer_generation)

    assert abort.called and hw.barrier.opened == 0


# --- exit with payment ---


@respx.mock
async def test_exit_payment_declined_then_ok_reuses_nothing_after_a_definite_answer() -> None:
    respx.post(f"{BASE}/gate/v1/exit/scan").mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": "PAYMENT_REQUIRED",
                "passage_id": "p-1",
                "amount_minor": 120000,
                "display_text": "Please pay 1,200.00 RSD",
            },
        )
    )
    pay = respx.post(f"{BASE}/gate/v1/exit/pay").mock(
        side_effect=[
            httpx.Response(200, json={"decision": "DECLINED", "passage_id": "p-1", "display_text": "Card declined"}),
            httpx.Response(200, json={"decision": "OPEN", "passage_id": "p-1", "display_text": "Paid, goodbye!"}),
        ]
    )
    machine, hw = lane(LaneKind.EXIT)
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")
    assert machine.state is State.AWAITING_PAYMENT and hw.display.current == "Please pay 1,200.00 RSD"

    await ev(machine, Ev.CARD, "tok_decline")
    assert machine.state is State.AWAITING_PAYMENT and hw.display.current == TEXT["declined"] and hw.barrier.opened == 0
    await ev(machine, Ev.CARD, "tok_ok")

    assert hw.barrier.is_open
    ids = [json.loads(c.request.content)["request_id"] for c in pay.calls]
    assert ids[0] != ids[1]  # declined is a definite answer: the next card is a new action


@respx.mock
async def test_exit_payment_unknown_outcome_retries_with_the_same_request_id() -> None:
    """No answer from the server: did the card get charged? Maybe. The next tap MUST reuse the id (no double charge)."""
    respx.post(f"{BASE}/gate/v1/exit/scan").mock(
        return_value=httpx.Response(
            200, json={"decision": "PAYMENT_REQUIRED", "passage_id": "p-1", "amount_minor": 1, "display_text": "Pay"}
        )
    )
    pay = respx.post(f"{BASE}/gate/v1/exit/pay").mock(
        side_effect=[httpx.ReadTimeout("slow")] * 4
        + [httpx.Response(200, json={"decision": "PAYMENT_PENDING", "display_text": "wait"})]
        + [httpx.Response(200, json={"decision": "OPEN", "passage_id": "p-1", "display_text": "Paid"})]
    )
    machine, hw = lane(LaneKind.EXIT)
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")

    await ev(machine, Ev.CARD, "tok_slow")
    assert hw.display.current == TEXT["call_staff"] and hw.barrier.opened == 0
    await ev(machine, Ev.CARD, "tok_slow")

    assert hw.barrier.is_open
    assert len({json.loads(c.request.content)["request_id"] for c in pay.calls}) == 1


# --- operator manual open via heartbeat ---


@respx.mock
async def test_heartbeat_delivers_manual_open_and_acks() -> None:
    respx.post(f"{BASE}/gate/v1/heartbeat").mock(
        return_value=httpx.Response(
            200, json={"commands": [{"id": "c-1", "command": "OPEN_BARRIER", "reason": "stuck"}]}
        )
    )
    ack = respx.post(f"{BASE}/gate/v1/commands/c-1/ack").mock(return_value=httpx.Response(204))
    complete = respx.post(url__regex=r".*/complete").mock(return_value=httpx.Response(200, json={}))
    machine, hw = lane()

    await beat_once(machine, machine.api, "1.0")
    await machine.handle(machine.queue.get_nowait())
    assert ack.called and hw.barrier.is_open and hw.display.current == TEXT["manual"]

    await ev(machine, Ev.CAR_PASSED)
    assert not complete.called  # a manual opening has no passage of ours to complete


@respx.mock
async def test_failed_completion_is_retried_by_the_next_heartbeat() -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(200, json=OPEN))
    complete = respx.post(f"{BASE}/gate/v1/passages/p-1/complete").mock(
        side_effect=[httpx.ConnectError("down")] * 4 + [httpx.Response(200, json={})]
    )
    respx.post(f"{BASE}/gate/v1/heartbeat").mock(return_value=httpx.Response(200, json={"commands": []}))
    machine, _ = lane()
    await ev(machine, Ev.CAR_ARRIVED)
    await ev(machine, Ev.SCAN, "EP1-AAA")

    await ev(machine, Ev.CAR_PASSED)
    assert machine.unsent == [("complete", "p-1")]
    await beat_once(machine, machine.api, "1.0")

    assert machine.unsent == [] and complete.call_count == 5


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_device_refused_shows_lane_closed(status: int) -> None:
    respx.post(f"{BASE}/gate/v1/entry/scan").mock(return_value=httpx.Response(status, json={"code": "DEVICE_DISABLED"}))
    machine, hw = lane()
    await ev(machine, Ev.CAR_ARRIVED)

    await ev(machine, Ev.SCAN, "EP1-AAA")

    assert hw.display.current == TEXT["lane_closed"] and hw.barrier.opened == 0
