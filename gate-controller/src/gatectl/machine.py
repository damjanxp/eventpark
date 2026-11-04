"""The lane state machine: what one gate lane does, step by step (docs/02-architecture.md §10).

Entry lane:
    IDLE ─car on loop─▶ WAITING_FOR_INPUT ─scan/button─▶ REQUESTING ─OPEN─▶ BARRIER_OPEN ─car passed─▶ IDLE
                              │                               │                  │
                              └─car left─▶ IDLE             DENY ─▶ message     └─30 s, no car─▶ abort ─▶ IDLE
    drive-up:  REQUESTING ─OPEN─▶ PRINTING ─ticket taken─▶ BARRIER_OPEN   (barrier opens only when the ticket is pulled)
Exit lane adds:  REQUESTING ─PAYMENT_REQUIRED─▶ AWAITING_PAYMENT ─card─▶ CHARGING ─OPEN─▶ BARRIER_OPEN

Rules:
- Input (scan, button, card) only counts while a car is on the arming loop and the lane is waiting for it. Otherwise
  it's ignored locally and the server is never asked (a pedestrian can't pull tickets).
- FAIL CLOSED: the barrier opens only on an OPEN decision (or an operator's manual-open command). No answer from the
  server = barrier stays shut + "please call staff". A shut barrier is annoying; an open one gives away free parking.
- One action id per driver action, reused for every retry of it (also across "call staff" and a second try), so a
  retry can never take a second place or charge a card twice.
- Events are handled one at a time from a single queue: two inputs can never race inside the lane.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gatectl.client import GateApiError, GateApiUnavailable, GateClient, new_action_id
from gatectl.hal.base import Hardware, PrinterStatus

log = logging.getLogger("gatectl.machine")


class LaneKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class State(StrEnum):
    IDLE = "IDLE"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    REQUESTING = "REQUESTING"
    PRINTING = "PRINTING"
    BARRIER_OPEN = "BARRIER_OPEN"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    CHARGING = "CHARGING"


class Ev(StrEnum):
    CAR_ARRIVED = "car_arrived"  # arming loop (in front of the barrier) detects a vehicle
    CAR_LEFT = "car_left"  # arming loop cleared without passing (driver backed off)
    SCAN = "scan"  # value = ticket code read by the QR/barcode scanner
    BUTTON = "button"  # "press for ticket"
    TICKET_TAKEN = "ticket_taken"  # ticket-taken sensor in the printer slot
    CAR_PASSED = "car_passed"  # loop after the barrier: the car drove through
    CAR_REVERSED = "car_reversed"  # barrier open, but the car reversed away
    CARD = "card"  # value = card token from the terminal
    MANUAL_OPEN = "manual_open"  # value = reason; operator command delivered by the heartbeat
    TIMEOUT = "timeout"  # value = timer generation (internal)


@dataclass(frozen=True)
class LaneEvent:
    kind: Ev
    value: Any = None


TEXT = {
    "idle": "EventPark - welcome",
    "entry_prompt": "Scan your ticket or press for a ticket",
    "exit_prompt": "Please scan your ticket",
    "checking": "One moment...",
    "no_paper": "No tickets - please use another lane",
    "call_staff": "Please call staff (button on the left)",
    "lane_closed": "Lane closed - please use another lane",
    "goodbye": "Have a nice day!",
    "declined": "Card declined - please try another card",
    "charging": "Processing payment...",
    "payment_wait": "Please wait...",
    "manual": "Barrier opened by staff",
}


@dataclass
class LaneConfig:
    kind: LaneKind
    barrier_timeout: float = 30.0  # barrier open (or ticket printed) and nothing happens -> abort
    payment_poll_delay: float = 1.0  # PAYMENT_PENDING -> ask again after this long
    payment_poll_attempts: int = 5


class Lane:
    def __init__(self, cfg: LaneConfig, hw: Hardware, api: GateClient) -> None:
        self.cfg, self.hw, self.api = cfg, hw, api
        self.state = State.IDLE
        self.queue: asyncio.Queue[LaneEvent] = asyncio.Queue()
        self.passage_id: str | None = None
        self.manual = False
        # Idempotency keys of actions whose outcome we don't know yet (server unreachable): reused on the next try.
        self._pending_scan: tuple[str, str] | None = None  # (ticket code, scan_id)
        self._pending_issue: str | None = None
        self._pending_pay: str | None = None
        # Completions/aborts that couldn't be sent: the car is through, the server must still hear about it.
        self.unsent: list[tuple[str, str]] = []  # (complete|abort, passage_id)
        self._timer_generation = 0
        self._timer: asyncio.Task[None] | None = None
        self.hw.display.show(TEXT["idle"])

    # --- plumbing ---

    def post(self, event: LaneEvent) -> None:
        self.queue.put_nowait(event)

    async def run(self) -> None:
        """Handle events forever, one at a time."""
        while True:
            event = await self.queue.get()
            try:
                await self.handle(event)
            except Exception:  # a bug must not kill the lane; log it and keep serving
                log.exception("lane.handler_failed", extra={"event": event.kind.value})

    def _to(self, state: State) -> None:
        log.info("lane.state %s -> %s", self.state.value, state.value)
        self.state = state

    def _start_timer(self, seconds: float) -> None:
        self._cancel_timer()
        self._timer_generation += 1
        generation = self._timer_generation

        async def fire() -> None:
            await asyncio.sleep(seconds)
            self.post(LaneEvent(Ev.TIMEOUT, generation))

        try:
            self._timer = asyncio.get_running_loop().create_task(fire())
        except RuntimeError:  # no loop (unit tests calling handle() synchronously): tests post TIMEOUT themselves
            self._timer = None

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    @property
    def timer_generation(self) -> int:
        return self._timer_generation

    def _prompt(self) -> str:
        return TEXT["entry_prompt"] if self.cfg.kind is LaneKind.ENTRY else TEXT["exit_prompt"]

    def _reset(self) -> None:
        self._cancel_timer()
        self.passage_id, self.manual = None, False
        self._pending_scan = self._pending_issue = self._pending_pay = None

    # --- the machine ---

    async def handle(self, event: LaneEvent) -> None:
        kind = event.kind
        if kind is Ev.MANUAL_OPEN:
            return self._manual_open(str(event.value or ""))
        if kind is Ev.TIMEOUT:
            if event.value == self._timer_generation:
                await self._timed_out()
            return None

        match self.state:
            case State.IDLE:
                if kind is Ev.CAR_ARRIVED:
                    self._to(State.WAITING_FOR_INPUT)
                    self.hw.display.show(self._prompt())
                # anything else without a car on the arming loop: ignored, the server is never asked
            case State.WAITING_FOR_INPUT:
                if kind is Ev.CAR_LEFT:
                    self._reset()
                    self._to(State.IDLE)
                    self.hw.display.show(TEXT["idle"])
                elif kind is Ev.SCAN:
                    await self._scan(str(event.value))
                elif kind is Ev.BUTTON and self.cfg.kind is LaneKind.ENTRY:
                    await self._issue()
            case State.PRINTING:
                if kind is Ev.TICKET_TAKEN:
                    self._cancel_timer()
                    self._open_barrier()
            case State.BARRIER_OPEN:
                if kind is Ev.CAR_PASSED:
                    await self._car_passed()
                elif kind is Ev.CAR_REVERSED:
                    await self._release("abort")
            case State.AWAITING_PAYMENT:
                if kind is Ev.CARD:
                    await self._pay(str(event.value))
                elif kind is Ev.CAR_LEFT:
                    self._reset()
                    self._to(State.IDLE)
                    self.hw.display.show(TEXT["idle"])
            case State.REQUESTING | State.CHARGING:
                pass  # busy: inputs while we wait for the server are ignored
        return None

    async def _call(self, coro: Any) -> dict[str, Any] | None:
        """Run one API call; on failure show the right message and return None. The barrier stays shut."""
        try:
            result: dict[str, Any] = await coro
            return result
        except GateApiUnavailable:
            log.warning("lane.api_unavailable")
            self.hw.display.show(TEXT["call_staff"])
        except GateApiError as exc:
            log.error("lane.api_error status=%s code=%s", exc.status, exc.code)
            self.hw.display.show(TEXT["lane_closed"] if exc.status in (401, 403) else TEXT["call_staff"])
        return None

    async def _scan(self, code: str) -> None:
        # Same ticket again after an unanswered attempt -> same scan_id (the server may already have opened for it).
        scan_id = self._pending_scan[1] if self._pending_scan and self._pending_scan[0] == code else new_action_id()
        self._pending_scan = (code, scan_id)
        self._to(State.REQUESTING)
        self.hw.display.show(TEXT["checking"])
        call = self.api.entry_scan if self.cfg.kind is LaneKind.ENTRY else self.api.exit_scan
        decision = await self._call(call(scan_id, code))
        if decision is None:
            self._to(State.WAITING_FOR_INPUT)
            return
        self._pending_scan = None
        await self._apply(decision)

    async def _issue(self) -> None:
        printer = self.hw.printer
        if printer is None:
            return
        if printer.status is PrinterStatus.PAPER_OUT:
            self.hw.display.show(TEXT["no_paper"])  # decided locally, no server call: QR scans still work here
            return
        request_id = self._pending_issue or new_action_id()
        self._pending_issue = request_id
        self._to(State.REQUESTING)
        self.hw.display.show(TEXT["checking"])
        decision = await self._call(self.api.entry_issue(request_id))
        if decision is None:
            self._to(State.WAITING_FOR_INPUT)
            return
        self._pending_issue = None
        await self._apply(decision)

    async def _apply(self, d: dict[str, Any]) -> None:
        self.hw.display.show(str(d.get("display_text", "")))
        decision = d.get("decision")
        if decision == "OPEN":
            self.passage_id = str(d["passage_id"])
            ticket = d.get("print")
            if ticket and self.hw.printer is not None:
                self.hw.printer.print_ticket(ticket["ticket_code"], ticket["zone"], ticket["entered_at"])
                self._to(State.PRINTING)  # barrier opens when the ticket is pulled out of the slot
                self._start_timer(self.cfg.barrier_timeout)
            else:
                self._open_barrier()
        elif decision == "PAYMENT_REQUIRED":
            self.passage_id = str(d["passage_id"])
            self._to(State.AWAITING_PAYMENT)
        else:  # DENY (or anything unexpected): never open
            self._to(State.WAITING_FOR_INPUT)

    def _open_barrier(self) -> None:
        self.hw.barrier.open()
        self._to(State.BARRIER_OPEN)
        self._start_timer(self.cfg.barrier_timeout)

    async def _pay(self, card_token: str) -> None:
        if self.passage_id is None:  # pragma: no cover (AWAITING_PAYMENT always has one)
            return
        request_id = self._pending_pay or new_action_id()  # outcome unknown last time -> SAME id (no double charge)
        self._pending_pay = request_id
        self._to(State.CHARGING)
        self.hw.display.show(TEXT["charging"])
        for _ in range(self.cfg.payment_poll_attempts):
            result = await self._call(self.api.exit_pay(request_id, self.passage_id, card_token))
            if result is None:
                break
            decision = result.get("decision")
            if decision == "PAYMENT_PENDING":  # provider slow: ask again with the same id
                self.hw.display.show(TEXT["payment_wait"])
                await asyncio.sleep(self.cfg.payment_poll_delay)
                continue
            self._pending_pay = None
            if decision == "OPEN":
                self.hw.display.show(str(result.get("display_text") or TEXT["goodbye"]))
                self.passage_id = str(result.get("passage_id") or self.passage_id)
                self._open_barrier()
                return
            self.hw.display.show(TEXT["declined"] if decision == "DECLINED" else str(result.get("display_text", "")))
            self._to(State.AWAITING_PAYMENT)
            return
        else:
            self.hw.display.show(TEXT["call_staff"])  # still pending after polling: staff decides
        self._to(State.AWAITING_PAYMENT)

    async def _car_passed(self) -> None:
        self._cancel_timer()
        self.hw.barrier.close()  # the safety loop keeps it from closing onto a car (barrier hardware)
        self.hw.display.show(TEXT["goodbye"])
        passage_id, manual = self.passage_id, self.manual
        self._reset()
        self._to(State.IDLE)
        if passage_id and not manual:
            await self._send("complete", passage_id)

    async def _release(self, action: str) -> None:
        self._cancel_timer()
        self.hw.barrier.close()
        passage_id, manual = self.passage_id, self.manual
        self._reset()
        self._to(State.IDLE)
        self.hw.display.show(TEXT["idle"])
        if passage_id and not manual:
            await self._send(action, passage_id)

    async def _timed_out(self) -> None:
        if self.state in (State.BARRIER_OPEN, State.PRINTING):
            log.info("lane.timeout_no_car state=%s", self.state.value)
            await self._release("abort")

    async def _send(self, action: str, passage_id: str) -> None:
        """complete/abort: the car already moved, so failures are queued and retried (by the heartbeat loop)."""
        call = self.api.complete if action == "complete" else self.api.abort
        try:
            await call(passage_id)
        except (GateApiUnavailable, GateApiError) as exc:
            if isinstance(exc, GateApiError) and exc.status < 500 and exc.status not in (408, 429):
                log.error("lane.%s_refused passage=%s code=%s", action, passage_id, exc.code)
                return  # e.g. passage already expired: nothing to retry, staff sees it on the dashboard
            self.unsent.append((action, passage_id))

    async def flush_unsent(self) -> None:
        pending, self.unsent = self.unsent, []
        for action, passage_id in pending:
            await self._send(action, passage_id)

    def _manual_open(self, reason: str) -> None:
        log.info("lane.manual_open reason=%s", reason)
        self.manual = True
        self.hw.display.show(TEXT["manual"])
        self.hw.barrier.open()
        self._to(State.BARRIER_OPEN)
        self._start_timer(self.cfg.barrier_timeout)
