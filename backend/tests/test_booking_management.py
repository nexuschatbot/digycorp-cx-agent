"""Booking-management journey tests — look up / reschedule / cancel.

Covers the acceptance scenarios this journey unlocks (FR-RS, FR-CN, behaviour
rules 10/11):

  S-09  reschedule via lookup -> new slot -> OLD reminders gone, NEW ones exist
  S-11  repeated reschedules, no context loss (ref stable, only latest reminders)
  S-12  cancel -> retention offer accepted -> converts to reschedule
  S-13  cancel -> offer declined -> proceeds, reason optional, win-back flag set

plus the front-door reads and the write guards: get_booking by ref-only and by
mobile-only, honest not-found, reschedule idempotency + lock guard + vehicle
match, and cancel idempotency.

Like test_booking.py, these run against the REAL seed catalogue and REAL
generated inventory (conftest), so a green test is a claim about the demo that
ships — not about hand-made rows.

A NOTE ON "THE CANCEL DOESN'T LOOP" (rule 11). The single-retention-offer / no
second offer is a PROMPT behaviour (app/agent/prompt.py). The scripted-graph
harness replays canned turns and ignores the prompt, so no deterministic test can
prove the *model* won't offer twice — that belongs to a live-LLM eval (S-12/S-13).
What the suite proves instead is the guarantee that makes a stray loop HARMLESS:
cancel is immediate and idempotent, so even a doubled cancel produces exactly one
cancellation, never an error or a double effect (see
test_cancel_is_idempotent_so_a_loop_cannot_double_apply and the end-to-end
test_reschedule_and_cancel_journey_end_to_end).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    Booking,
    BookingStatus,
    Customer,
    LocationType,
    Notification,
    NotificationKind,
    NotificationStatus,
    Slot,
    SlotStatus,
)
from app.services import booking, customers
from app.services import slots as slots_service

SESSION = "sess-mgmt"
OTHER_SESSION = "sess-other"
MOBILE = "+971501112233"

TD_REF = re.compile(r"^TD-\d{4}-[A-Z0-9]{4}$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _customer(db, mobile=MOBILE, **kw) -> int:
    return customers.find_or_create(db, mobile=mobile, **kw)["customer"]["customer_id"]


def _far_slots_same_vehicle(db, n: int) -> list[Slot]:
    """`n` earliest OPEN showroom slots for ONE vehicle, all > 25h out.

    > 25h so BOTH the T-24h and T-2h reminders are in the future (see
    _far_future_slot in test_booking.py) — otherwise schedule_for_booking
    legitimately skips a reminder and the count assertions wobble for a reason
    unrelated to reschedule/cancel. Same vehicle because a reschedule keeps the
    car (decision 4), so every candidate slot must be for it.
    """
    cutoff = datetime.now() + timedelta(hours=25)
    rows = db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.start > cutoff,
        )
        .order_by(Slot.start, Slot.id)
    ).all()
    by_vehicle: dict[int, list[Slot]] = {}
    for s in rows:
        by_vehicle.setdefault(s.vehicle_id, []).append(s)
    for slots in by_vehicle.values():
        if len(slots) >= n:
            return slots[:n]
    raise AssertionError(f"no vehicle has {n} far-future open showroom slots")


def _book(db, slot: Slot, cid: int, *, session=SESSION, lang=None) -> dict:
    """Lock + create through the real guards (never an INSERT)."""
    locked = slots_service.lock(db, session_id=session, slot_id=slot.id)
    assert locked["locked"] is True
    # Rule 9 precondition 3 (added later than these tests): create_booking refuses
    # without it. Part of setting up a booking, not the behaviour under test here
    # — the guard itself is covered by tests/test_licence_reminder.py.
    slots_service.acknowledge_licence_reminder(db, session_id=session)
    out = booking.create(
        db, session_id=session, customer_id=cid, vehicle_id=slot.vehicle_id, slot_id=slot.id, lang=lang,
    )
    assert out.get("created") is True, out
    return out["booking"]


def _scheduled(db, ref: str) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification).where(
                Notification.ref == ref, Notification.status == NotificationStatus.scheduled
            )
        ).all()
    )


def _scheduled_kinds(db, ref: str) -> set[str]:
    return {n.kind.value for n in _scheduled(db, ref)}


# --------------------------------------------------------------------------- #
# get_booking — the read front door (FR-RS-01, FR-CN-01)
# --------------------------------------------------------------------------- #
def test_get_booking_by_ref_only(db, slots):
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    out = booking.find(db, ref=ref)
    assert out["count"] == 1
    assert out["bookings"][0]["booking_ref"] == ref
    # Human-readable slot is present for the recap (LOC-09).
    assert out["bookings"][0]["start_human"]


def test_get_booking_by_ref_is_case_insensitive(db, slots):
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    assert booking.find(db, ref=ref.lower())["count"] == 1


def test_get_booking_by_mobile_only(db, slots):
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    out = booking.find(db, mobile=MOBILE)
    assert out["count"] == 1
    assert out["bookings"][0]["booking_ref"] == ref


def test_get_booking_no_match_is_honest_not_found(db, slots):
    # A valid, unknown number: an EMPTY list, never an invented booking (rule 2).
    out = booking.find(db, mobile="+971509999999")
    assert out == {"bookings": [], "count": 0}

    # A reference nobody holds: same honest empty.
    assert booking.find(db, ref="TD-2026-9999") == {"bookings": [], "count": 0}


def test_get_booking_requires_an_identifier(db, slots):
    assert booking.find(db)["error"] == "identifier_required"


def test_get_booking_is_active_only(db, slots):
    """active = status in (confirmed, rescheduled), NOT "future". A cancelled
    booking disappears from the lookup; a rescheduled one stays (it is active)."""
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)
    assert booking.find(db, ref=ref)["count"] == 0  # cancelled -> hidden
    assert booking.find(db, mobile=MOBILE)["count"] == 0


def test_get_booking_returns_all_when_customer_has_several(db, slots):
    cid = _customer(db)
    s1, s2 = _far_slots_same_vehicle(db, 2)
    r1 = _book(db, s1, cid)["booking_ref"]
    r2 = _book(db, s2, cid)["booking_ref"]

    out = booking.find(db, mobile=MOBILE)
    assert out["count"] == 2
    assert {b["booking_ref"] for b in out["bookings"]} == {r1, r2}


# --------------------------------------------------------------------------- #
# reschedule (FR-RS, rule 10) — S-09, S-11
# --------------------------------------------------------------------------- #
def test_reschedule_keeps_ref_moves_slot_and_swaps_reminders(db, slots):
    """S-09: after a reschedule the OLD reminders are gone and only the NEW
    slot's exist, and the TD reference is unchanged."""
    cid = _customer(db)
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    ref = _book(db, old_slot, cid)["booking_ref"]

    # A new booking scheduled a confirmation + t24 + t2.
    assert _scheduled_kinds(db, ref) == {"confirmation", "t24", "t2"}

    slots_service.lock(db, session_id=SESSION, slot_id=new_slot.id)
    out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, ref=ref)

    assert out["rescheduled"] is True
    moved = out["booking"]
    assert moved["booking_ref"] == ref  # SAME reference (S-11 basis)
    assert moved["slot_id"] == new_slot.id
    assert moved["status"] == "rescheduled"

    # Old slot back to inventory; new slot booked.
    assert db.get(Slot, old_slot.id).status == SlotStatus.open
    assert db.get(Slot, new_slot.id).status == SlotStatus.booked

    # Only the new slot's reminders remain scheduled; the primary notice is now a
    # `reschedule`, not a `confirmation`.
    assert _scheduled_kinds(db, ref) == {"reschedule", "t24", "t2"}
    # The originals were retired, not deleted (audit-preserving) — they are
    # cancelled, and there is exactly one scheduled row per kind.
    kinds = [n.kind.value for n in _scheduled(db, ref)]
    assert sorted(kinds) == ["reschedule", "t2", "t24"]
    cancelled = db.scalars(
        select(Notification).where(
            Notification.ref == ref, Notification.status == NotificationStatus.cancelled
        )
    ).all()
    assert {n.kind.value for n in cancelled} == {"confirmation", "t24", "t2"}


