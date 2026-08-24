"""Calendar-rule tests, centred on S-22: no slot can ever fall in the Friday
prayer window (~11:30-13:30). See CLAUDE.md rule 6.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.calendar_rules import (
    FRIDAY,
    FRIDAY_PRAYER_END,
    FRIDAY_PRAYER_START,
    PUBLIC_HOLIDAYS,
    WEEKDAY_KEYS,
    hours_for_day,
    in_friday_prayer_window,
    is_public_holiday,
    slot_times_for_day,
)

# A uniform 10:00-22:00 week. Synthetic on purpose: these exercise cadence and
# the Friday cutout independently of what any real showroom publishes.
SYNTHETIC_10_22: dict[str, list[int] | None] = {k: [10, 22] for k in WEEKDAY_KEYS}

# The hours actually seeded for all six Jetour showrooms: Mon-Sat 09:00-21:00,
# Sun 12:00-21:00. Kept in sync with seed/showrooms.json by
# test_seeded_hours_fixture_matches_seed_file below.
SEEDED_HOURS: dict[str, list[int] | None] = {
    **{k: [9, 21] for k in ("mon", "tue", "wed", "thu", "fri", "sat")},
    "sun": [12, 21],
}


def _next_friday(from_day: date) -> date:
    """Return the first Friday on or after `from_day`."""
    offset = (FRIDAY - from_day.weekday()) % 7
    return from_day + timedelta(days=offset)


def _next_weekday(from_day: date, weekday: int) -> date:
    """Return the first `weekday` (Mon=0..Sun=6) on or after `from_day`."""
    return from_day + timedelta(days=(weekday - from_day.weekday()) % 7)


# --------------------------------------------------------------------------- #
# in_friday_prayer_window
# --------------------------------------------------------------------------- #
def test_friday_noon_is_in_window():
    friday = _next_friday(date(2026, 7, 16))
    start = datetime.combine(friday, time(12, 0))
    end = start + timedelta(minutes=90)
    assert in_friday_prayer_window(start, end) is True


def test_friday_morning_before_window_is_clear():
    friday = _next_friday(date(2026, 7, 16))
    start = datetime.combine(friday, time(9, 30))
    end = start + timedelta(minutes=90)  # 09:30-11:00, ends before 11:30
    assert in_friday_prayer_window(start, end) is False


def test_friday_afternoon_after_window_is_clear():
    friday = _next_friday(date(2026, 7, 16))
    start = datetime.combine(friday, time(13, 30))  # window end exactly
    end = start + timedelta(minutes=90)
    assert in_friday_prayer_window(start, end) is False


def test_slot_straddling_window_start_is_blocked():
    friday = _next_friday(date(2026, 7, 16))
    start = datetime.combine(friday, time(10, 30))
    end = start + timedelta(minutes=90)  # 10:30-12:00 overlaps 11:30
    assert in_friday_prayer_window(start, end) is True


def test_non_friday_noon_is_clear():
    # A Wednesday noon must never be treated as the prayer window.
    wednesday = _next_friday(date(2026, 7, 16)) - timedelta(days=2)
    assert wednesday.weekday() != FRIDAY
    start = datetime.combine(wednesday, time(12, 0))
    end = start + timedelta(minutes=90)
    assert in_friday_prayer_window(start, end) is False


# --------------------------------------------------------------------------- #
# S-22: generated slots never overlap the Friday window
# --------------------------------------------------------------------------- #
def test_no_generated_slot_overlaps_friday_window_over_full_horizon():
    """Sweep 14 rolling days incl. Fridays; assert no slot hits the window."""
    start_day = date(2026, 7, 16)
    for offset in range(14):
        day = start_day + timedelta(days=offset)
        for slot_start, slot_end in slot_times_for_day(day, SYNTHETIC_10_22):
            assert not in_friday_prayer_window(slot_start, slot_end), (
                f"slot {slot_start}-{slot_end} overlaps the Friday prayer window"
            )


def test_friday_has_slots_outside_the_window_but_none_inside():
    friday = _next_friday(date(2026, 7, 16))
    slots = slot_times_for_day(friday, SYNTHETIC_10_22)
    assert slots, "Friday should still offer slots outside the prayer window"
    window_start = datetime.combine(friday, FRIDAY_PRAYER_START)
    window_end = datetime.combine(friday, FRIDAY_PRAYER_END)
    for slot_start, slot_end in slots:
        assert not (slot_start < window_end and window_start < slot_end)


# --------------------------------------------------------------------------- #
# slot generation invariants
# --------------------------------------------------------------------------- #
def test_slots_are_90_minutes_and_within_hours():
    day = date(2026, 7, 20)  # a Monday
    slots = slot_times_for_day(day, SYNTHETIC_10_22)
    assert slots
    for slot_start, slot_end in slots:
        assert slot_end - slot_start == timedelta(minutes=90)
        assert slot_start.hour >= 10
        assert slot_end <= datetime.combine(day, time(22, 0))


def test_evening_is_denser_than_daytime():
    day = date(2026, 7, 20)
    slots = slot_times_for_day(day, SYNTHETIC_10_22)
    day_starts = [s for s, _ in slots if s.hour < 17]
    eve_starts = [s for s, _ in slots if s.hour >= 17]
    # Evening cadence (90m) must be tighter than daytime cadence (120m).
    if len(day_starts) >= 2:
        assert (day_starts[1] - day_starts[0]) == timedelta(minutes=120)
    if len(eve_starts) >= 2:
        assert (eve_starts[1] - eve_starts[0]) == timedelta(minutes=90)


# --------------------------------------------------------------------------- #
# holidays
# --------------------------------------------------------------------------- #
def test_public_holiday_yields_no_slots(monkeypatch):
    day = date(2026, 7, 20)
    monkeypatch.setattr("app.calendar_rules.PUBLIC_HOLIDAYS", {day})
    assert is_public_holiday(day) is True
    assert slot_times_for_day(day, SYNTHETIC_10_22) == []


def test_public_holidays_empty_by_default():
    assert PUBLIC_HOLIDAYS == set()


# --------------------------------------------------------------------------- #
# per-weekday hours (FR-SL-02) — exercised against the REAL seeded hours
# --------------------------------------------------------------------------- #
def test_sunday_generates_no_slot_before_noon():
    """Sun opens at 12:00, not 09:00 — the bug a single open/close pair hid."""
    sunday = _next_weekday(date(2026, 7, 16), 6)
    assert sunday.weekday() == 6
    slots = slot_times_for_day(sunday, SEEDED_HOURS)
    assert slots, "Sunday should still offer afternoon/evening slots"
    noon = datetime.combine(sunday, time(12, 0))
    for slot_start, _ in slots:
        assert slot_start >= noon, f"Sunday slot {slot_start} starts before 12:00"


def test_monday_opens_at_nine_on_seeded_hours():
    """The Mon-Sat window is 09:00, so Sunday's 12:00 is not applied globally."""
    monday = _next_weekday(date(2026, 7, 16), 0)
    slots = slot_times_for_day(monday, SEEDED_HOURS)
    assert slots
    assert min(s for s, _ in slots) == datetime.combine(monday, time(9, 0))


