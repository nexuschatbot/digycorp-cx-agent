"""STANDING GUARD: the console's Status filter agrees with the status it draws.

THE DECISION (2026-07-28). The console shows a `rescheduled` booking as
"Confirmed": a moved booking is exactly as alive as one never moved, and no
operator decision turns on which of the two it is. The blue "Rescheduled" pill
beside the green ones read as a warning about nothing.

WHY THAT NEEDED A BACKEND CHANGE AT ALL, which is the whole point of this file.
Relabelling the pill is one line of TSX and looks finished. It is not: the Status
filter is SERVER-SIDE, so a card reading "Confirmed" then vanished the moment you
filtered for Confirmed. A screen that contradicts itself that plainly makes an
operator distrust everything else on it. Found by clicking the filter, not by
reasoning about it.

WHAT IS PINNED:
  * status=confirmed returns confirmed AND rescheduled (the filter matches the
    badge)
  * status=rescheduled still returns ONLY rescheduled, so "which were moved"
    stays answerable even though the filter row no longer asks it
  * status=cancelled is unaffected
  * THE STORED STATUS IS UNTOUCHED. This is the assertion that matters most:
    `rescheduled` is load-bearing in the schema (the partial unique index on
    slot_id covers WHERE status IN ('confirmed','rescheduled'); reschedule and
    cancel both read it; the dashboard funnel counts reschedules as their own
    stage). If anyone ever "simplifies" this by writing `confirmed` into the
    column instead, that is a data change wearing a display change's clothes,
    and this test is what should stop it.

DB-only. Nothing here calls a model or a vendor.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import Booking, BookingStatus, Slot, SlotStatus
from app.services import booking, console, customers
from app.services import slots as slots_service

SESSION = "sess-console-status"


def _customer(db, mobile: str) -> int:
    return customers.find_or_create(db, mobile=mobile)["customer"]["customer_id"]


def _open_slots(db) -> list[Slot]:
    """Far-future open showroom slots, earliest first."""
    cutoff = datetime.now() + timedelta(hours=25)
    return list(
        db.scalars(
            select(Slot)
            .where(
                Slot.status == SlotStatus.open,
                Slot.location_type == "showroom",
                Slot.start > cutoff,
            )
            .order_by(Slot.start, Slot.id)
            .limit(60)
        ).all()
    )


def _same_vehicle_pair(pool: list[Slot]) -> tuple[Slot, Slot]:
    """Two open slots for the SAME vehicle.

    Reschedule keeps the car (`vehicle_slot_mismatch` otherwise), so a move needs
    a second slot on the same model — not merely the next slot in the calendar.
    """
    by_vehicle: dict[int, list[Slot]] = {}
    for s in pool:
        by_vehicle.setdefault(s.vehicle_id, []).append(s)
    for group in by_vehicle.values():
        if len(group) >= 2:
            return group[0], group[1]
    raise AssertionError("no vehicle has two open far-future slots")


def _book(db, cid: int, slot: Slot) -> dict:
    """A real booking through the real guards, never an INSERT."""
    assert slots_service.lock(db, session_id=SESSION, slot_id=slot.id)["locked"] is True
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)
    out = booking.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
    )
    assert out.get("created") is True, out
    return out["booking"]


def _refs(payload: dict) -> set[str]:
    return {r["ref"] for r in payload["rows"]}


def _fixture(db) -> tuple[str, str]:
    """One plain confirmed booking and one that has been rescheduled.

    Returns (confirmed_ref, rescheduled_ref).
    """
    pool = _open_slots(db)
    origin, target = _same_vehicle_pair(pool)

    plain_slot = next(s for s in pool if s.id not in (origin.id, target.id))
    plain = _book(db, _customer(db, "+971501112255"), plain_slot)

    mover_id = _customer(db, "+971501112266")
    moved = _book(db, mover_id, origin)

    assert slots_service.lock(db, session_id=SESSION, slot_id=target.id)["locked"] is True
    out = booking.reschedule(
        db,
        session_id=SESSION,
        ref=moved["booking_ref"],
        new_slot_id=target.id,
        session_customer_id=mover_id,
    )
    assert out.get("rescheduled") is True, out

    row = db.scalar(select(Booking).where(Booking.ref == moved["booking_ref"]))
    assert row.status == BookingStatus.rescheduled, (
        "fixture precondition: reschedule must leave the stored status as "
        "`rescheduled`, or this file is testing nothing"
    )
    return plain["booking_ref"], moved["booking_ref"]


# --------------------------------------------------------------------------- #
# The filter matches the badge
# --------------------------------------------------------------------------- #
def test_confirmed_returns_rescheduled_bookings_too(db, slots):
    confirmed_ref, rescheduled_ref = _fixture(db)

    got = _refs(console.list_bookings(db, status="confirmed"))
    assert confirmed_ref in got
    assert rescheduled_ref in got, (
        "a booking the console DRAWS as Confirmed must come back when you filter "
        "for Confirmed"
    )


def test_rescheduled_still_selects_only_moved_bookings(db, slots):
    confirmed_ref, rescheduled_ref = _fixture(db)

    got = _refs(console.list_bookings(db, status="rescheduled"))
    assert rescheduled_ref in got
    assert confirmed_ref not in got


def test_cancelled_is_unaffected(db, slots):
    confirmed_ref, rescheduled_ref = _fixture(db)

    got = _refs(console.list_bookings(db, status="cancelled"))
    assert confirmed_ref not in got
    assert rescheduled_ref not in got


def test_the_total_counts_the_widened_set(db, slots):
    """The count query has to widen with the filter, or the header disagrees
    with the cards under it — the same class of bug this whole file is about."""
    _fixture(db)

    payload = console.list_bookings(db, status="confirmed")
    assert payload["total"] == len(payload["rows"])


# --------------------------------------------------------------------------- #
# The stored value is untouched — display only
# --------------------------------------------------------------------------- #
def test_the_stored_status_is_still_rescheduled(db, slots):
    """Relabelling is a DISPLAY decision. The column keeps its own vocabulary."""
    _, rescheduled_ref = _fixture(db)

    row = db.scalar(select(Booking).where(Booking.ref == rescheduled_ref))
    assert row.status == BookingStatus.rescheduled

    payload = console.list_bookings(db, status="confirmed")
    moved = next(r for r in payload["rows"] if r["ref"] == rescheduled_ref)
    assert moved["status"] == BookingStatus.rescheduled.value, (
        "the API must keep reporting the real status; the console maps it to the "
        "word 'Confirmed' at the moment of drawing, and nowhere else"
    )