def test_repeated_reschedules_keep_one_ref_and_only_latest_reminders(db, slots):
    """S-11: move it three times. The reference never changes, no booking is
    duplicated, and only the final slot's reminders are live."""
    cid = _customer(db)
    s0, s1, s2, s3 = _far_slots_same_vehicle(db, 4)
    ref = _book(db, s0, cid)["booking_ref"]

    for target in (s1, s2, s3):
        slots_service.lock(db, session_id=SESSION, slot_id=target.id)
        out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=target.id, ref=ref)
        assert out["rescheduled"] is True
        assert out["booking"]["booking_ref"] == ref

    # Exactly one booking row for this customer — no context loss, no duplication.
    rows = db.scalars(select(Booking).where(Booking.customer_id == cid)).all()
    assert len(rows) == 1
    assert rows[0].slot_id == s3.id
    assert rows[0].status == BookingStatus.rescheduled

    # Only the last slot's reminders are scheduled; every earlier slot released.
    assert _scheduled_kinds(db, ref) == {"reschedule", "t24", "t2"}
    assert len(_scheduled(db, ref)) == 3
    for released in (s0, s1, s2):
        assert db.get(Slot, released.id).status == SlotStatus.open
    assert db.get(Slot, s3.id).status == SlotStatus.booked


def test_reschedule_is_idempotent_on_the_same_slot(db, slots):
    """A retry that targets the slot the booking already sits on returns the
    booking, does not move again, and does not re-issue reminders."""
    cid = _customer(db)
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    ref = _book(db, old_slot, cid)["booking_ref"]

    slots_service.lock(db, session_id=SESSION, slot_id=new_slot.id)
    booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, ref=ref)
    before = len(db.scalars(select(Notification).where(Notification.ref == ref)).all())

    # Retry onto the SAME (now current) slot.
    again = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, ref=ref)
    assert again["idempotent"] is True
    assert again["rescheduled"] is False
    after = len(db.scalars(select(Notification).where(Notification.ref == ref)).all())
    assert after == before  # no new notification rows


