"""The two constraints a customer states and the agent used to drop (2026-07-27).

Both were found in a live chat, and both are the same shape of failure: the
customer said something that narrows the query, and the narrowing reached the
PROSE but never the TOOL CALL, so get_available_slots came back with slots that
could not have been right.

  #9  "same date and showroom" / a reschedule of a booking at Rabdan -> the agent
      offered Al Quoz, Deira and Al Ain. Changing the TIME of a booking silently
      moved the customer to another emirate.
  #8  "back to back slots for another car, same day" -> the first drive ran
      11:00-12:30 and the earliest slot offered was 13:00, with no mention of
      the gap and no guard against overlapping the drive they already had.

Both are prompt-and-schema fixes: showroom_id and earliest_hour ALREADY EXIST as
parameters (services/slots.query_available applies both server-side, and
tests/test_slots.py proves that). Nothing was missing except the instruction to
pass them, so what is pinned here is the INSTRUCTION — in the prompt body, in the
RESCHEDULING section, and in the tool schema the model reads at call time.

OFFLINE ONLY (CLAUDE.md): pure string assertions over the prompt and the schema,
plus the real slots service against the in-memory DB. No model, no vendor call.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.agent.prompt import system_prompt
from app.models import Showroom, Slot, SlotStatus, VehicleModel
from app.services import slots as slots_service
from app.tools.slot_tools import GET_AVAILABLE_SLOTS_SCHEMA

_PROPS = GET_AVAILABLE_SLOTS_SCHEMA["input_schema"]["properties"]


# --------------------------------------------------------------------------- #
# #9 — the branch already in play
# --------------------------------------------------------------------------- #
def test_prompt_tells_the_agent_to_keep_the_branch():
    p = system_prompt()
    assert "STAY AT THE BRANCH THEY ALREADY HAVE" in p
    # The reschedule journey states it too — that is where it actually bit.
    assert "AND SO DOES THE BRANCH" in p
    # And the honesty half: the empty branch is named BEFORE the alternatives,
    # so a substitution never arrives disguised as an answer.
    assert "BEFORE" in p


def test_showroom_id_schema_covers_the_reschedule_case():
    desc = _PROPS["showroom_id"]["description"]
    assert "RESCHEDULING" in desc
    assert "has not asked to change its city" in desc


# --------------------------------------------------------------------------- #
# #8 — a second drive on a day they are already coming in
# --------------------------------------------------------------------------- #
def test_prompt_tells_the_agent_a_second_drive_runs_after_the_first():
    p = system_prompt()
    assert "A SECOND DRIVE ON A DAY THEY ARE ALREADY COMING IN RUNS AFTER THE FIRST ONE." in p
    assert "earliest_hour" in p
    # It must say to READ the end time rather than guess a slot length: the agent
    # has no business assuming every drive is 90 minutes.
    assert "the hour it ENDS" in p


def test_earliest_hour_schema_covers_the_back_to_back_case():
    desc = _PROPS["earliest_hour"]["description"]
    assert "back to back" in desc
    assert "cannot overlap" in desc


# --------------------------------------------------------------------------- #
# The service half of both, so the instructions above are asking for something
# that actually works. These are the guarantees the prompt is relying on.
# --------------------------------------------------------------------------- #
def test_showroom_id_really_restricts_and_earliest_hour_really_bounds(slots):
    db = slots
    vehicle = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X70FL"))
    day = date.today() + timedelta(days=2)

    wide = slots_service.query_available(
        db, vehicle_id=vehicle.id, date_from=day.isoformat(), date_to=day.isoformat(), limit=50
    )
    branches = {
        (s.get("showroom") or {}).get("showroom_id")
        for s in wide["slots"]
        if s.get("showroom")
    }
    assert len(branches) > 1, "fixture should span several branches for this to mean anything"

    target = sorted(b for b in branches if b is not None)[-1]
    narrowed = slots_service.query_available(
        db, vehicle_id=vehicle.id, showroom_id=target,
        date_from=day.isoformat(), date_to=day.isoformat(), limit=50,
    )
    assert narrowed["slots"], "the branch should have inventory that day"
    assert all(
        (s.get("showroom") or {}).get("showroom_id") == target for s in narrowed["slots"]
    ), "showroom_id must restrict to exactly that branch"

    # earliest_hour is the other half: a second drive ending at 12:30 means no
    # slot may START before 13:00, and the bound is applied server-side so a
    # ruled-out slot can never reach a tappable chip.
    bounded = slots_service.query_available(
        db, vehicle_id=vehicle.id, date_from=day.isoformat(), date_to=day.isoformat(),
        earliest_hour=13, limit=50,
    )
    assert bounded["slots"], "there should be afternoon inventory to bound"
    assert all(
        datetime.fromisoformat(s["start_iso"]).hour >= 13 for s in bounded["slots"]
    ), "earliest_hour must exclude everything starting earlier"


def test_the_two_constraints_compose(slots):
    """Both at once — the exact shape of "another car, same day, same showroom"."""
    db = slots
    vehicle = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X50"))
    day = date.today() + timedelta(days=2)
    branch = db.scalars(select(Showroom).order_by(Showroom.id)).first()

    out = slots_service.query_available(
        db, vehicle_id=vehicle.id, showroom_id=branch.id,
        date_from=day.isoformat(), date_to=day.isoformat(), earliest_hour=13, limit=50,
    )
    for s in out["slots"]:
        assert (s.get("showroom") or {}).get("showroom_id") == branch.id
        assert datetime.fromisoformat(s["start_iso"]).hour >= 13


def test_an_empty_branch_is_an_honest_empty_not_an_error(slots):
    """The prompt promises to say "that branch has nothing" before widening, which
    only works if an empty branch comes back as data rather than an error."""
    db = slots
    vehicle = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X50"))
    branch = db.scalars(select(Showroom).order_by(Showroom.id)).first()
    # Take every slot at that branch out of play, so the branch is genuinely empty.
    for s in db.scalars(
        select(Slot).where(Slot.vehicle_id == vehicle.id, Slot.showroom_id == branch.id)
    ).all():
        s.status = SlotStatus.booked
    db.flush()

    out = slots_service.query_available(db, vehicle_id=vehicle.id, showroom_id=branch.id)
    assert "error" not in out
    assert out["slots"] == []
    assert out["count"] == 0
