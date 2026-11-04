"""Payments and bookings (docs/01-product-and-domain.md §9).

Payment state machine:
    PENDING ──▶ SUCCEEDED ──▶ REFUNDED (out of scope, state exists)
       │
       ├──▶ FAILED      (declined / cancelled / expired)
       └──▶ UNKNOWN ──(reconciliation asks the provider)──▶ SUCCEEDED | FAILED

UNKNOWN is the honest answer to "the provider didn't reply in time": the charge may or may not have happened.
We never guess. We ask again with the same idempotency key until the provider says one or the other.
"""

from dataclasses import dataclass
from enum import StrEnum


class PaymentKind(StrEnum):
    BOOKING = "BOOKING"
    EXIT_FEE = "EXIT_FEE"
    LOST_TICKET = "LOST_TICKET"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REFUNDED = "REFUNDED"


class PaymentOutcome(StrEnum):
    """What we learned from the provider (or didn't)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NO_ANSWER = "no_answer"  # timeout / 5xx / network error
    REFUNDED = "refunded"


@dataclass(frozen=True)
class PaymentTransitionError:
    message: str


_PAYMENT_TRANSITIONS: dict[tuple[PaymentStatus, PaymentOutcome], PaymentStatus] = {
    (PaymentStatus.PENDING, PaymentOutcome.SUCCEEDED): PaymentStatus.SUCCEEDED,
    (PaymentStatus.PENDING, PaymentOutcome.FAILED): PaymentStatus.FAILED,
    (PaymentStatus.PENDING, PaymentOutcome.NO_ANSWER): PaymentStatus.UNKNOWN,
    (PaymentStatus.UNKNOWN, PaymentOutcome.SUCCEEDED): PaymentStatus.SUCCEEDED,
    (PaymentStatus.UNKNOWN, PaymentOutcome.FAILED): PaymentStatus.FAILED,
    (PaymentStatus.UNKNOWN, PaymentOutcome.NO_ANSWER): PaymentStatus.UNKNOWN,
    (PaymentStatus.SUCCEEDED, PaymentOutcome.REFUNDED): PaymentStatus.REFUNDED,
    # Hearing the same final answer again (duplicate webhook, retried request) is not an error: nothing changes.
    (PaymentStatus.SUCCEEDED, PaymentOutcome.SUCCEEDED): PaymentStatus.SUCCEEDED,
    (PaymentStatus.FAILED, PaymentOutcome.FAILED): PaymentStatus.FAILED,
    (PaymentStatus.SUCCEEDED, PaymentOutcome.NO_ANSWER): PaymentStatus.SUCCEEDED,
    (PaymentStatus.FAILED, PaymentOutcome.NO_ANSWER): PaymentStatus.FAILED,
}

FINAL_PAYMENT_STATUSES = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.REFUNDED})


def payment_transition(current: PaymentStatus, outcome: PaymentOutcome) -> PaymentStatus | PaymentTransitionError:
    """A provider can't change its mind: SUCCEEDED -> FAILED (or the reverse) is a contradiction we must not apply."""
    target = _PAYMENT_TRANSITIONS.get((current, outcome))
    if target is None:
        return PaymentTransitionError(f"payment is {current.value}; outcome {outcome.value} contradicts it")
    return target


class BookingStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"  # quota (and VIP space) held for 15 min while the driver pays
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"  # not paid in time: hold released
    CANCELLED = "CANCELLED"  # payment failed, or the event was cancelled


ACTIVE_BOOKING_STATUSES = frozenset({BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED})


def normalize_plate(plate: str) -> str:
    """'bg 123-ab' -> 'BG123AB': one vehicle, however the driver types it."""
    return "".join(ch for ch in plate.upper() if ch.isalnum())
