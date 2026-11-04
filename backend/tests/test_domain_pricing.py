"""calculate_drive_up_fee: the boundary cases from the learning path (Phase 3, task 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from eventpark.domain.pricing import FeeRules, calculate_drive_up_fee, parked_minutes

RULES = FeeRules(free_minutes=15, hourly_minor=30_000, daily_cap_minor=150_000, exit_grace_minutes=15)
ENTERED = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)


def fee(after: timedelta, **kwargs: object) -> int:
    return calculate_drive_up_fee(entered_at=ENTERED, now=ENTERED + after, rules=RULES, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (timedelta(0), 0),  # 0 min
        (timedelta(minutes=15), 0),  # exactly the free minutes
        (timedelta(minutes=15, seconds=1), 30_000),  # one second over -> 16 started minutes -> 1 started hour
        (timedelta(minutes=16), 30_000),  # free + 1
        (timedelta(minutes=59), 30_000),
        (timedelta(minutes=60), 30_000),
        (timedelta(minutes=61), 60_000),
        (timedelta(minutes=215), 120_000),  # the worked example: 18:05 -> 21:40 = 1,200.00 RSD
        (timedelta(hours=5), 150_000),  # 5 h x 300 = 1,500 = exactly the daily cap
        (timedelta(hours=6), 150_000),  # capped
        (timedelta(hours=23, minutes=59), 150_000),
        (timedelta(hours=24), 150_000),  # 1 full day, 0 rest
        (timedelta(hours=24, minutes=1), 180_000),  # 1 day + 1 started hour
        (timedelta(hours=49), 330_000),  # 2 days (300,000) + 1 h (30,000)
        (timedelta(hours=72), 450_000),
    ],
)
def test_fee_boundaries(after: timedelta, expected: int) -> None:
    assert fee(after) == expected


def test_already_paid_within_grace_is_free() -> None:
    paid_at = ENTERED + timedelta(hours=3)

    assert fee(timedelta(hours=3, minutes=14), paid_minor=90_000, paid_at=paid_at) == 0
    assert fee(timedelta(hours=3, minutes=15), paid_minor=90_000, paid_at=paid_at) == 0  # grace is inclusive


def test_already_paid_after_grace_pays_only_the_difference() -> None:
    """Paid 900 at 21:05 (3 h), still at the barrier at 21:30 -> now 4 started hours = 1,200 -> 300 due."""
    paid_at = ENTERED + timedelta(hours=3)

    assert fee(timedelta(hours=3, minutes=25), paid_minor=90_000, paid_at=paid_at) == 30_000


def test_never_negative() -> None:
    assert fee(timedelta(minutes=30), paid_minor=999_999, paid_at=ENTERED) == 0


def test_clock_skew_counts_as_zero_minutes() -> None:
    assert parked_minutes(ENTERED, ENTERED - timedelta(seconds=5)) == 0


def test_amounts_are_integers() -> None:
    assert isinstance(fee(timedelta(minutes=215)), int)
