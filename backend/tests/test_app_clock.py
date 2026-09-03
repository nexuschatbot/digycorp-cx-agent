"""The app runs on the dealership's clock, not the host's (2026-09-03).

WHAT WENT WRONG, so that a future reader knows what these pin. Every naive
datetime in this app is Asia/Dubai wall time — slot starts come from showroom
opening hours, api/feedback.py states the convention — but nothing enforced it,
so the convention was carried by whichever machine the process ran on. It was
developed on a +04 box (see the note on FeedbackSurvey.created_at) and deployed
to a UTC VM, where `datetime.now()` ran four hours behind the data it compared
against. Measured on the test VM, 2026-09-03: 39 open slots were at once "in the
future" by the app's clock and already past in Dubai, and TD-2026-25N1 was
booked for 11:00 at 14:19 local.

THE HOST-TIMEZONE TEST RUNS IN A SUBPROCESS on purpose. Pinning is a process-wide
side effect of importing `app`, so it cannot be un-done or re-done inside a
suite that has already imported it; a child process with a hostile TZ is the only
honest way to assert the pin actually holds.

Offline: no vendor, no network.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app import clock
from app.models import LocationType, Slot, SlotStatus, VehicleModel
from app.services import booking as booking_svc
from app.services import customers
from app.services import slots as svc

_DUBAI = timezone(timedelta(hours=4))


# --------------------------------------------------------------------------- #
# The clock itself
# --------------------------------------------------------------------------- #
def test_the_clock_is_dubai_wall_time():
    """Not the host's, and naive — it is compared against naive columns."""
    assert clock.now().tzinfo is None
    expected = datetime.now(_DUBAI).replace(tzinfo=None)
    assert abs((clock.now() - expected).total_seconds()) < 5


def test_a_utc_host_still_gets_the_dealership_clock():
    """The exact configuration the test VM runs, and the bug it produced."""
    code = (
        "from datetime import datetime, timedelta, timezone\n"
        "from app import clock\n"
        "dubai = datetime.now(timezone(timedelta(hours=4))).replace(tzinfo=None)\n"
        "print(abs((clock.now() - dubai).total_seconds()) < 5)\n"
        # The pin must reach bare datetime.now() too: most of the app still
        # calls it directly, and that is exactly what went wrong.
        "print(abs((datetime.now() - dubai).total_seconds()) < 5)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env={"TZ": "UTC", "PATH": "/usr/bin:/bin"},
        cwd=".",
    )
    assert out.stdout.split() == ["True", "True"], (out.stdout, out.stderr)


def test_the_zone_is_configurable_and_never_silently_falls_back_to_the_host():
    """A dealership in another market changes one variable. A zone we cannot
    resolve lands on the default, never on whatever the host was doing — that
    silent fallback is the failure this module exists to stop."""
    out = subprocess.run(
        [sys.executable, "-c", "from app import clock; print(clock.TIMEZONE)"],
        capture_output=True, text=True,
        env={"TZ": "UTC", "PATH": "/usr/bin:/bin", "APP_TIMEZONE": "not/a/zone"},
        cwd=".",
    )
    assert out.stdout.strip() == "Asia/Dubai", (out.stdout, out.stderr)


def test_today_is_the_dealerships_date():
    """Between 20:00 and midnight in Dubai the host's date is still yesterday,
    and the prompt's date anchor claims to be speaking about the UAE."""
    assert clock.today() == datetime.now(_DUBAI).date()
    assert date.today() == clock.today()


# --------------------------------------------------------------------------- #
# What the clock is FOR: no slot in the past ever reaches a customer
# --------------------------------------------------------------------------- #
def _a_slot(db, *, start, end) -> Slot:
    model = db.scalars(select(VehicleModel)).first()
    variant = model.variants[0] if getattr(model, "variants", None) else None
    slot = Slot(
        location_type=LocationType.showroom,
        showroom_id=db.scalars(select(Slot.showroom_id)).first() or 1,
        vehicle_id=model.id,
        variant_id=variant.id if variant is not None else None,
        start=start, end=end, status=SlotStatus.open,
    )
    db.add(slot)
    db.flush()
    return slot


def test_a_slot_that_has_already_started_is_never_offered(db):
    now = clock.now()
    model = db.scalars(select(VehicleModel)).first()
    _a_slot(db, start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    out = svc.query_available(db, vehicle_id=model.id, now=now)
    assert all(s["start_iso"] > now.isoformat() for s in out.get("slots", []))


def test_a_slot_whose_end_has_passed_is_never_offered(db):
    """Both ends in the future, stated as its own bar — a row whose end is at or
    before now is a data defect, and inventory is the last place it should show."""
    now = clock.now()
    model = db.scalars(select(VehicleModel)).first()
    dead = _a_slot(db, start=now - timedelta(hours=3), end=now - timedelta(hours=2))
    out = svc.query_available(db, vehicle_id=model.id, now=now)
    assert dead.id not in {s["slot_id"] for s in out.get("slots", [])}


def test_a_drive_that_is_already_over_cannot_be_booked(db):
    """The offer buffer is deliberately not applied at create (see
    _lead_cutoff), so this bar is the only one a booking crosses."""
    now = clock.now()
    customer_id = customers.find_or_create(
        db, mobile="+971509990001", name="Clock Test"
    )["customer"]["customer_id"]
    dead = _a_slot(db, start=now - timedelta(hours=3), end=now - timedelta(hours=2))
    dead.status = SlotStatus.locked
    dead.locked_by_session = "s-clock"
    # locked_at must be fresh or the lazy expiry sweep releases it first and the
    # ownership guard answers before the bar under test does.
    dead.locked_at = now
    dead.licence_ack = True
    db.flush()
    res = booking_svc.create(
        db, session_id="s-clock", customer_id=customer_id,
        vehicle_id=dead.vehicle_id, slot_id=dead.id, now=now,
    )
    assert res.get("error") == "slot_in_the_past"
