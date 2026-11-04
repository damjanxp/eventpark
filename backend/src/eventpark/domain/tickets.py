"""Tickets and passages (docs/01-product-and-domain.md §5-6): states, reasons, codes.

Ticket state machine:

                 entry authorized               car passes entry loop
   VALID ───────────────────────────▶ ENTERING ─────────────────────────▶ INSIDE
     ▲                                   │                                   │
     └──── authorization expired (60s) ──┘          exit authorized          │
            (DRIVE_UP: → VOID instead)          ┌──────────────────────────────┘
                                                ▼
                                             EXITING ── car passes exit loop ──▶ EXITED
                                                │
                                                └── authorization expired ──▶ INSIDE

   Any non-final state ──(operator void / lost ticket)──▶ VOID
   VALID at archive time ─────────────────────────────▶ EXPIRED
"""

import base64
import secrets
from enum import StrEnum


class TicketType(StrEnum):
    PREBOOKED = "PREBOOKED"
    DRIVE_UP = "DRIVE_UP"


class TicketStatus(StrEnum):
    VALID = "VALID"
    ENTERING = "ENTERING"
    INSIDE = "INSIDE"
    EXITING = "EXITING"
    EXITED = "EXITED"
    VOID = "VOID"
    EXPIRED = "EXPIRED"


FINAL_TICKET_STATUSES = frozenset({TicketStatus.EXITED, TicketStatus.VOID, TicketStatus.EXPIRED})


class PassageStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    AUTHORIZED = "AUTHORIZED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    ABORTED = "ABORTED"
    REJECTED = "REJECTED"


class Decision(StrEnum):
    OPEN = "OPEN"
    DENY = "DENY"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    DECLINED = "DECLINED"


class RejectReason(StrEnum):
    UNKNOWN_TICKET = "UNKNOWN_TICKET"
    WRONG_EVENT = "WRONG_EVENT"
    EVENT_NOT_LIVE = "EVENT_NOT_LIVE"
    ALREADY_INSIDE = "ALREADY_INSIDE"
    NOT_INSIDE = "NOT_INSIDE"
    ALREADY_USED = "ALREADY_USED"
    TICKET_VOID = "TICKET_VOID"
    ENTRY_IN_PROGRESS = "ENTRY_IN_PROGRESS"
    EXIT_IN_PROGRESS = "EXIT_IN_PROGRESS"
    CAR_PARK_FULL = "CAR_PARK_FULL"
    DEVICE_NOT_ALLOWED = "DEVICE_NOT_ALLOWED"


# What the driver sees on the lane display for each rejection.
DISPLAY_TEXT: dict[RejectReason, str] = {
    RejectReason.UNKNOWN_TICKET: "Ticket not recognised - please call staff",
    RejectReason.WRONG_EVENT: "This ticket is for another event",
    RejectReason.EVENT_NOT_LIVE: "Car park is closed",
    RejectReason.ALREADY_INSIDE: "This ticket is already inside",
    RejectReason.NOT_INSIDE: "This ticket has not entered - please call staff",
    RejectReason.ALREADY_USED: "This ticket has already been used",
    RejectReason.TICKET_VOID: "This ticket is no longer valid",
    RejectReason.ENTRY_IN_PROGRESS: "Entry already in progress at another lane",
    RejectReason.EXIT_IN_PROGRESS: "Exit already in progress at another lane",
    RejectReason.CAR_PARK_FULL: "Car park full - please follow signs",
    RejectReason.DEVICE_NOT_ALLOWED: "Please use the correct lane",
}


def entry_rejection(status: TicketStatus) -> RejectReason | None:
    """Anti-passback for entry: only a VALID ticket may enter. Returns why not, or None if allowed.

    (An ENTERING ticket whose hold has expired is treated as VALID by the caller before asking.)
    """
    return {
        TicketStatus.VALID: None,
        TicketStatus.ENTERING: RejectReason.ENTRY_IN_PROGRESS,
        TicketStatus.INSIDE: RejectReason.ALREADY_INSIDE,
        TicketStatus.EXITING: RejectReason.ALREADY_INSIDE,
        TicketStatus.EXITED: RejectReason.ALREADY_USED,  # pre-booked passes are single-entry
        TicketStatus.VOID: RejectReason.TICKET_VOID,
        TicketStatus.EXPIRED: RejectReason.TICKET_VOID,
    }[status]


def exit_rejection(status: TicketStatus) -> RejectReason | None:
    """Anti-passback for exit: only an INSIDE ticket may leave."""
    return {
        TicketStatus.INSIDE: None,
        TicketStatus.EXITING: RejectReason.EXIT_IN_PROGRESS,
        TicketStatus.VALID: RejectReason.NOT_INSIDE,
        TicketStatus.ENTERING: RejectReason.NOT_INSIDE,
        TicketStatus.EXITED: RejectReason.ALREADY_USED,
        TicketStatus.VOID: RejectReason.TICKET_VOID,
        TicketStatus.EXPIRED: RejectReason.TICKET_VOID,
    }[status]


TICKET_CODE_PREFIX = "EP1-"


def new_ticket_code() -> str:
    """16 random bytes in Base32 (26 chars, no padding) behind a version prefix: EP1-JBSWY3DPEHPK3PXP...

    Opaque and unguessable (128 bits): it contains no data, so a gate must ask the server what it means (ADR-006).
    Base32 = only A-Z and 2-7: easy for QR/barcode scanners, no confusable 0/O or 1/l, case-insensitive.
    """
    return TICKET_CODE_PREFIX + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")


def mask_ticket_code(code: str) -> str:
    """For logs: never log a full ticket code (it's a bearer credential, like a key). CLAUDE.md §8."""
    return code[:8] + "…" if len(code) > 8 else "…"
