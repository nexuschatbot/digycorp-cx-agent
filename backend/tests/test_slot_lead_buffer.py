"""The minimum-lead buffer on offered slots (rule 6, FR-SL-01).

"Not in the past" was the old bar and it is too low: a slot starting in four
minutes is real, open inventory that no customer can physically reach, and
offering it promises something we already know cannot happen (FR-CX-07).

The buffer is enforced where availability is COMPUTED, so a too-soon slot never
reaches the model at all — the same structural approach as the Friday-prayer
window: the agent cannot offer what it is never given. That is why the first
test here asserts on query_available rather than on any prose.

DB-only and offline: no model, no vendor, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import SLOT_MIN_LEAD_MINUTES
from app.models import LocationType, Slot, SlotStatus
from app.services import slots as svc

SESSION = "sess-lead"


def _one_open_showroom_slot(slots) -> Slot:
    return slots.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
    ).first()


def _at(slots, slot: Slot, *, minutes_from: datetime, offset: int) -> datetime:
    """Move `slot` to `offset` minutes after the given moment and return that
    moment, so the caller can pass it as `now`."""
    slot.start = minutes_from + timedelta(minutes=offset)
    slot.end = slot.start + timedelta(minutes=60)
    slots.flush()
    return minutes_from


# --------------------------------------------------------------------------- #
# What is OFFERED
# --------------------------------------------------------------------------- #
def test_a_slot_inside_the_buffer_is_never_offered(slots):
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=SLOT_MIN_LEAD_MINUTES - 20)

    out = svc.query_available(slots, vehicle_id=slot.vehicle_id, now=now, limit=999)

    assert slot.id not in {s["slot_id"] for s in out["slots"]}
    # An empty result is an honest answer, never an error (the agent then offers
    # alternatives) — so the absence must not arrive as a failure.
    assert "error" not in out


def test_a_slot_beyond_the_buffer_is_still_offered(slots):
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=SLOT_MIN_LEAD_MINUTES + 10)

    out = svc.query_available(slots, vehicle_id=slot.vehicle_id, now=now, limit=999)

    assert slot.id in {s["slot_id"] for s in out["slots"]}


# --------------------------------------------------------------------------- #
# What can be GRABBED
# --------------------------------------------------------------------------- #
def test_locking_inside_the_buffer_is_refused_as_too_soon_not_as_past(slots):
    """Its own reason, deliberately. slot_in_the_past would be a false statement
    about a slot that has not happened yet — and the agent repeats what the tool
    tells it (rule 2)."""
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=SLOT_MIN_LEAD_MINUTES - 20)

    out = svc.lock(slots, session_id=SESSION, slot_id=slot.id, now=now)

    assert out["locked"] is False
    assert out["reason"] == "slot_too_soon"
    slots.refresh(slot)
    assert slot.status == SlotStatus.open  # refused, not consumed


def test_a_genuinely_past_slot_still_reads_as_past(slots):
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=-30)

    out = svc.lock(slots, session_id=SESSION, slot_id=slot.id, now=now)

    assert out["locked"] is False
    assert out["reason"] == "slot_in_the_past"


def test_locking_beyond_the_buffer_still_succeeds(slots):
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=SLOT_MIN_LEAD_MINUTES + 10)

    out = svc.lock(slots, session_id=SESSION, slot_id=slot.id, now=now)

    assert out["locked"] is True


# --------------------------------------------------------------------------- #
# The deliberate asymmetry
# --------------------------------------------------------------------------- #
def test_the_buffer_is_never_leaked_to_the_agent(slots):
    """Rule 14's corollary: the agent is not told the lock TTL, for fear it turns
    inventory hygiene into 'hurry, it expires'. A lead buffer is the same kind of
    fact and gets the same treatment — withheld, not merely discouraged."""
    slot = _one_open_showroom_slot(slots)
    now = _at(slots, slot, minutes_from=datetime.now(), offset=SLOT_MIN_LEAD_MINUTES + 10)

    out = svc.query_available(slots, vehicle_id=slot.vehicle_id, now=now, limit=999)

    blob = repr(out)
    for leak in ("lead", "buffer", "cutoff", "expires", "ttl"):
        assert leak not in blob.lower()
