"""Booking write-guard tests (FR-BK-05/08/11, FR-CF-05, FR-SL-06/07, LOC-06).

These are the tests that matter most: each one corresponds to a way a live demo
could produce a wrong booking in front of a client.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AuditEvent,
    AuditType,
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
from app.services import booking, consent, customers

# Imported under an alias: the conftest fixture is also called `slots`, and a
# test taking that fixture would otherwise shadow the module.
from app.services import slots as slots_service

SESSION = "sess-aaa"
OTHER_SESSION = "sess-bbb"

TD_REF = re.compile(r"^TD-\d{4}-[A-Z0-9]{4}$")


def _customer(db, mobile="+971501234567", **kw):
    return customers.find_or_create(db, mobile=mobile, **kw)["customer"]["customer_id"]


def _book(db, slot, cid, *, session=SESSION, **kw):
    # Rule 9 precondition 3 (added later than these tests): create_booking refuses
    # without the licence acknowledgement. It is setup here, not the behaviour
    # under test — the guard is covered by tests/test_licence_reminder.py. A
    # no-op when this session holds no lock, so the lock-guard tests below still
    # see the error they expect (create checks lock ownership FIRST).
    slots_service.acknowledge_licence_reminder(db, session_id=session)
    return booking.create(
        db, session_id=session, customer_id=cid, vehicle_id=slot.vehicle_id, slot_id=slot.id, **kw
    )


def _open_slots(db, n: int) -> list[Slot]:
    """The n earliest open showroom slots."""
    rows = db.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
    ).all()
    return list(rows)[:n]


def _far_future_slot(db) -> Slot:
    """The earliest open slot more than 24h away, so BOTH reminders are future.

    The generic open_slot fixture starts tomorrow, which can be under 24h out
    depending on the hour the suite runs — and a T-24h reminder would then be
    legitimately skipped, making a reminder assertion fail for a reason that has
    nothing to do with what it is testing.
    """
    cutoff = datetime.now() + timedelta(hours=25)
    return db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.start > cutoff,
        )
        .order_by(Slot.start, Slot.id)
    ).first()


# --------------------------------------------------------------------------- #
# GUARD 1 — the slot must be locked by THIS session
# --------------------------------------------------------------------------- #
def test_lock_then_book_succeeds(db, open_slot):
    cid = _customer(db)
    assert slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)["locked"] is True
    out = _book(db, open_slot, cid)
    assert out["created"] is True
    assert TD_REF.match(out["booking"]["booking_ref"])


def test_booking_without_a_lock_is_rejected(db, open_slot):
    """The prompt says "lock, then recap, then book". The service is what makes
    that true — a model that skipped straight to booking must not succeed.
    """
    cid = _customer(db)
    out = _book(db, open_slot, cid)
    assert out["error"] == "slot_not_locked_by_session"
    assert db.scalar(select(Booking)) is None


def test_booking_on_another_sessions_lock_is_rejected(db, open_slot):
    """Two conversations, one slot. Without locked_by_session this check would
    degrade to "is it locked at all" and both would book it.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid, session=SESSION)
    assert out["error"] == "slot_not_locked_by_session"
    assert db.scalar(select(Booking)) is None


def test_expired_lock_cannot_be_booked(db, open_slot):
    """The hold lapsed mid-recap. Booking anyway would hand the customer a slot
    another conversation may already have taken.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    later = datetime.now() + timedelta(days=1)
    out = booking.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=open_slot.vehicle_id,
        slot_id=open_slot.id,
        now=later,
    )
    assert out["error"] == "slot_not_locked_by_session"


# --------------------------------------------------------------------------- #
# GUARD 2 — idempotency. LLMs retry.
# --------------------------------------------------------------------------- #
def test_double_create_booking_returns_the_same_ref(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    first = _book(db, open_slot, cid)
    second = _book(db, open_slot, cid)

    assert second["created"] is False
    assert second["idempotent"] is True
    assert second["booking"]["booking_ref"] == first["booking"]["booking_ref"]
    assert db.scalar(select(Booking.ref)) == first["booking"]["booking_ref"]
    assert len(list(db.scalars(select(Booking)).all())) == 1


def test_retry_is_answered_even_though_the_lock_is_gone(db, open_slot):
    """Booking consumes the lock, so on a retry the guard-1 check would say
    "not locked by this session" — an error for a booking that SUCCEEDED. This
    is why idempotency is answered before ownership is asked about.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    _book(db, open_slot, cid)
    db.refresh(open_slot)
    assert open_slot.locked_by_session is None  # the lock really is gone

    second = _book(db, open_slot, cid)
    assert "error" not in second
    assert second["idempotent"] is True


