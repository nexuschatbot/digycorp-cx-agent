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

from app import clock
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


# --------------------------------------------------------------------------- #
# Asking for a trim, and getting only that trim
# --------------------------------------------------------------------------- #
def _bookable(db, model_id: int) -> list[VehicleVariant]:
    """VehicleModel has no `variants` relationship — query, do not guess."""
    return list(
        db.scalars(
            select(VehicleVariant)
            .where(
                VehicleVariant.model_id == model_id,
                VehicleVariant.coming_soon.is_(False),
            )
            .order_by(VehicleVariant.id)
        ).all()
    )


def _live_window(db):
    """Generate inventory the availability query can actually return.

    WINDOW_START above is a fixed past Thursday, which is right for asserting the
    SHAPE of what generate() writes and useless for asking what is bookable — the
    lead buffer excludes all of it. These tests want live inventory, so they
    start the window today.
    """
    return svc.generate(db, days=14, prebooked=0.0, start_day=clock.today())


def _multi_trim_model(db) -> VehicleModel:
    for model in db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all():
        if len(_bookable(db, model.id)) > 1:
            return model
    raise AssertionError("the fixture must carry a model with several trims")


def test_asking_for_a_trim_returns_only_that_trim(db):
    _live_window(db)
    model = _multi_trim_model(db)
    wanted = _bookable(db, model.id)[0]
    out = svc.query_available(db, vehicle_id=model.id, variant_id=wanted.id)
    assert out.get("slots"), "a bookable trim must have inventory"
    assert {s["variant_id"] for s in out["slots"]} == {wanted.id}


def test_asking_without_a_trim_still_means_any_trim(db):
    """Callers that do not care must not be broken by the finer grain — and a
    single-trim model must never require the argument."""
    _live_window(db)
    model = _multi_trim_model(db)
    out = svc.query_available(db, vehicle_id=model.id)
    assert len({s["variant_id"] for s in out.get("slots", [])}) >= 1


def test_a_trim_from_a_different_model_is_its_own_error(db):
    """Not 'no times'. Answering a mistyped id with an empty list reads as 'that
    branch is full', which is a lie about availability."""
    _generate(db, days=14)
    model = _multi_trim_model(db)
    other = db.scalars(
        select(VehicleVariant).where(VehicleVariant.model_id != model.id)
    ).first()
    out = svc.query_available(db, vehicle_id=model.id, variant_id=other.id)
    assert out.get("error") == "variant_not_of_vehicle"
    assert out.get("slots") == []


def test_a_coming_soon_trim_says_so_rather_than_reading_as_a_full_branch(db):
    _generate(db, days=14)
    soon = db.scalars(
        select(VehicleVariant).where(VehicleVariant.coming_soon.is_(True))
    ).first()
    out = svc.query_available(db, vehicle_id=soon.model_id, variant_id=soon.id)
    assert out.get("error") == "variant_coming_soon"


def test_a_slot_with_no_trim_never_satisfies_a_trim_filter(db):
    """Pre-migration inventory. Matching it would hand a customer who asked for
    the Flagship whichever car the row really is — the defect this grain removes.
    Answering 'no times' is visible and wrong-safe; answering plausibly is not.
    """
    _generate(db, days=14)
    model = _multi_trim_model(db)
    wanted = _bookable(db, model.id)[0]
    for slot in db.scalars(select(Slot).where(Slot.vehicle_id == model.id)).all():
        slot.variant_id = None
    db.flush()
    out = svc.query_available(db, vehicle_id=model.id, variant_id=wanted.id)
    assert out.get("slots") == []


# --------------------------------------------------------------------------- #
# The booking records which car was driven
# --------------------------------------------------------------------------- #
def test_a_booking_records_the_trim_off_the_locked_slot(db):
    from app.services import booking as booking_svc
    from app.services import customers
    from app.services import slots as slots_svc

    _live_window(db)
    model = _multi_trim_model(db)
    wanted = _bookable(db, model.id)[1]
    offered = svc.query_available(db, vehicle_id=model.id, variant_id=wanted.id)
    slot_id = offered["slots"][0]["slot_id"]

    session = "s-trim"
    cid = customers.find_or_create(
        db, mobile="+971509990002", name="Trim Test"
    )["customer"]["customer_id"]
    slots_svc.lock(db, session_id=session, slot_id=slot_id)
    slots_svc.acknowledge_licence_reminder(db, session_id=session)
    res = booking_svc.create(
        db, session_id=session, customer_id=cid,
        vehicle_id=model.id, slot_id=slot_id,
    )
    assert res.get("booking", {}).get("variant_id") == wanted.id
    stored = db.scalar(
        select(Booking).where(Booking.ref == res["booking"]["booking_ref"])
    )
    assert stored.variant_id == wanted.id


def test_a_booking_whose_trim_disagrees_with_its_slot_is_refused(db):
    """Same rule as vehicle_slot_mismatch, one level down — and it costs more,
    because two trims of one model look alike in a way two models do not."""
    from app.services import booking as booking_svc
    from app.services import customers
    from app.services import slots as slots_svc

    _live_window(db)
    model = _multi_trim_model(db)
    bookable = _bookable(db, model.id)
    offered = svc.query_available(db, vehicle_id=model.id, variant_id=bookable[0].id)
    slot_id = offered["slots"][0]["slot_id"]

    session = "s-trim-mismatch"
    cid = customers.find_or_create(
        db, mobile="+971509990003", name="Trim Mismatch"
    )["customer"]["customer_id"]
    slots_svc.lock(db, session_id=session, slot_id=slot_id)
    slots_svc.acknowledge_licence_reminder(db, session_id=session)
    res = booking_svc.create(
        db, session_id=session, customer_id=cid,
        vehicle_id=model.id, slot_id=slot_id, variant_id=bookable[1].id,
    )
    assert res.get("error") == "variant_slot_mismatch"
