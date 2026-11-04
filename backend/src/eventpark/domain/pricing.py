"""Drive-up parking fee (docs/01-product-and-domain.md §8). Pure function: no clock, no database.

    minutes = ceil((now - entered_at) in minutes)
    if minutes <= free_minutes: fee = 0
    else:
        days, rest = divmod(minutes, 1440)
        fee = days * daily_cap + min(ceil(rest / 60) * hourly, daily_cap)
    fee_due = fee - already_paid    (if already paid and still within the exit grace period -> 0)

Worked example: entered 18:05, exit scan 21:40 -> 215 min -> ceil(215/60) = 4 -> 4 x 300.00 = 1,200.00 RSD.
All amounts are integers in minor units (para): 1,200.00 RSD = 120000. Never floats (CLAUDE.md §3).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class FeeRules:
    free_minutes: int
    hourly_minor: int
    daily_cap_minor: int
    exit_grace_minutes: int


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)  # integer ceiling division, no float rounding involved


def parked_minutes(entered_at: datetime, now: datetime) -> int:
    """Started minutes: 0 s -> 0, 1 s -> 1, 60 s -> 1, 61 s -> 2. Negative (clock skew) counts as 0."""
    microseconds = (now - entered_at) // timedelta(microseconds=1)
    return max(0, _ceil_div(microseconds, 60_000_000))


def total_fee(minutes: int, rules: FeeRules) -> int:
    if minutes <= rules.free_minutes:
        return 0
    days, rest = divmod(minutes, MINUTES_PER_DAY)
    return days * rules.daily_cap_minor + min(_ceil_div(rest, 60) * rules.hourly_minor, rules.daily_cap_minor)


def calculate_drive_up_fee(
    *, entered_at: datetime, now: datetime, rules: FeeRules, paid_minor: int = 0, paid_at: datetime | None = None
) -> int:
    """Amount still due at the exit lane, in minor units."""
    if paid_at is not None and now <= paid_at + timedelta(minutes=rules.exit_grace_minutes):
        return 0  # paid recently: you have `exit_grace_minutes` to drive to the barrier
    fee = total_fee(parked_minutes(entered_at, now), rules)
    return max(0, fee - paid_minor)