def test_reschedule_requires_the_new_slot_locked_by_this_session(db, slots):
    """The move reuses create's ownership guard: an unlocked (or another
    session's) new slot is refused as data, never applied."""
    cid = _customer(db)
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    ref = _book(db, old_slot, cid)["booking_ref"]

    # No lock on new_slot at all.
    out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, ref=ref)
    assert out["error"] == "slot_not_locked_by_session"
    assert db.get(Booking, db.scalar(select(Booking.id))).slot_id == old_slot.id  # unmoved

    # Locked by someone else — still refused.
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=new_slot.id)
    out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, ref=ref)
    assert out["error"] == "slot_not_locked_by_session"


def test_reschedule_refuses_a_different_vehicles_slot(db, slots):
    """A reschedule moves the time, never swaps the car (decision 4)."""
    cid = _customer(db)
    my_slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, my_slot, cid)["booking_ref"]

    other = db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.open,
            Slot.vehicle_id != my_slot.vehicle_id,
            Slot.start > datetime.now() + timedelta(hours=25),
        ).order_by(Slot.start)
    ).first()
    assert other is not None
    slots_service.lock(db, session_id=SESSION, slot_id=other.id)
    out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=other.id, ref=ref)
    assert out["error"] == "vehicle_slot_mismatch"


def test_reschedule_by_mobile_works(db, slots):
    cid = _customer(db)
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    ref = _book(db, old_slot, cid)["booking_ref"]

    slots_service.lock(db, session_id=SESSION, slot_id=new_slot.id)
    out = booking.reschedule(db, session_id=SESSION, session_customer_id=cid, new_slot_id=new_slot.id, mobile=MOBILE)
    assert out["rescheduled"] is True
    assert out["booking"]["booking_ref"] == ref


def test_reschedule_not_found_is_data(db, slots):
    cid = _customer(db)  # a verified session, but no such booking
    slot = _far_slots_same_vehicle(db, 1)[0]
    slots_service.lock(db, session_id=SESSION, slot_id=slot.id)
    out = booking.reschedule(
        db, session_id=SESSION, session_customer_id=cid, new_slot_id=slot.id, ref="TD-2026-9999"
    )
    assert out["error"] == "booking_not_found"


