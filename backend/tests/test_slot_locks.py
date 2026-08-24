"""Slot locking tests (FR-SL, FR-SL-06).

Covers the auto-release decision (lock_slot releases this session's previous
hold) and lazy TTL expiry (no background job — AD-F).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import SLOT_LOCK_TTL_MINUTES
from app.models import SlotStatus
from app.services import slots as slots_service

SESSION = "sess-aaa"
OTHER_SESSION = "sess-bbb"


def _two(db):
    from sqlalchemy import select

    from app.models import LocationType, Slot

    rows = db.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
    ).all()
    return list(rows)[:2]


# --------------------------------------------------------------------------- #
# Basic locking
# --------------------------------------------------------------------------- #
def test_lock_marks_the_slot_locked_and_records_the_owner(db, open_slot):
    out = slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    assert out["locked"] is True
    db.refresh(open_slot)
    assert open_slot.status == SlotStatus.locked
    assert open_slot.locked_by_session == SESSION


def test_locking_someone_elses_lock_fails_as_data(db, open_slot):
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=open_slot.id)
    out = slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    assert out == {
        "locked": False,
        "reason": "slot_no_longer_available",
        "slot_id": open_slot.id,
    }


def test_relocking_our_own_lock_is_idempotent(db, open_slot):
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    assert out["locked"] is True
    assert out["already_held"] is True


def test_unknown_slot_is_data_not_an_exception(db, slots):
    out = slots_service.lock(db, session_id=SESSION, slot_id=99999)
    assert out["locked"] is False
    assert out["reason"] == "slot_not_found"


def test_locking_a_booked_slot_fails(db, open_slot):
    open_slot.status = SlotStatus.booked
    db.flush()
    out = slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    assert out["reason"] == "slot_no_longer_available"


# --------------------------------------------------------------------------- #
# AUTO-RELEASE — "actually, make it 8pm"
# --------------------------------------------------------------------------- #
def test_locking_a_new_slot_releases_the_previous_one(db, slots):
    """A changed mind must not strand the first slot for the full TTL. The model
    is not asked to remember to tidy up.
    """
    a, b = _two(db)
    slots_service.lock(db, session_id=SESSION, slot_id=a.id)
    out = slots_service.lock(db, session_id=SESSION, slot_id=b.id)

    assert out["locked"] is True
    assert out["released_previous_lock"] is True
    db.refresh(a)
    db.refresh(b)
    assert a.status == SlotStatus.open and a.locked_by_session is None
    assert b.status == SlotStatus.locked and b.locked_by_session == SESSION


def test_auto_release_only_touches_our_own_lock(db, slots):
    a, b = _two(db)
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=a.id)
    slots_service.lock(db, session_id=SESSION, slot_id=b.id)

    db.refresh(a)
    assert a.status == SlotStatus.locked
    assert a.locked_by_session == OTHER_SESSION, "another session's hold is untouchable"


def test_a_failed_lock_does_not_release_the_one_we_hold(db, slots):
    """Releasing first would leave the customer holding NOTHING when the new
    slot turns out to be taken.
    """
    a, b = _two(db)
    slots_service.lock(db, session_id=SESSION, slot_id=a.id)
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=b.id)  # b is taken

    out = slots_service.lock(db, session_id=SESSION, slot_id=b.id)
    assert out["locked"] is False

    db.refresh(a)
    assert a.status == SlotStatus.locked
    assert a.locked_by_session == SESSION, "we must still hold our original slot"


def test_release_lock_is_scoped_to_the_session(db, slots):
    a, b = _two(db)
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=a.id)
    slots_service.lock(db, session_id=SESSION, slot_id=b.id)

    assert slots_service.release_lock(db, session_id=SESSION) == 1
    db.refresh(a)
    db.refresh(b)
    assert a.status == SlotStatus.locked  # not ours, untouched
    assert b.status == SlotStatus.open


# --------------------------------------------------------------------------- #
# LAZY TTL EXPIRY — no background job (AD-F)
# --------------------------------------------------------------------------- #
def test_expired_lock_is_released_on_the_next_query(db, open_slot):
    """An abandoned conversation must return inventory. With no scheduler, the
    next query is the only thing that can notice.
    """
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    later = datetime.now() + timedelta(minutes=SLOT_LOCK_TTL_MINUTES + 1)

    out = slots_service.query_available(db, vehicle_id=open_slot.vehicle_id, now=later)
    db.refresh(open_slot)
    assert open_slot.status == SlotStatus.open
    assert open_slot.locked_by_session is None
    assert open_slot.id in {s["slot_id"] for s in out["slots"]}


def test_a_live_lock_is_not_released_by_a_query(db, open_slot):
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    soon = datetime.now() + timedelta(minutes=SLOT_LOCK_TTL_MINUTES - 1)

    slots_service.query_available(db, vehicle_id=open_slot.vehicle_id, now=soon)
    db.refresh(open_slot)
    assert open_slot.status == SlotStatus.locked


def test_a_locked_slot_is_invisible_to_availability(db, open_slot):
    """The point of a lock: nobody else is offered it while we finish."""
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = slots_service.query_available(db, vehicle_id=open_slot.vehicle_id)
    assert open_slot.id not in {s["slot_id"] for s in out["slots"]}


def test_expired_lock_can_be_taken_by_another_session(db, open_slot):
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    later = datetime.now() + timedelta(minutes=SLOT_LOCK_TTL_MINUTES + 1)

    out = slots_service.lock(db, session_id=OTHER_SESSION, slot_id=open_slot.id, now=later)
    assert out["locked"] is True
    db.refresh(open_slot)
    assert open_slot.locked_by_session == OTHER_SESSION


# --------------------------------------------------------------------------- #
# The TTL is never handed to the agent (FR-CX-04, LOC-10)
# --------------------------------------------------------------------------- #
def test_lock_result_never_exposes_an_expiry(db, open_slot):
    """A model that knows the lock expires at 19:42 will eventually say "it
    expires in ten minutes" — a scarcity tactic the tone rules forbid outright.
    Expiry is inventory hygiene, not a conversational lever.
    """
    out = slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    blob = str(out).lower()
    assert "expire" not in blob
    assert "ttl" not in blob
    assert "locked_at" not in blob