def test_seeded_hours_close_by_nine_pm():
    for weekday in range(7):
        day = _next_weekday(date(2026, 7, 16), weekday)
        for _, slot_end in slot_times_for_day(day, SEEDED_HOURS):
            assert slot_end <= datetime.combine(day, time(21, 0))


def test_no_seeded_slot_overlaps_friday_window_over_full_horizon():
    """S-22 again, on real hours rather than the synthetic 10-22 week."""
    start_day = date(2026, 7, 16)
    for offset in range(14):
        day = start_day + timedelta(days=offset)
        for slot_start, slot_end in slot_times_for_day(day, SEEDED_HOURS):
            assert not in_friday_prayer_window(slot_start, slot_end), (
                f"slot {slot_start}-{slot_end} overlaps the Friday prayer window"
            )


def test_friday_is_a_working_day_on_seeded_hours():
    """Friday trades 09:00-21:00; only the prayer window is cut out."""
    friday = _next_friday(date(2026, 7, 16))
    slots = slot_times_for_day(friday, SEEDED_HOURS)
    assert slots, "Friday is a working day and must offer slots"
    assert min(s for s, _ in slots) == datetime.combine(friday, time(9, 0))


def test_saturday_and_sunday_both_trade():
    """UAE weekend is Sat-Sun, but showrooms trade — neither day is empty."""
    for weekday in (5, 6):
        day = _next_weekday(date(2026, 7, 16), weekday)
        assert slot_times_for_day(day, SEEDED_HOURS), f"weekday {weekday} had no slots"


# --------------------------------------------------------------------------- #
# closed days
# --------------------------------------------------------------------------- #
def test_null_weekday_means_closed():
    hours = {**SEEDED_HOURS, "sun": None}
    sunday = _next_weekday(date(2026, 7, 16), 6)
    assert hours_for_day(sunday, hours) is None
    assert slot_times_for_day(sunday, hours) == []


def test_absent_weekday_key_means_closed():
    hours = {k: v for k, v in SEEDED_HOURS.items() if k != "sun"}
    sunday = _next_weekday(date(2026, 7, 16), 6)
    assert hours_for_day(sunday, hours) is None
    assert slot_times_for_day(sunday, hours) == []


# --------------------------------------------------------------------------- #
# the SEEDED_HOURS fixture must not drift from the seed file
# --------------------------------------------------------------------------- #
def test_seeded_hours_fixture_matches_seed_file():
    import json
    from pathlib import Path

    fixture = Path(__file__).resolve().parent.parent / "seed" / "showrooms.json"
    rows = json.loads(fixture.read_text(encoding="utf-8"))
    assert rows, "showrooms.json is empty"
    for r in rows:
        assert r["hours_by_day"] == SEEDED_HOURS, (
            f"showroom {r['id']} hours_by_day drifted from the test fixture"
        )