# --------------------------------------------------------------------------- #
# cancel (FR-CN, rule 11) — S-13
# --------------------------------------------------------------------------- #
def test_cancel_releases_slot_flags_winback_and_stops_reminders(db, slots):
    """S-13: offer declined -> cancel proceeds. Slot released, customer flagged
    for win-back, pending reminders retired, a written cancellation notice sent —
    and NO reason demanded (none is passed here)."""
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]
    assert _scheduled_kinds(db, ref) == {"confirmation", "t24", "t2"}

    out = booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)  # no reason_code
    assert out["cancelled"] is True
    assert out["winback_flag"] is True
    assert out["booking"]["status"] == "cancelled"

    # Slot back to open inventory instantly (FR-CN-04), so it is bookable again.
    assert db.get(Slot, slot.id).status == SlotStatus.open
    # Win-back flag set (FR-CN-05).
    assert db.get(Customer, cid).winback_flag is True
    # Reason stayed optional — never demanded, so it is None here.
    assert db.get(Booking, db.scalar(select(Booking.id))).reason_code is None

    # The confirmation/t24/t2 are retired; the ONLY scheduled row is the written
    # cancellation notice (FR-CN-03).
    assert _scheduled_kinds(db, ref) == {"cancellation"}


def test_cancel_records_reason_only_when_offered(db, slots):
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref, reason_code="bought_elsewhere")
    assert db.get(Booking, db.scalar(select(Booking.id))).reason_code == "bought_elsewhere"


def test_cancelled_slot_can_be_booked_by_someone_else(db, slots):
    """The partial unique index excludes cancelled, so a freed slot is genuinely
    available to the next customer (not blocked by the cancelled row)."""
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]
    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)

    other_cid = _customer(db, mobile="+971502223344")
    rebooked = _book(db, db.get(Slot, slot.id), other_cid, session=OTHER_SESSION)
    assert rebooked["booking_ref"] != ref
    assert rebooked["slot_id"] == slot.id


def test_cancel_is_idempotent_so_a_loop_cannot_double_apply(db, slots):
    """Rule 11's no-loop guarantee at the service layer: cancelling an
    already-cancelled booking returns current state (idempotent), never an error
    and never a second effect. So even if the agent looped and fired cancel
    twice, the outcome is exactly one cancellation."""
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    first = booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)
    assert first["cancelled"] is True

    second = booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)
    assert "error" not in second
    assert second["idempotent"] is True
    assert second["cancelled"] is False
    assert second["booking"]["status"] == "cancelled"

    # Still exactly one cancellation notice — the loop did not double-notify.
    notices = db.scalars(
        select(Notification).where(
            Notification.ref == ref, Notification.kind == NotificationKind.cancellation
        )
    ).all()
    assert len(notices) == 1


def test_cancel_by_mobile_and_multiple_bookings_disambiguates(db, slots):
    """With two live bookings and only a mobile, cancel refuses to guess — it
    returns the list so the agent asks which TD reference (decision 6)."""
    cid = _customer(db)
    s1, s2 = _far_slots_same_vehicle(db, 2)
    _book(db, s1, cid)
    _book(db, s2, cid)

    out = booking.cancel(db, session_id=SESSION, session_customer_id=cid, mobile=MOBILE)
    assert out["error"] == "multiple_bookings"
    assert len(out["bookings"]) == 2
    # Nothing was cancelled.
    assert db.scalars(
        select(Booking).where(Booking.status == BookingStatus.cancelled)
    ).all() == []


def test_cancel_not_found_is_data(db, slots):
    cid = _customer(db)  # verified, but holds no such booking
    out = booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref="TD-2026-9999")
    assert out["error"] == "booking_not_found"


# --------------------------------------------------------------------------- #
# Identity guard (developer decision 2026-07-23, D1 PDPL) — a booking can only be
# viewed or changed by the customer it belongs to, even from a valid TD ref.
# --------------------------------------------------------------------------- #
def test_reschedule_without_verified_identity_is_refused(db, slots):
    """No session_customer_id -> the move is refused as data, never applied. This
    is the server-side guard: even a confused caller cannot reschedule on a bare
    reference."""
    cid = _customer(db)
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    ref = _book(db, old_slot, cid)["booking_ref"]

    slots_service.lock(db, session_id=SESSION, slot_id=new_slot.id)
    out = booking.reschedule(db, session_id=SESSION, new_slot_id=new_slot.id, ref=ref)  # no identity
    assert out["error"] == "identity_not_verified"
    assert db.get(Booking, db.scalar(select(Booking.id))).slot_id == old_slot.id  # unmoved


