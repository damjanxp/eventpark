"""Exhaustive tests for the event lifecycle state machine (domain/events.py)."""

from datetime import UTC, datetime, timedelta

import pytest

from eventpark.domain.events import (
    EventAction,
    EventSnapshot,
    EventStatus,
    TransitionError,
    automatic_transition,
    transition,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
READY = EventSnapshot(
    gates_open_at=NOW + timedelta(hours=5), gates_close_at=NOW + timedelta(hours=9), has_pricing=True, zone_count=3
)

VALID = {
    (EventStatus.DRAFT, EventAction.PUBLISH): EventStatus.PUBLISHED,
    (EventStatus.PUBLISHED, EventAction.GO_LIVE): EventStatus.LIVE,
    (EventStatus.LIVE, EventAction.END): EventStatus.ENDED,
    (EventStatus.ENDED, EventAction.ARCHIVE): EventStatus.ARCHIVED,
    (EventStatus.DRAFT, EventAction.CANCEL): EventStatus.CANCELLED,
    (EventStatus.PUBLISHED, EventAction.CANCEL): EventStatus.CANCELLED,
}


@pytest.mark.parametrize(("current", "action"), list(VALID))
def test_valid_transitions(current: EventStatus, action: EventAction) -> None:
    assert transition(current, action, now=NOW, event=READY) == VALID[(current, action)]


@pytest.mark.parametrize(
    ("current", "action"),
    [(s, a) for s in EventStatus for a in EventAction if (s, a) not in VALID],
)
def test_every_other_combination_is_rejected(current: EventStatus, action: EventAction) -> None:
    """All 6 x 5 = 30 combinations are covered: 6 valid above, the other 24 here."""
    result = transition(current, action, now=NOW, event=READY)

    assert isinstance(result, TransitionError)
    assert result.code == "INVALID_TRANSITION"


def test_cannot_cancel_a_live_event() -> None:
    result = transition(EventStatus.LIVE, EventAction.CANCEL, now=NOW, event=READY)

    assert isinstance(result, TransitionError)


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (EventSnapshot(READY.gates_open_at, READY.gates_close_at, has_pricing=False, zone_count=3), "PRICING_MISSING"),
        (EventSnapshot(READY.gates_open_at, READY.gates_close_at, has_pricing=True, zone_count=0), "ZONES_MISSING"),
        (EventSnapshot(NOW - timedelta(hours=5), NOW, has_pricing=True, zone_count=3), "EVENT_IN_PAST"),
    ],
)
def test_publish_requires_a_complete_future_event(snapshot: EventSnapshot, code: str) -> None:
    result = transition(EventStatus.DRAFT, EventAction.PUBLISH, now=NOW, event=snapshot)

    assert isinstance(result, TransitionError)
    assert result.code == code


def test_manual_go_live_before_gates_open_is_allowed() -> None:
    assert READY.gates_open_at > NOW
    assert transition(EventStatus.PUBLISHED, EventAction.GO_LIVE, now=NOW, event=READY) is EventStatus.LIVE


def test_go_live_after_gates_closed_is_rejected() -> None:
    past = EventSnapshot(NOW - timedelta(hours=5), NOW - timedelta(minutes=1), has_pricing=True, zone_count=1)

    result = transition(EventStatus.PUBLISHED, EventAction.GO_LIVE, now=NOW, event=past)

    assert isinstance(result, TransitionError)
    assert result.code == "EVENT_IN_PAST"


# --- automatic transitions (maintenance tick) ---


def test_published_goes_live_at_gates_open() -> None:
    assert automatic_transition(EventStatus.PUBLISHED, now=READY.gates_open_at, event=READY) is EventStatus.LIVE


def test_published_stays_published_before_gates_open() -> None:
    assert automatic_transition(EventStatus.PUBLISHED, now=NOW, event=READY) is None


def test_live_ends_at_gates_close() -> None:
    assert automatic_transition(EventStatus.LIVE, now=READY.gates_close_at, event=READY) is EventStatus.ENDED


def test_published_event_missed_entirely_does_not_go_live() -> None:
    """If the tick runs only after gates_close_at (system was down), don't open gates for a finished event."""
    assert automatic_transition(EventStatus.PUBLISHED, now=READY.gates_close_at, event=READY) is None


@pytest.mark.parametrize("status", [EventStatus.DRAFT, EventStatus.ENDED, EventStatus.ARCHIVED, EventStatus.CANCELLED])
def test_other_states_never_change_automatically(status: EventStatus) -> None:
    for now in (NOW, READY.gates_open_at, READY.gates_close_at + timedelta(days=1)):
        assert automatic_transition(status, now=now, event=READY) is None
