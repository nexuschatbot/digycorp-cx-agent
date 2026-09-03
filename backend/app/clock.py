"""The application's wall clock, pinned to the dealership's timezone.

WHY THIS EXISTS, AND WHAT IT FIXES. Every naive datetime this application
computes is Asia/Dubai wall time, because every datetime it STORES is: slot
starts come from each showroom's published opening hours (9:00-21:00 local),
bookings quote them back to the customer verbatim, and api/feedback.py states
the convention outright — "the app stores local times as naive Asia/Dubai
(UTC+4)". Nothing ever attached a timezone to them, so the convention was
carried entirely by the machine the process happened to run on.

That held for as long as that machine was in the Gulf. The note on
FeedbackSurvey.created_at records the app being measured on "a +04 box"; the
test VM runs Etc/UTC. On it, `datetime.now()` returned 10:31 while the showrooms
it was reasoning about were at 14:31, so every comparison against a slot ran
four hours behind the data:

    - `query_available` offered slots up to four hours in the past. Measured
      2026-09-03: 39 open slots were simultaneously "in the future" by the app's
      clock and already gone by Dubai's.
    - SLOT_MIN_LEAD_MINUTES bought nothing — a 30-minute buffer measured from a
      clock four hours slow is not a buffer.
    - A customer booked TD-2026-25N1 for 11:00 at 14:19 Dubai time. The booking
      succeeded, the confirmation went out, and the car had been and gone.
    - The prompt's date anchor tells the model "it is {today} in the UAE
      (Asia/Dubai timezone)" while deriving {today} from the process clock, so
      between 20:00 and midnight Dubai it names the wrong DAY.

THE FIX IS TO STOP DEPENDING ON THE HOST. Importing this module pins the
process's timezone, so `datetime.now()` means Dubai wall time on a UAE VM, a
London laptop and a UTC container alike — and every module that already reasons
in naive local time becomes correct again without being rewritten.

WHAT THIS DELIBERATELY DOES NOT TOUCH. Columns defaulted by `server_default=
func.now()` are on the DATABASE's clock, which is UTC on both SQLite and
Postgres. That was already true and stays true; see the note on
FeedbackSurvey.created_at for the incident that documents the split, and
OPEN-ISSUES for the places that still compare one against the other. Pinning
the process clock does not create that mixture and does not resolve it — it
restores the assumption every one of those modules was written under.

`APP_TIMEZONE` overrides the zone for a dealership in another market. Setting it
to "UTC" reproduces the old VM behaviour exactly, which is what makes this
inspectable rather than magic.
"""

from __future__ import annotations

import os
import time as _time
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Read directly rather than through app.config: this module is imported by
# app/__init__.py, before config's own imports have necessarily settled, and it
# needs exactly one string.
TIMEZONE: str = (os.getenv("APP_TIMEZONE") or "Asia/Dubai").strip() or "Asia/Dubai"

try:
    ZONE = ZoneInfo(TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - bad config only
    # A zone we cannot resolve must not take the app down, and must not silently
    # become UTC either — that is the failure this module exists to stop. Fall
    # back loudly to the default rather than to whatever the host was doing.
    ZONE = ZoneInfo("Asia/Dubai")
    TIMEZONE = "Asia/Dubai"


def _pin_process_timezone() -> None:
    """Make bare `datetime.now()` mean TIMEZONE, for this process.

    Unix only, which is every environment this runs in (Linux VM, Docker,
    macOS). `tzset()` is absent on Windows; there the pinning is skipped and
    `now()` below still returns the right answer, so nothing that calls THIS
    module is wrong — only the modules still calling `datetime.now()` directly.
    """
    os.environ["TZ"] = TIMEZONE
    if hasattr(_time, "tzset"):
        _time.tzset()


_pin_process_timezone()


def now() -> datetime:
    """Wall-clock time at the dealership, naive — the storage convention.

    Naive on purpose: it is compared against `Slot.start`, `Slot.end` and every
    other naive column in the schema, and an aware value would raise on the
    first comparison rather than quietly work.
    """
    return datetime.now(ZONE).replace(tzinfo=None)


def today() -> date:
    """The dealership's current date. Not the host's."""
    return now().date()
