"""Test-drive inventory is grained per TRIM, not per model (2026-09-03).

WHAT THIS PINS AND WHY IT IS NOT OBVIOUS. `slots.generate` used to iterate
(showroom, model) on the stated premise that "a showroom genuinely has one demo
car per model". Three of nine models carry several trims — the G700 spans AED
169,000 to 240,450 across four — so a customer booked "a G700" and there was
nowhere to record, and no way to ask, which one they were coming to drive.

The premise is now "one demo car per BOOKABLE TRIM", and it is Elite's to
confirm (OPEN-ISSUES §7). These tests pin the CONSEQUENCES of it, so that a
re-import, a reseed or a well-meant simplification back to model grain fails
here rather than shipping a customer the wrong car.

THE COMING-SOON EXCLUSION IS THE SUBTLE ONE. A trim that cannot be ordered
cannot be driven, and `coming_soon` — not a name, not a null price — is the
predicate. Hardcoding "Top Fire" would pass today and rot the moment Elite adds
a second one.

Offline: the seeded `db` fixture and generated inventory, no vendor.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models import Booking, LocationType, Slot, VehicleModel, VehicleVariant
from app.services import slots as svc

WINDOW_START = date(2026, 7, 16)  # a Thursday, matching test_slots.py


def _generate(db, **kw):
    kw.setdefault("start_day", WINDOW_START)
    kw.setdefault("prebooked", 0.0)
    return svc.generate(db, **kw)


def _slots(db) -> list[Slot]:
    return list(db.scalars(select(Slot)).all())


# --------------------------------------------------------------------------- #
# The grain itself
# --------------------------------------------------------------------------- #
def test_every_generated_slot_names_a_trim(db):
    """A NULL variant_id is pre-migration inventory and generate never writes one.

    This is the assertion the whole change rests on: query_available refuses to
    satisfy an explicit variant filter from a NULL row, so a generator that left
    any NULL behind would produce inventory that silently answers "no times".
    """
    _generate(db, days=14)
    assert [s.id for s in _slots(db) if s.variant_id is None] == []


def test_a_trim_that_cannot_be_ordered_gets_no_inventory(db):
    """Coming soon is not sold out: it cannot be driven at all."""
    _generate(db, days=14)
    soon = {
        v.id
        for v in db.scalars(
            select(VehicleVariant).where(VehicleVariant.coming_soon.is_(True))
        ).all()
    }
    assert soon, "the fixture must carry at least one coming-soon trim or this proves nothing"
    assert [s.id for s in _slots(db) if s.variant_id in soon] == []


def test_each_branch_stocks_every_bookable_trim_of_the_models_it_lists(db):
    """The grain, stated as the pairing it produces.

    Not "more slots than before" — that is a symptom. The property is that the
    (branch, trim) pairs are exactly the (branch, model) pairs expanded by each
    model's bookable trims.
    """
    _generate(db, days=14)
    expected: set[tuple[int, int]] = set()
    for model in db.scalars(select(VehicleModel)).all():
        variants = db.scalars(
            select(VehicleVariant).where(
                VehicleVariant.model_id == model.id,
                VehicleVariant.coming_soon.is_(False),
            )
        ).all()
        for showroom_id in model.showroom_ids or []:
            for variant in variants:
                expected.add((showroom_id, variant.id))

    actual = {
        (s.showroom_id, s.variant_id)
        for s in _slots(db)
        if s.location_type == LocationType.showroom
    }
    assert actual == expected


def test_a_slots_trim_always_belongs_to_its_own_model(db):
    """The two columns must never disagree — a Dashing slot carrying a G700 trim
    would let create_booking write a booking for a car that was never offered."""
    _generate(db, days=14)
    owner = {
        v.id: v.model_id for v in db.scalars(select(VehicleVariant)).all()
    }
    mismatched = [
        (s.id, s.vehicle_id, s.variant_id)
        for s in _slots(db)
        if owner.get(s.variant_id) != s.vehicle_id
    ]
    assert mismatched == []


def test_the_doorstep_fleet_is_per_trim_too(db):
    """Split the grain in one path and not the other and they disagree — the
    shape of defect this file's history is mostly made of."""
    _generate(db, days=14)
    door = [s for s in _slots(db) if s.location_type == LocationType.doorstep]
    assert door, "the fixture must generate doorstep inventory"
    assert all(s.variant_id is not None for s in door)
    multi = [
        s for s in door
        if len(
            db.scalars(
                select(VehicleVariant).where(
                    VehicleVariant.model_id == s.vehicle_id,
                    VehicleVariant.coming_soon.is_(False),
                )
            ).all()
        ) > 1
    ]
    assert len({s.variant_id for s in multi}) > 1, (
        "a multi-trim model that travels must put more than one trim on the road"
    )


# --------------------------------------------------------------------------- #
# The columns, and what a NULL is allowed to mean
# --------------------------------------------------------------------------- #
def test_a_booking_can_carry_the_trim_it_is_for(db):
    """Denormalised from the slot on purpose — a booking outlives the fortnightly
    slot window, so a trim reachable only through Slot.variant_id would be lost
    the first time the window rolled."""
    assert "variant_id" in Booking.__table__.c
    assert Booking.__table__.c.variant_id.nullable, (
        "NULL means 'we do not know which trim', which is the honest reading of "
        "the historic rows; it must stay expressible"
    )


def test_the_report_counts_demo_cars_not_just_models(db):
    """Nine models are fourteen demo cars at this grain. A report that said only
    'vehicles: 9' would describe a branch's fleet as a third smaller than it is."""
    report = _generate(db, days=14)
    for branch in report["per_showroom"]:
        assert branch["variants"] >= branch["vehicles"]
    assert any(b["variants"] > b["vehicles"] for b in report["per_showroom"])
