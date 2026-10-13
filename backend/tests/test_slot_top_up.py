"""slots.top_up(): the daily job that keeps the rolling window from running out.

WHY THIS EXISTS. generate() writes a fixed window and nothing re-ran it: on the
test VM the window ended 2026-09-23 and every availability search came back
empty until a client reported it on 2026-10-13. top_up() must extend a lapsed
or short window, never regenerate a day that already has inventory, and be a
no-op when the window is already long enough.

Offline: the seeded `db` fixture and generated inventory, no vendor.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import Booking, Slot
from app.services import slots as svc

TODAY = date(2026, 10, 13)


def _days(db) -> list[date]:
    return sorted({s.start.date() for s in db.scalars(select(Slot)).all()})


def _duplicate_groups(db) -> int:
    return len(
        db.execute(
            select(Slot.location_type, Slot.showroom_id, Slot.vehicle_id, Slot.variant_id, Slot.start)
            .group_by(Slot.location_type, Slot.showroom_id, Slot.vehicle_id, Slot.variant_id, Slot.start)
            .having(func.count() > 1)
        ).all()
    )


def test_empty_table_gets_a_full_window_from_today(db):
    result = svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY)

    assert result["added"] > 0
    days = _days(db)
    assert days[0] == TODAY
    assert days[-1] == TODAY + timedelta(days=13)


def test_lapsed_window_restarts_today_and_leaves_old_days_alone(db):
    """The VM case: inventory ended weeks ago, so nothing in the future exists."""
    old_start = TODAY - timedelta(days=30)
    svc.generate(db, days=14, prebooked=0.0, start_day=old_start)
    old_count = db.scalar(select(func.count()).select_from(Slot))

    result = svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY)

    assert result["window_start"] == TODAY.isoformat()
    assert result["window_end"] == (TODAY + timedelta(days=13)).isoformat()
    assert db.scalar(select(func.count()).select_from(Slot)) == old_count + result["added"]
    assert _duplicate_groups(db) == 0


def test_short_window_is_extended_from_the_day_after_its_last(db):
    svc.generate(db, days=5, prebooked=0.0, start_day=TODAY)

    result = svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY)

    assert result["window_start"] == (TODAY + timedelta(days=5)).isoformat()
    assert _days(db)[-1] == TODAY + timedelta(days=13)
    assert _duplicate_groups(db) == 0


def test_full_window_is_a_no_op(db):
    svc.generate(db, days=14, prebooked=0.0, start_day=TODAY)
    before = db.scalar(select(func.count()).select_from(Slot))

    result = svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY)

    assert result["added"] == 0
    assert db.scalar(select(func.count()).select_from(Slot)) == before


def test_daily_runs_add_one_day_each_and_never_duplicate(db):
    svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY)
    for n in range(1, 4):
        svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY + timedelta(days=n))
        # A second run on the same day is what a restart looks like.
        assert svc.top_up(db, horizon_days=14, prebooked=0.0, today=TODAY + timedelta(days=n))["added"] == 0

    days = _days(db)
    assert days == [TODAY + timedelta(days=i) for i in range(17)]
    assert _duplicate_groups(db) == 0


def test_existing_slots_and_their_bookings_are_untouched(db):
    svc.generate(db, days=3, prebooked=0.18, start_day=TODAY)
    before = {(s.id, s.status) for s in db.scalars(select(Slot)).all()}
    bookings_before = db.scalar(select(func.count()).select_from(Booking))

    svc.top_up(db, horizon_days=14, today=TODAY)

    after = {(s.id, s.status) for s in db.scalars(select(Slot)).all()}
    assert before <= after
    assert db.scalar(select(func.count()).select_from(Booking)) == bookings_before
