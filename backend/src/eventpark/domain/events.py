"""Event lifecycle (docs/01-product-and-domain.md §4) as a pure state machine.

    DRAFT ──publish──▶ PUBLISHED ──go_live──▶ LIVE ──end──▶ ENDED ──archive──▶ ARCHIVED
      │                    │
      └──────cancel────────┴──▶ CANCELLED

All rules live here, in one place, with no database or clock access: the caller passes the current status, the
action, the time and a snapshot of the event. Invalid transitions come back as a `TransitionError` value (not a
silent no-op, not a half-updated row), which the service turns into a 409 response.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    LIVE = "LIVE"
    ENDED = "ENDED"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"


class EventAction(StrEnum):
    PUBLISH = "publish"
    GO_LIVE = "go-live"
    END = "end"
    ARCHIVE = "archive"
    CANCEL = "cancel"


@dataclass(frozen=True)
class EventSnapshot:
    """What the rules need to know about an event (the service builds this from the DB rows)."""

    gates_open_at: datetime
    gates_close_at: datetime
    has_pricing: bool
    zone_count: int


@dataclass(frozen=True)
class TransitionError:
    code: str
    message: str


# Which action is allowed from which status, and where it leads. Anything not listed is invalid.
_TRANSITIONS: dict[tuple[EventStatus, EventAction], EventStatus] = {
    (EventStatus.DRAFT, EventAction.PUBLISH): EventStatus.PUBLISHED,
    (EventStatus.PUBLISHED, EventAction.GO_LIVE): EventStatus.LIVE,
    (EventStatus.LIVE, EventAction.END): EventStatus.ENDED,
    (EventStatus.ENDED, EventAction.ARCHIVE): EventStatus.ARCHIVED,
    (EventStatus.DRAFT, EventAction.CANCEL): EventStatus.CANCELLED,
    (EventStatus.PUBLISHED, EventAction.CANCEL): EventStatus.CANCELLED,
}

# States in which the event's configuration (times, pricing, zone capacities) may still change.
EDITABLE_STATUSES = frozenset({EventStatus.DRAFT, EventStatus.PUBLISHED})
# States in which gates accept entries / exits.
ENTRY_ALLOWED_STATUSES = frozenset({EventStatus.LIVE})
EXIT_ALLOWED_STATUSES = frozenset({EventStatus.LIVE, EventStatus.ENDED})  # ENDED: cars are still inside!


def transition(
    current: EventStatus, action: EventAction, *, now: datetime, event: EventSnapshot
) -> EventStatus | TransitionError:
    target = _TRANSITIONS.get((current, action))
    if target is None:
        return TransitionError("INVALID_TRANSITION", f"Cannot {action.value} an event that is {current.value}.")

    if action is EventAction.PUBLISH:
        if not event.has_pricing:
            return TransitionError("PRICING_MISSING", "Set the event pricing before publishing.")
        if event.zone_count == 0:
            return TransitionError("ZONES_MISSING", "Configure at least one zone before publishing.")
        if now >= event.gates_close_at:
            return TransitionError("EVENT_IN_PAST", "Gates have already closed for this event.")

    if action is EventAction.GO_LIVE and now >= event.gates_close_at:
        # Manual go-live before gates_open_at is allowed (staff may open early); after closing time it makes no sense.
        return TransitionError("EVENT_IN_PAST", "Gates have already closed for this event.")

    return target


def automatic_transition(current: EventStatus, *, now: datetime, event: EventSnapshot) -> EventStatus | None:
    """What the maintenance job should do on its tick (Phase 6): open and close gates by the clock.

    Returns the new status, or None if nothing is due. Archiving stays manual (staff confirm the event is over).
    """
    if current is EventStatus.PUBLISHED and event.gates_open_at <= now < event.gates_close_at:
        return EventStatus.LIVE
    if current is EventStatus.LIVE and now >= event.gates_close_at:
        return EventStatus.ENDED
    return None