def test_cancel_without_verified_identity_is_refused(db, slots):
    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    out = booking.cancel(db, session_id=SESSION, ref=ref)  # no identity
    assert out["error"] == "identity_not_verified"
    assert db.get(Booking, db.scalar(select(Booking.id))).status == BookingStatus.confirmed


def test_reschedule_a_strangers_booking_is_not_found(db, slots):
    """A session verified as customer A cannot move customer B's booking, even
    with B's exact TD ref — the owner-scoped lookup makes it simply not found, an
    honest answer that never confirms B's booking exists."""
    a = _customer(db)
    b = _customer(db, mobile="+971503334455")
    old_slot, new_slot = _far_slots_same_vehicle(db, 2)
    b_ref = _book(db, old_slot, b, session=OTHER_SESSION)["booking_ref"]

    slots_service.lock(db, session_id=SESSION, slot_id=new_slot.id)
    out = booking.reschedule(
        db, session_id=SESSION, session_customer_id=a, new_slot_id=new_slot.id, ref=b_ref
    )
    assert out["error"] == "booking_not_found"
    # B's booking is untouched.
    assert db.get(Slot, old_slot.id).status == SlotStatus.booked


def test_cancel_a_strangers_booking_is_not_found(db, slots):
    a = _customer(db)
    b = _customer(db, mobile="+971503334455")
    slot = _far_slots_same_vehicle(db, 1)[0]
    b_ref = _book(db, slot, b, session=OTHER_SESSION)["booking_ref"]

    out = booking.cancel(db, session_id=SESSION, session_customer_id=a, ref=b_ref)
    assert out["error"] == "booking_not_found"
    # B's booking is still confirmed, its slot still booked.
    assert db.get(Slot, slot.id).status == SlotStatus.booked


def test_find_owner_scope_hides_another_customers_booking(db, slots):
    """booking.find owner-scoped returns only the owner's bookings; a stranger's
    ref comes back empty, while the trusted (unscoped) call still sees it."""
    a = _customer(db)
    b = _customer(db, mobile="+971503334455")
    slot = _far_slots_same_vehicle(db, 1)[0]
    b_ref = _book(db, slot, b, session=OTHER_SESSION)["booking_ref"]

    # Owner-scoped to A: not theirs -> empty.
    assert booking.find(db, ref=b_ref, owner_customer_id=a)["count"] == 0
    # Owner-scoped to B: their own -> found.
    assert booking.find(db, ref=b_ref, owner_customer_id=b)["count"] == 1
    # Unscoped (trusted caller, e.g. the reload card): still found.
    assert booking.find(db, ref=b_ref)["count"] == 1


def test_get_booking_tool_requires_an_identified_session(db, slots):
    """The read TOOL refuses an unidentified session (viewer None) with
    identity_required, rather than returning a booking off a bare ref."""
    from app.tools.booking_tools import get_booking as get_booking_tool

    cid = _customer(db)
    slot = _far_slots_same_vehicle(db, 1)[0]
    ref = _book(db, slot, cid)["booking_ref"]

    # Unidentified viewer -> refused.
    out = get_booking_tool(db, SESSION, ref=ref, viewer_customer_id=None)
    assert out["error"] == "identity_required"

    # Identified as the owner -> the booking, scoped to them.
    ok = get_booking_tool(db, SESSION, ref=ref, viewer_customer_id=cid)
    assert ok["count"] == 1
    assert ok["bookings"][0]["booking_ref"] == ref


def test_reschedule_reads_slot_id_as_an_alias_for_new_slot_id(db):
    """Found live, 2026-08-31. Every other booking action calls its slot
    `slot_id`, and the sub-agent proposed that name here too — twice, refused
    twice for a spelling. The alias is safe: the service still books only a
    slot locked by this session, so a wrong id is refused loudly, never used."""
    from app.tools.booking_tools import reschedule_booking

    out = reschedule_booking(
        db, "sess-alias", customer_id=1, ref="TD-2026-XXXX", slot_id=999999
    )
    # The alias took: we got PAST invalid_slot_id to the real lookup guards.
    assert out.get("error") != "invalid_slot_id"
