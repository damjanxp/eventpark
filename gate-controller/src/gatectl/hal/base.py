"""Hardware Abstraction Layer: what the lane logic needs from the hardware, as interfaces (Protocols).

The state machine (machine.py) only talks to these interfaces, never to GPIO pins or USB devices. That's dependency
inversion: business logic depends on abstractions; the concrete hardware is plugged in at startup (main.py).
Today the implementations are simulated (simulated.py); a future rpi.py would drive real GPIO, a USB barcode
scanner and an ESC/POS printer, and nothing in machine.py would change.

Inputs (scanner, ticket button, loop detectors, card terminal, ticket-taken sensor) are not methods here: the hardware
reports them as `LaneEvent`s pushed into the machine's queue (see machine.py). These are the outputs + status.
"""

from enum import StrEnum
from typing import Protocol


class PrinterStatus(StrEnum):
    OK = "OK"
    PAPER_LOW = "PAPER_LOW"
    PAPER_OUT = "PAPER_OUT"


class Display(Protocol):
    def show(self, text: str) -> None:
        """What the driver reads on the lane screen."""
        ...


class Barrier(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


class Printer(Protocol):
    def print_ticket(self, ticket_code: str, zone: str, entered_at: str) -> None:
        """Print a drive-up ticket; it stays in the slot until the driver pulls it out (ticket-taken sensor)."""
        ...

    @property
    def status(self) -> PrinterStatus: ...


class Hardware(Protocol):
    """Everything one lane has. Exit lanes have no printer. (Read-only properties: any object exposing these fits.)"""

    @property
    def display(self) -> Display: ...

    @property
    def barrier(self) -> Barrier: ...

    @property
    def printer(self) -> Printer | None: ...