def test_another_customers_booking_is_never_returned(db, open_slot):
    """The data-leak branch. If the active booking on this slot belongs to
    someone else, the answer is "unavailable" — returning their booking would
    hand this customer another customer's name and appointment.
    """
    ravi = _customer(db, "+971501234567", name="Ravi")
    sara = _customer(db, "+971521234567", name="Sara")

    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=open_slot.id)
    first = _book(db, open_slot, ravi, session=OTHER_SESSION)
    assert first["created"] is True

    out = _book(db, open_slot, sara, session=SESSION)
    assert out["error"] == "slot_unavailable"
    assert "booking" not in out
    assert "Ravi" not in str(out)


# --------------------------------------------------------------------------- #
# TD-YYYY-NNNN (FR-BK-08)
# --------------------------------------------------------------------------- #
def test_ref_format(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    ref = _book(db, open_slot, cid)["booking"]["booking_ref"]
    assert TD_REF.match(ref)
    assert ref.startswith(f"TD-{datetime.now().year}-")


def test_refs_are_random_and_unique(db, slots):
    """The suffix is a random base32 token (2026-07-24), not MAX+1, so refs carry
    NO ordering signal — TD-2026-0003 no longer implies -0002 exists. Uniqueness
    is still the UNIQUE constraint's job, not the arithmetic's; here we only pin
    that distinct bookings get distinct refs of the right shape.
    """
    cid = _customer(db)
    a, b = _open_slots(db, 2)

    slots_service.lock(db, session_id=SESSION, slot_id=a.id)
    first = _book(db, a, cid)["booking"]["booking_ref"]
    slots_service.lock(db, session_id=SESSION, slot_id=b.id)
    second = _book(db, b, cid)["booking"]["booking_ref"]

    assert first != second
    assert TD_REF.match(first) and TD_REF.match(second)


def test_ref_suffix_avoids_ambiguous_glyphs(db, slots):
    """The alphabet excludes I, L, O and U so a ref read aloud or copied off a
    screen at a live demo has no 0/O or 1/I/L confusion. Draw a batch and assert
    none of the banned glyphs ever appears in a suffix.
    """
    cid = _customer(db)
    banned = set("ILOU")
    for slot in _open_slots(db, 12):
        slots_service.lock(db, session_id=SESSION, slot_id=slot.id)
        ref = _book(db, slot, cid)["booking"]["booking_ref"]
        assert TD_REF.match(ref)
        assert not (set(ref[-4:]) & banned), f"{ref} contains a banned glyph"


# --------------------------------------------------------------------------- #
# The partial unique index — no double-booking, but cancel-then-rebook works
# --------------------------------------------------------------------------- #
def test_two_active_bookings_on_one_slot_are_impossible_at_the_db_level(db, open_slot):
    """The guarantee that matters: two customers arriving for the same car at
    the same time. This bypasses the service entirely — the DB itself must
    refuse, because a service check alone loses the race it is trying to win.
    """
    from sqlalchemy.exc import IntegrityError

    ravi = _customer(db, "+971501234567")
    sara = _customer(db, "+971521234567")
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    _book(db, open_slot, ravi)

    db.add(
        Booking(
            ref="TD-2026-9999",
            customer_id=sara,
            vehicle_id=open_slot.vehicle_id,
            slot_id=open_slot.id,
            location=open_slot.location_type,
            status=BookingStatus.confirmed,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_cancelled_booking_does_not_block_rebooking_the_slot(db, open_slot):
    """Why the index MUST be partial. Cancel releases the slot to inventory but
    leaves the cancelled row still pointing at slot_id. A plain unique
    constraint would reject the next customer to book a slot that is genuinely
    free — a bug that would not surface until the cancel journey exists.
    """
    ravi = _customer(db, "+971501234567")
    sara = _customer(db, "+971521234567")

    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    first = _book(db, open_slot, ravi)
    assert first["created"] is True

    # Cancel, the way the cancel service will: status flips, row stays, slot
    # returns to inventory.
    stored = db.scalar(select(Booking).where(Booking.ref == first["booking"]["booking_ref"]))
    stored.status = BookingStatus.cancelled
    open_slot.status = SlotStatus.open
    open_slot.locked_by_session = None
    db.flush()

    slots_service.lock(db, session_id=OTHER_SESSION, slot_id=open_slot.id)
    second = _book(db, open_slot, sara, session=OTHER_SESSION)
    assert second["created"] is True, "a genuinely free slot must be bookable"
    assert second["booking"]["booking_ref"] != first["booking"]["booking_ref"]


def test_the_partial_index_exists_in_the_schema(db):
    """Pin the mechanism, not just the behaviour: if the predicate is ever
    dropped the index silently becomes a plain unique constraint and the test
    above starts failing for a mysterious reason.
    """
    from sqlalchemy import inspect

    indexes = inspect(db.get_bind()).get_indexes("bookings")
    active = next(i for i in indexes if i["name"] == "uq_bookings_active_slot")
    assert active["unique"]  # SQLite reports 1, Postgres True — do not use `is`
    assert active["column_names"] == ["slot_id"]
    # The predicate is what makes it partial. SQLAlchemy surfaces it under a
    # dialect-specific key, so accept either rather than pinning one backend.
    # Reflected as a TextClause, whose truthiness raises — compare to None, and
    # never chain these with `or`.
    opts = active.get("dialect_options", {})
    predicate = opts.get("sqlite_where")
    if predicate is None:
        predicate = opts.get("postgresql_where")
    assert predicate is not None, "the index must be PARTIAL, not a plain unique constraint"
    assert "cancelled" not in str(predicate)


# --------------------------------------------------------------------------- #
# Doorstep (FR-SL-07) + PDPL data minimisation
# --------------------------------------------------------------------------- #
def test_doorstep_without_an_address_is_rejected(db, doorstep_slot):
    """We cannot bring a car to an address we do not have."""
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=doorstep_slot.id)
    out = _book(db, doorstep_slot, cid)
    assert out["error"] == "doorstep_address_required"
    assert db.scalar(select(Booking)) is None


def test_doorstep_with_an_address_succeeds(db, doorstep_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=doorstep_slot.id)
    out = _book(db, doorstep_slot, cid, doorstep_address="Villa 12, Jumeirah 1, Dubai")
    assert out["created"] is True
    assert out["booking"]["doorstep_address"] == "Villa 12, Jumeirah 1, Dubai"


def test_showroom_booking_stores_no_address(db, open_slot):
    """Data minimisation: they are coming to us, so we have no purpose for
    their address — even if the model helpfully volunteered one.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid, doorstep_address="Villa 12, Jumeirah 1, Dubai")
    assert out["created"] is True

    stored = db.scalar(select(Booking).where(Booking.ref == out["booking"]["booking_ref"]))
    assert stored.doorstep_address is None
    assert "doorstep_address" not in out["booking"]


def test_doorstep_address_consent_is_audited(db, doorstep_slot):
    """FR-SL-07: doorstep requires explicit consent, logged to audit. It is
    per-booking (this address, this visit), so it is an audit event rather than
    a standing customer-level flag.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=doorstep_slot.id)
    out = _book(db, doorstep_slot, cid, doorstep_address="Villa 12, Jumeirah 1, Dubai")

    events = list(db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.consent)).all())
    axes = [e.payload["axis"] for e in events]
    assert "doorstep_address" in axes
    consent_event = next(e for e in events if e.payload["axis"] == "doorstep_address")
    assert consent_event.payload["ref"] == out["booking"]["booking_ref"]
    assert consent_event.session == SESSION


