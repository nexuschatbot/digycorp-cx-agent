"""STANDING GUARD: FR-SL-04 — relative dates resolve to the RIGHT day.

WHY THIS FILE EXISTS. The date anchor used to give the model only "today" and let
it compute the rest. Asked what "this weekend" meant on Monday 20 July 2026, it
answered:

    "This weekend is Saturday 26 July and Sunday 27 July."

26 July 2026 is a Sunday; 27 July is a Monday. Both dates AND both day names were
wrong, reproducibly. The booking path stayed correct because slots are described
from tool data (rule 2 grounding), so the customer would have been booked on the
right day while being TOLD the wrong one — the failure is in prose, which is
exactly where a demo audience notices it.

The fix is injection, not instruction: Python computes every relative term and the
prompt states it as fact. So the thing to guard is that the injected list is
correct for any day the demo might run on, which is a pure-arithmetic property
testable without a model or an API key.

The live half asserts the model READS the list instead of recomputing — the one
failure mode injection cannot rule out on its own.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

import pytest

from app.agent.prompt import _relative_dates, system_prompt


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
DAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}  # Monday=0

# "Saturday, 25 July" / "Saturday 25 July" / "Saturday, July 25"
PAIR = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b,?\s+"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]+)|([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?)",
    re.I,
)

# A fortnight of start days, so every weekday is covered including the two the
# weekend logic actually turns on (Saturday and Sunday).
A_MONDAY = date(2026, 7, 20)
EVERY_WEEKDAY = [A_MONDAY + timedelta(days=n) for n in range(14)]


def day_name_mismatches(text: str, year: int) -> list[str]:
    """Every (day-name, date) pair in `text` whose day name is wrong for real."""
    bad = []
    for m in PAIR.finditer(text or ""):
        dayname = m.group(1).lower()
        if m.group(2):
            dom, monthname = int(m.group(2)), m.group(3).lower()
        else:
            monthname, dom = m.group(4).lower(), int(m.group(5))
        month = MONTHS.get(monthname)
        if month is None:
            continue
        try:
            actual = date(year, month, dom)
        except ValueError:
            bad.append(f"{m.group(0)!r} is not a real date")
            continue
        if actual.weekday() != DAYS[dayname]:
            bad.append(
                f"{m.group(0)!r} — {dom} {monthname.title()} {year} is a "
                f"{calendar.day_name[actual.weekday()]}"
            )
    return bad


# --------------------------------------------------------------------------- #
# STRUCTURAL — always runs. Pure arithmetic, no model, no API key.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("today", EVERY_WEEKDAY, ids=lambda d: d.strftime("%a-%Y-%m-%d"))
def test_every_injected_day_name_matches_its_date(today: date) -> None:
    """THE REGRESSION CASE, generalised. Whatever day the demo runs on, every
    day-name/date pair in the prompt must agree with the real calendar."""
    bad = day_name_mismatches(system_prompt(today), today.year)
    assert not bad, f"prompt states impossible dates for {today}: {bad}"


@pytest.mark.parametrize("today", EVERY_WEEKDAY, ids=lambda d: d.strftime("%a-%Y-%m-%d"))
def test_relative_terms_are_arithmetically_right(today: date) -> None:
    """tomorrow / ba'd bukra / the weekend, checked against date arithmetic."""
    r = _relative_dates(today)

    assert r["today"] == today.isoformat()
    assert r["tomorrow"] == (today + timedelta(days=1)).isoformat()
    assert r["day_after"] == (today + timedelta(days=2)).isoformat()

    sat, sun = date.fromisoformat(r["sat"]), date.fromisoformat(r["sun"])
    assert sat.weekday() == 5, f"'this weekend' Saturday is a {calendar.day_name[sat.weekday()]}"
    assert sun.weekday() == 6, f"'this weekend' Sunday is a {calendar.day_name[sun.weekday()]}"
    assert sun == sat + timedelta(days=1), "the weekend must be consecutive days"

    nsat, nsun = date.fromisoformat(r["next_sat"]), date.fromisoformat(r["next_sun"])
    assert nsat == sat + timedelta(days=7)
    assert nsun == nsat + timedelta(days=1)


@pytest.mark.parametrize("today", EVERY_WEEKDAY, ids=lambda d: d.strftime("%a-%Y-%m-%d"))
def test_this_weekend_is_never_in_the_past(today: date) -> None:
    """Offering a weekend that has already happened is worse than reading
    forward — and the inventory window is forward-looking anyway."""
    assert date.fromisoformat(_relative_dates(today)["sat"]) >= today


def test_the_original_failure_is_fixed() -> None:
    """The exact case, pinned: Monday 20 July 2026 -> Sat 25, Sun 26."""
    r = _relative_dates(A_MONDAY)
    assert r["sat"] == "2026-07-25" and r["sat_day"] == "Saturday"
    assert r["sun"] == "2026-07-26" and r["sun_day"] == "Sunday"
    # What it used to say, and must never say again.
    prompt = system_prompt(A_MONDAY)
    assert "Saturday, 2026-07-26" not in prompt
    assert "Sunday, 2026-07-27" not in prompt


def test_the_detector_would_have_caught_the_original_bug() -> None:
    """Guard the guard: the checker must fail the sentence that shipped."""
    assert day_name_mismatches("This weekend is Saturday 26 July and Sunday 27 July.", 2026)
    assert not day_name_mismatches("This weekend is Saturday 25 July and Sunday 26 July.", 2026)


def test_prompt_forbids_the_model_doing_its_own_arithmetic() -> None:
    """Injection only works if the model is told to READ rather than compute."""
    body = system_prompt(A_MONDAY).lower()
    assert "never do date arithmetic yourself" in body
    assert "resolved dates" in body
