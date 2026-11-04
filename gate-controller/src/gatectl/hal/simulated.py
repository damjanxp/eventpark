"""In-memory hardware for tests, the simulator and interactive mode. Records everything it was asked to do."""

from collections.abc import Callable
from dataclasses import dataclass, field

from gatectl.hal.base import PrinterStatus


@dataclass
class SimDisplay:
    lines: list[str] = field(default_factory=list)
    on_show: Callable[[str], None] | None = None

    def show(self, text: str) -> None:
        self.lines.append(text)
        if self.on_show:
            self.on_show(text)

    @property
    def current(self) -> str:
        return self.lines[-1] if self.lines else ""


@dataclass
class SimBarrier:
    opened: int = 0  # how many times it was opened (tests assert "never opened")
    _open: bool = False

    def open(self) -> None:
        self.opened += 1
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


@dataclass
class SimPrinter:
    printed: list[tuple[str, str, str]] = field(default_factory=list)
    paper_left: int = 500
    low_threshold: int = 50

    def print_ticket(self, ticket_code: str, zone: str, entered_at: str) -> None:
        if self.paper_left <= 0:
            raise RuntimeError("printer is out of paper")
        self.paper_left -= 1
        self.printed.append((ticket_code, zone, entered_at))

    @property
    def status(self) -> PrinterStatus:
        if self.paper_left <= 0:
            return PrinterStatus.PAPER_OUT
        if self.paper_left <= self.low_threshold:
            return PrinterStatus.PAPER_LOW
        return PrinterStatus.OK


@dataclass
class SimHardware:
    display: SimDisplay = field(default_factory=SimDisplay)
    barrier: SimBarrier = field(default_factory=SimBarrier)
    printer: SimPrinter | None = field(default_factory=SimPrinter)