def test_showroom_booking_logs_no_address_consent(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    _book(db, open_slot, cid)
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.consent)).all())
    assert [e for e in events if e.payload.get("axis") == "doorstep_address"] == []


# --------------------------------------------------------------------------- #
# CONSENT DOES NOT GATE THE BOOKING (the BRD exception path)
# --------------------------------------------------------------------------- #
def test_booking_succeeds_when_messaging_consent_is_false(db, open_slot):
    """A customer who declines WhatsApp still gets their test drive — we simply
    do not message them. Gating the booking on consent would turn a privacy
    choice into a denial of service.
    """
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=False)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)

    out = _book(db, open_slot, cid)
    assert out["created"] is True
    assert TD_REF.match(out["booking"]["booking_ref"])


def test_booking_succeeds_with_no_consent_recorded_at_all(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    assert _book(db, open_slot, cid)["created"] is True


# --------------------------------------------------------------------------- #
# The slot is the appointment
# --------------------------------------------------------------------------- #
def test_vehicle_must_match_the_slot(db, open_slot):
    """A model that mixed up two ids could otherwise book the G700's slot and
    tell the customer they are driving a T2 — discovered on arrival.
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)  # rule 9 precondition 3
    wrong = open_slot.vehicle_id + 1
    out = booking.create(
        db, session_id=SESSION, customer_id=cid, vehicle_id=wrong, slot_id=open_slot.id
    )
    assert out["error"] == "vehicle_slot_mismatch"


def test_booking_marks_the_slot_booked_and_clears_the_lock(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    _book(db, open_slot, cid)
    db.refresh(open_slot)
    assert open_slot.status == SlotStatus.booked
    assert open_slot.locked_by_session is None
    assert open_slot.locked_at is None


def test_two_test_drives_in_one_conversation_still_work(db, slots):
    """The one-live-lock-per-session invariant is a bounded constraint, not a
    lost feature: booking consumes the lock, so the next lock has nothing to
    auto-release.
    """
    cid = _customer(db)
    a, b = _open_slots(db, 2)

    slots_service.lock(db, session_id=SESSION, slot_id=a.id)
    first = _book(db, a, cid)
    slots_service.lock(db, session_id=SESSION, slot_id=b.id)
    second = _book(db, b, cid)

    assert first["created"] is True and second["created"] is True
    assert first["booking"]["booking_ref"] != second["booking"]["booking_ref"]


# --------------------------------------------------------------------------- #
# Errors as data — never an exception mid-turn
# --------------------------------------------------------------------------- #
def test_unknown_customer_is_data(db, open_slot):
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = booking.create(
        db, session_id=SESSION, customer_id=9999, vehicle_id=open_slot.vehicle_id, slot_id=open_slot.id
    )
    assert out["error"] == "customer_not_found"


def test_unknown_slot_is_data(db, slots):
    cid = _customer(db)
    out = booking.create(db, session_id=SESSION, customer_id=cid, vehicle_id=1, slot_id=99999)
    assert out["error"] == "slot_not_found"


def test_garbage_identifiers_are_data(db, slots):
    out = booking.create(db, session_id=SESSION, customer_id="abc", vehicle_id=None, slot_id="x")
    assert out["error"] == "invalid_identifiers"


# --------------------------------------------------------------------------- #
# Personalisation fields (FR-BK-05/11, LOC-06)
# --------------------------------------------------------------------------- #
def test_booking_for_a_family_member_records_the_driver(db, open_slot):
    """"Book it for my husband" — he needs his own licence on the day."""
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid, driver_name="Ahmed", driver_relationship="husband")
    assert out["booking"]["driver_name"] == "Ahmed"
    assert out["booking"]["driver_relationship"] == "husband"


def test_personalisation_fields_are_stored(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid, companions="wife and two kids", child_seat=True,
                consultant_pref="female")
    assert out["booking"]["companions"] == "wife and two kids"
    assert out["booking"]["child_seat"] is True
    assert out["booking"]["consultant_pref"] == "female"


def test_personalisation_defaults_are_empty_not_guessed(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid)
    assert out["booking"]["companions"] is None
    assert out["booking"]["child_seat"] is False
    assert out["booking"]["consultant_pref"] is None


# --------------------------------------------------------------------------- #
# GUARD 3 — audit at the SERVICE layer (FR-AD-02)
# --------------------------------------------------------------------------- #
def test_booking_is_audited(db, open_slot):
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=open_slot.id)
    out = _book(db, open_slot, cid)

    events = list(
        db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.intent)).all()
    )
    assert len(events) == 1
    assert events[0].payload["intent"] == "create_booking"
    assert events[0].payload["ref"] == out["booking"]["booking_ref"]
    assert events[0].session == SESSION


# --------------------------------------------------------------------------- #
# Notifications — a side effect of the booking, never a tool (FR-CF)
# --------------------------------------------------------------------------- #
def test_booking_schedules_confirmation_and_both_reminders(db, slots):
    """The demo books days out, so T-24h and T-2h are both in the future."""
    cid = _customer(db)
    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    out = _book(db, far, cid)

    rows = list(db.scalars(select(Notification).where(Notification.ref == out["booking"]["booking_ref"])).all())
    assert {r.kind for r in rows} == {
        NotificationKind.confirmation,
        NotificationKind.t24,
        NotificationKind.t2,
    }
    assert all(r.status == NotificationStatus.scheduled for r in rows)


def test_reminder_send_at_is_24h_and_2h_before_the_slot(db, slots):
    cid = _customer(db)
    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    ref = _book(db, far, cid)["booking"]["booking_ref"]

    by_kind = {
        r.kind: r
        for r in db.scalars(select(Notification).where(Notification.ref == ref)).all()
    }
    assert by_kind[NotificationKind.t24].send_at == far.start - timedelta(hours=24)
    assert by_kind[NotificationKind.t2].send_at == far.start - timedelta(hours=2)


def test_overdue_reminders_are_skipped_and_reported(db, slots):
    """Booking a slot 90 minutes out must not write a T-2h reminder whose
    send_at is already past — the scheduler would fire it instantly, reminding
    someone about a drive they are already driving to.
    """
    cid = _customer(db)
    a = _open_slots(db, 1)[0]
    just_before = a.start - timedelta(minutes=90)

    slots_service.lock(db, session_id=SESSION, slot_id=a.id, now=just_before)
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)  # rule 9 precondition 3
    out = booking.create(
        db, session_id=SESSION, customer_id=cid, vehicle_id=a.vehicle_id, slot_id=a.id,
        now=just_before,
    )

    kinds = {n["kind"] for n in out["notifications"]["scheduled"]}
    skipped = {n["kind"] for n in out["notifications"]["skipped"]}
    assert kinds == {"confirmation"}
    assert skipped == {"t24", "t2"}
    assert all(n["reason"] == "send_at_in_the_past" for n in out["notifications"]["skipped"])


def test_notification_body_carries_no_english_copy(db, slots):
    """Customer-facing copy must not live in the data layer: it is display text
    in a data field, and English-only copy breaks an Arabic conversation
    (LOC-01). Templates are a later phase; the facts go in payload.
    """
    cid = _customer(db)
    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    ref = _book(db, far, cid)["booking"]["booking_ref"]

    for r in db.scalars(select(Notification).where(Notification.ref == ref)).all():
        assert r.body == ""
        assert r.payload["booking_ref"] == ref
        assert r.payload["vehicle_name"]
        assert r.payload["slot_start_iso"]


def test_notification_payload_holds_no_address_copy(db, doorstep_slot):
    """The address already lives on the booking. A second copy is a second
    place to erase on "forget me", for no gain (PDPL data minimisation).
    """
    cid = _customer(db)
    slots_service.lock(db, session_id=SESSION, slot_id=doorstep_slot.id)
    out = _book(db, doorstep_slot, cid, doorstep_address="Villa 12, Jumeirah 1, Dubai")

    for r in db.scalars(select(Notification).where(Notification.ref == out["booking"]["booking_ref"])).all():
        assert "Villa 12" not in str(r.payload)


def test_notifications_are_audited(db, slots):
    cid = _customer(db)
    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    _book(db, far, cid)

    events = list(
        db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.notification)).all()
    )
    assert {e.payload["kind"] for e in events} == {"confirmation", "t24", "t2"}


def test_notification_lang_defaults_to_the_stored_preference(db, slots):
    """An approximation, deliberately overridable: FR-CF-01 says the
    confirmation goes out in the CONVERSATION's language, which the agent loop
    will pass through the lang parameter.
    """
    from app.models import Language

    cid = _customer(db)
    db.get(Customer, cid).language_pref = Language.ar
    db.flush()

    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    ref = _book(db, far, cid)["booking"]["booking_ref"]

    rows = list(db.scalars(select(Notification).where(Notification.ref == ref)).all())
    assert all(r.lang == Language.ar for r in rows)


def test_notification_lang_can_be_overridden_by_the_conversation(db, slots):
    from app.models import Language

    cid = _customer(db)
    far = _far_future_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=far.id)
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)  # rule 9 precondition 3
    out = booking.create(
        db, session_id=SESSION, customer_id=cid, vehicle_id=far.vehicle_id,
        slot_id=far.id, lang="ar",
    )
    rows = list(
        db.scalars(select(Notification).where(Notification.ref == out["booking"]["booking_ref"])).all()
    )
    assert all(r.lang == Language.ar for r in rows)


def test_a_failed_booking_schedules_nothing(db, open_slot):
    """No lock, no booking — and therefore no confirmation for a booking that
    does not exist.
    """
    cid = _customer(db)
    assert _book(db, open_slot, cid)["error"] == "slot_not_locked_by_session"
    assert db.scalar(select(Notification)) is None
