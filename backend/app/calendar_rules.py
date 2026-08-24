"""Server-side UAE calendar rules for test-drive slots (FR-SL, S-22).

These rules are the single source of truth for what times may exist as bookable
slots. They live here in the service layer — never in a prompt — so a forbidden
slot (e.g. Friday noon) can never even be generated, and this is unit-testable
("no conversation can ever offer Friday noon"). See CLAUDE.md rule 6.

UAE conventions in play here:
- Friday IS a working day; only the ~11:30-13:30 prayer window is blocked.
- Saturday and Sunday are the UAE weekend, but showrooms trade — they are
  ordinary open days here, on whatever hours the showroom publishes.
- Public holidays block the whole day (list is a TODO stub for now).

Per-showroom opening hours arrive as a `hours_by_day` mapping (see
Showroom.hours_by_day) rather than a single open/close pair, because a
showroom's window varies by weekday — the seeded Jetour showrooms open 09:00
on Mon-Sat but only 12:00 on Sun, and a single pair would silently generate
9am Sunday slots that do not exist.

This module takes a plain mapping, never an ORM object, so it stays free of any
database import and testable without a session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta

# Python's date.weekday(): Monday=0 ... Sunday=6. Friday is 4.
FRIDAY = 4

# hours_by_day keys, indexed by date.weekday(). Names (not integers) because the
# seed JSON is proof-read by humans, and JSON object keys are strings anyway.
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Friday congregational-prayer window that must never contain a slot.
FRIDAY_PRAYER_START = time(11, 30)
FRIDAY_PRAYER_END = time(13, 30)

# Slots are 90 minutes long.
SLOT_MINUTES = 90

# Evening is denser than daytime (demand is evening-weighted).
EVENING_START_HOUR = 17  # 17:00
DAYTIME_STEP_MINUTES = 120  # sparse cadence before the evening
EVENING_STEP_MINUTES = 90  # denser cadence 17:00 onward

# TODO: populate with real UAE public holidays (Gregorian dates) before the demo.
# Empty for now so the horizon is fully open outside the Friday window.
PUBLIC_HOLIDAYS: set[date] = set()


def is_public_holiday(day: date) -> bool:
    """Return True if `day` is a UAE public holiday (no slots that day)."""
    return day in PUBLIC_HOLIDAYS


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap: [a_start, a_end) vs [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def in_friday_prayer_window(start: datetime, end: datetime) -> bool:
    """True if the interval [start, end) touches the Friday prayer window.

    Only Fridays are blocked. The window is evaluated on the calendar date of
    each endpoint, so a slot that begins Thursday night and runs into Friday, or
    begins Friday and crosses midnight, is still checked correctly.
    """
    # Check every calendar date the interval spans (normally one, at most a few).
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() == FRIDAY:
            window_start = datetime.combine(day, FRIDAY_PRAYER_START)
            window_end = datetime.combine(day, FRIDAY_PRAYER_END)
            if _overlaps(start, end, window_start, window_end):
                return True
        day += timedelta(days=1)
    return False


def hours_for_day(
    day: date, hours_by_day: Mapping[str, Sequence[int] | None]
) -> tuple[int, int] | None:
    """Resolve `day`'s (open_hour, close_hour), or None if the showroom is shut.

    A weekday whose key is absent, or maps to null, means closed that day.
    """
    window = hours_by_day.get(WEEKDAY_KEYS[day.weekday()])
    if window is None:
        return None
    open_hour, close_hour = window
    return int(open_hour), int(close_hour)


def slot_times_for_day(
    day: date, hours_by_day: Mapping[str, Sequence[int] | None]
) -> list[tuple[datetime, datetime]]:
    """Generate 90-minute (start, end) slots for `day`, evening-weighted.

    - Daytime (open_hour .. EVENING_START_HOUR): sparse, every 120 minutes.
    - Evening (EVENING_START_HOUR .. close_hour): dense, every 90 minutes.
    - A slot must end by that weekday's close_hour.
    - Slots overlapping the Friday prayer window are skipped (FR-SL-02, S-22).
    - Public holidays, and days the showroom is closed, yield no slots at all.

    `hours_by_day` comes from the caller (Showroom.hours_by_day); this function
    owns the cadence, the per-weekday window lookup, and the Friday/holiday
    cutouts. A forbidden time can therefore never be generated in the first
    place, which is what makes S-22 testable.
    """
    if is_public_holiday(day):
        return []

    window = hours_for_day(day, hours_by_day)
    if window is None:
        return []
    open_hour, close_hour = window

    day_open = datetime.combine(day, time(hour=open_hour))
    day_close = datetime.combine(day, time(hour=close_hour))
    evening_start = datetime.combine(day, time(hour=EVENING_START_HOUR))
    duration = timedelta(minutes=SLOT_MINUTES)

    slots: list[tuple[datetime, datetime]] = []
    cursor = day_open
    while cursor + duration <= day_close:
        end = cursor + duration
        step_minutes = (
            EVENING_STEP_MINUTES if cursor >= evening_start else DAYTIME_STEP_MINUTES
        )
        if not in_friday_prayer_window(cursor, end):
            slots.append((cursor, end))
        cursor += timedelta(minutes=step_minutes)

    return slots
