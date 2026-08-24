"""Slot generation + availability tests (FR-SL-01/02, S-05, S-22)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.calendar_rules import FRIDAY_PRAYER_END, FRIDAY_PRAYER_START
from app.models import LocationType, Slot, SlotStatus, VehicleModel
from app.services import slots as svc

WINDOW_START = date(2026, 7, 16)  # a Thursday


def _generate(db, **kw):
    kw.setdefault("start_day", WINDOW_START)
    return svc.generate(db, **kw)


def _all(db) -> list[Slot]:
    return list(db.scalars(__import__("sqlalchemy").select(Slot)).all())


# --------------------------------------------------------------------------- #
# S-22 — a forbidden slot cannot be GENERATED, so it cannot be offered
# --------------------------------------------------------------------------- #
def test_no_generated_slot_falls_in_the_friday_prayer_window(db):
    _generate(db, days=14, prebooked=0.0)
    for s in _all(db):
        if s.start.weekday() == 4:
            w_start = datetime.combine(s.start.date(), FRIDAY_PRAYER_START)
            w_end = datetime.combine(s.start.date(), FRIDAY_PRAYER_END)
            assert not (s.start < w_end and w_start < s.end), f"{s.start} is in the window"


def test_friday_still_has_inventory(db):
    """Friday is a working day — blocking the window must not empty the day."""
    _generate(db, days=14, prebooked=0.0)
    assert any(s.start.weekday() == 4 for s in _all(db))


def test_sunday_generates_nothing_before_noon(db):
    """Falls out of hours_by_day — no special case in the generator."""
    _generate(db, days=14, prebooked=0.0)
    for s in _all(db):
        if s.start.weekday() == 6 and s.location_type == LocationType.showroom:
            assert s.start.hour >= 12, f"Sunday slot at {s.start}"


def test_showroom_slots_respect_opening_hours(db):
    _generate(db, days=14, prebooked=0.0)
    for s in _all(db):
        if s.location_type == LocationType.showroom:
            assert s.start.hour >= 9
            assert s.end <= datetime.combine(s.start.date(), time(21, 0))


# --------------------------------------------------------------------------- #
# grain: the removed G700-dedicated branch stays removed
# --------------------------------------------------------------------------- #
# REPLACES test_showroom_2_has_g700_slots_only, deleted 2026-07-23 with the
# showroom it described. That test pinned "showroom 2 carries G700 slots and
# nothing else" — a true and useful fact right up until the branch was removed
# from seed/showrooms.json, at which point it asserted a fixture that no longer
# exists. Rather than drop the coverage, it is inverted: the id must now yield
# NOTHING, which fails loudly if the row is ever reinstated without the slot
# generator, the vehicle showroom_ids and this file being reconciled.
def test_removed_g700_showroom_generates_no_slots(db):
    _generate(db, days=14, prebooked=0.0)

    assert [s for s in _all(db) if s.showroom_id == 2] == []


def test_the_g700_is_still_bookable_somewhere(db):
    """The point of the removal check above: the flagship must not have been
    orphaned by it. The G700 listed showroom 2 alongside five others, so
    dropping it should cost the car a venue, never all of them."""
    _generate(db, days=14, prebooked=0.0)
    g700 = db.scalar(
        __import__("sqlalchemy").select(VehicleModel).where(VehicleModel.model_code == "JET-G700")
    )

    showrooms = {s.showroom_id for s in _all(db) if s.vehicle_id == g700.id and s.showroom_id}
    assert showrooms, "the G700 has no showroom slots at all"
    assert 2 not in showrooms


def test_every_showroom_slot_is_for_a_vehicle_that_lists_it(db):
    _generate(db, days=14, prebooked=0.0)
    vehicles = {v.id: set(v.showroom_ids or []) for v in db.scalars(
        __import__("sqlalchemy").select(VehicleModel)
    ).all()}
    for s in _all(db):
        if s.location_type == LocationType.showroom:
            assert s.showroom_id in vehicles[s.vehicle_id]


# --------------------------------------------------------------------------- #
# doorstep
# --------------------------------------------------------------------------- #
def test_doorstep_is_dubai_evenings_and_excludes_the_flagship(db):
    _generate(db, days=14, prebooked=0.0)
    g700 = db.scalar(
        __import__("sqlalchemy").select(VehicleModel).where(VehicleModel.model_code == "JET-G700")
    )
    door = [s for s in _all(db) if s.location_type == LocationType.doorstep]
    assert door
    assert all(s.service_area == "Dubai" for s in door)
    assert all(s.showroom_id is None for s in door)
    assert all(s.start.hour >= 17 for s in door)
    assert g700.id not in {s.vehicle_id for s in door}
    assert len({s.vehicle_id for s in door}) == 8


# --------------------------------------------------------------------------- #
# pre-booking (S-05) — deterministic, ~18%
# --------------------------------------------------------------------------- #
def test_prebooked_ratio_is_about_eighteen_percent(db):
    result = _generate(db, days=14, prebooked=0.18)
    assert abs(result["prebooked_ratio"] - 0.18) < 0.005
    rows = _all(db)
    booked = sum(1 for s in rows if s.status == SlotStatus.booked)
    assert abs(booked / len(rows) - 0.18) < 0.005


def test_prebooking_is_deterministic_across_runs(db):
    a = _generate(db, days=14, prebooked=0.18, reset=True)
    first = sorted(
        (s.start.isoformat(), s.vehicle_id, s.showroom_id or -1)
        for s in _all(db)
        if s.status == SlotStatus.booked
    )
    b = _generate(db, days=14, prebooked=0.18, reset=True)
    second = sorted(
        (s.start.isoformat(), s.vehicle_id, s.showroom_id or -1)
        for s in _all(db)
        if s.status == SlotStatus.booked
    )
    assert first == second, "same inputs must mark the same slots"
    assert a["total"] == b["total"]


def test_zero_prebooked_leaves_everything_open(db):
    _generate(db, days=14, prebooked=0.0)
    assert all(s.status == SlotStatus.open for s in _all(db))


# --------------------------------------------------------------------------- #
# --reset is idempotent and re-runnable
# --------------------------------------------------------------------------- #
def test_reset_is_idempotent(db):
    a = _generate(db, days=14, prebooked=0.18, reset=True)
    n1 = len(_all(db))
    b = _generate(db, days=14, prebooked=0.18, reset=True)
    n2 = len(_all(db))
    assert n1 == n2 == a["total"] == b["total"]


def test_generating_without_reset_against_nonempty_is_refused(db):
    """Fix 1: a second generate WITHOUT --reset used to silently DOUBLE the
    inventory (the duplicate-slot bug). It must now abort as data, leaving the
    first window untouched — so the double-run cannot happen by accident.
    """
    a = _generate(db, days=2, prebooked=0.0)
    n1 = len(_all(db))
    out = _generate(db, days=2, prebooked=0.0)  # no reset, inventory not empty
    assert out["error"] == "inventory_not_empty"
    assert out["existing"] == a["total"]
    assert len(_all(db)) == n1, "the refused run must not have added anything"


def test_force_appends_without_reset(db):
    """--force is the explicit 'append anyway' escape hatch (only --reset empties
    first). It bypasses the non-empty guard, so it still stacks — deliberately.
    """
    a = _generate(db, days=2, prebooked=0.0)
    _generate(db, days=2, prebooked=0.0, force=True)
    assert len(_all(db)) == a["total"] * 2


def test_generating_into_empty_inventory_needs_no_flag(db):
    """The guard only bites a NON-empty table — the first, clean generate (and the
    demo-morning `--reset` path) just works."""
    out = _generate(db, days=2, prebooked=0.0)
    assert "error" not in out and out["total"] > 0


# --------------------------------------------------------------------------- #
# query_available (FR-SL-01)
# --------------------------------------------------------------------------- #
def _vid(db, code: str) -> int:
    return db.scalar(
        __import__("sqlalchemy").select(VehicleModel).where(VehicleModel.model_code == code)
    ).id


def test_query_returns_open_slots_with_human_and_iso_forms(db):
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), now=datetime.combine(WINDOW_START, time(0, 0))
    )
    assert out["slots"]
    s = out["slots"][0]
    assert "start_iso" in s and "start_human" in s
    assert "AM" in s["start_human"] or "PM" in s["start_human"]  # 12-hour (LOC-09)


def test_query_never_returns_booked_slots(db):
    _generate(db, days=14, prebooked=0.18)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), now=datetime.combine(WINDOW_START, time(0, 0)), limit=999
    )
    ids = {s["slot_id"] for s in out["slots"]}
    booked = {s.id for s in _all(db) if s.status == SlotStatus.booked}
    assert not (ids & booked)


def test_past_dates_return_empty_not_yesterdays_inventory(db):
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db,
        vehicle_id=_vid(db, "JET-T2"),
        date_from=(WINDOW_START - timedelta(days=5)).isoformat(),
        date_to=(WINDOW_START - timedelta(days=1)).isoformat(),
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"] == []
    assert "error" not in out


def test_date_from_after_date_to_is_data_not_a_crash(db):
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), date_from="2026-07-20", date_to="2026-07-18"
    )
    assert out["error"] == "invalid_date_range"
    assert out["slots"] == []


def test_unparseable_date_is_data_not_a_crash(db):
    """The agent must resolve "tomorrow" itself — this is invalid input here."""
    out = svc.query_available(db, vehicle_id=_vid(db, "JET-T2"), date_from="tomorrow")
    assert out["error"] == "invalid_date_from"


def test_unknown_vehicle_is_data_not_a_crash(db):
    assert svc.query_available(db, vehicle_id=99)["error"] == "vehicle_not_found"


def test_invalid_daypart_is_data_not_a_crash(db):
    out = svc.query_available(db, vehicle_id=_vid(db, "JET-T2"), daypart="dawn")
    assert out["error"] == "invalid_daypart"


def test_no_slots_at_all_is_an_empty_list_not_an_error(db):
    """Nothing generated — an honest "we have nothing", not a failure."""
    out = svc.query_available(db, vehicle_id=_vid(db, "JET-T2"))
    assert out["slots"] == []
    assert "error" not in out


def test_sunday_morning_daypart_is_naturally_empty(db):
    """Sunday opens at 12:00, so "Sunday morning" has no inventory — from the
    hours, with no special case.
    """
    _generate(db, days=14, prebooked=0.0)
    sunday = WINDOW_START + timedelta(days=(6 - WINDOW_START.weekday()) % 7)
    out = svc.query_available(
        db,
        vehicle_id=_vid(db, "JET-T2"),
        date_from=sunday.isoformat(),
        date_to=sunday.isoformat(),
        daypart="morning",
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"] == []


def test_daypart_boundaries(db):
    _generate(db, days=14, prebooked=0.0)
    vid = _vid(db, "JET-T2")
    now = datetime.combine(WINDOW_START, time(0, 0))
    for part, check in (
        ("morning", lambda h: h < 12),
        ("afternoon", lambda h: 12 <= h < 17),
        ("evening", lambda h: h >= 17),
    ):
        out = svc.query_available(db, vehicle_id=vid, daypart=part, now=now, limit=999)
        assert out["slots"], f"{part} should have inventory"
        for s in out["slots"]:
            assert check(datetime.fromisoformat(s["start_iso"]).hour), f"{part}: {s['start_iso']}"


def test_default_cap_is_three_slots(db):
    """Fix 2 (FR-SL-01): the tool returns AT MOST 3 — the same set the agent
    offers, so prose and chips cannot diverge. The old 6-slot 'fallback' superset
    is gone (it was the source of the prose/chips mismatch)."""
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), now=datetime.combine(WINDOW_START, time(0, 0))
    )
    assert svc.DEFAULT_SLOT_LIMIT == 3
    assert len(out["slots"]) == 3  # inventory has plenty; the CAP is what limits it


def test_limit_is_still_overridable_for_inspection(db):
    """The cap is a default, not a hard wall: callers that need the full picture
    (console, tests) can still pass limit explicitly."""
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), now=datetime.combine(WINDOW_START, time(0, 0)), limit=999
    )
    assert len(out["slots"]) > 3


# --------------------------------------------------------------------------- #
# earliest_hour (Fix 3, FR-SL-04) — a stated time constraint filters server-side
# --------------------------------------------------------------------------- #
def test_earliest_hour_excludes_earlier_slots(db):
    """'I've office at 9am' / 'nothing before 11' -> earliest_hour=11 -> no slot
    starting before 11 is RETURNED, so none can reach the chips."""
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db,
        vehicle_id=_vid(db, "JET-T2"),
        earliest_hour=11,
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"]
    for s in out["slots"]:
        assert datetime.fromisoformat(s["start_iso"]).hour >= 11, s["start_iso"]


def test_earliest_hour_accepts_hh_mm_string(db):
    """Graceful parse: 'earliest_hour: "11:00"' still lands on hour 11."""
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), earliest_hour="11:00",
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"]
    for s in out["slots"]:
        assert datetime.fromisoformat(s["start_iso"]).hour >= 11


def test_earliest_hour_bad_value_is_ignored_not_an_error(db):
    """A SOFT filter must never hard-error a live turn (unlike an invalid daypart/
    date). An uninterpretable earliest_hour drops the bound and returns slots."""
    _generate(db, days=14, prebooked=0.0)
    for bad in ("later", 99, -3, "", None):
        out = svc.query_available(
            db, vehicle_id=_vid(db, "JET-T2"), earliest_hour=bad,
            now=datetime.combine(WINDOW_START, time(0, 0)),
        )
        assert "error" not in out
        assert out["slots"], f"bad earliest_hour={bad!r} should just drop the bound"


def test_earliest_hour_composes_with_daypart(db):
    """earliest_hour refines daypart, it does not fight it: morning + earliest 11
    -> only 11:00-11:59 slots (11am is still 'morning', which daypart alone can't
    narrow)."""
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db, vehicle_id=_vid(db, "JET-T2"), daypart="morning", earliest_hour=11,
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    for s in out["slots"]:
        h = datetime.fromisoformat(s["start_iso"]).hour
        assert 11 <= h < 12, s["start_iso"]


def test_reject_all_three_then_requery_returns_different_slots(db):
    """The reject-all path (more common now the offer is only 3): a fresh query
    with the customer's new constraint returns DIFFERENT, in-bound slots — no
    overlap with the first three, and none before the bound."""
    _generate(db, days=14, prebooked=0.0)
    vid = _vid(db, "JET-T2")
    now = datetime.combine(WINDOW_START, time(0, 0))
    first = svc.query_available(db, vehicle_id=vid, now=now)
    first_ids = {s["slot_id"] for s in first["slots"]}
    assert len(first_ids) == 3

    # "none of those work — nothing before 6pm" -> re-query with the bound.
    second = svc.query_available(db, vehicle_id=vid, earliest_hour=18, now=now)
    second_ids = {s["slot_id"] for s in second["slots"]}
    assert second_ids and not (first_ids & second_ids), "fresh offer must be different slots"
    for s in second["slots"]:
        assert datetime.fromisoformat(s["start_iso"]).hour >= 18


def test_doorstep_slots_carry_service_area_not_showroom(db):
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db,
        vehicle_id=_vid(db, "JET-X50"),
        location_type="doorstep",
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"]
    for s in out["slots"]:
        assert s["service_area"] == "Dubai"
        assert "showroom" not in s


def test_doorstep_unavailable_for_the_flagship_is_empty_not_an_error(db):
    _generate(db, days=14, prebooked=0.0)
    out = svc.query_available(
        db,
        vehicle_id=_vid(db, "JET-G700"),
        location_type="doorstep",
        now=datetime.combine(WINDOW_START, time(0, 0)),
    )
    assert out["slots"] == []
    assert "error" not in out
