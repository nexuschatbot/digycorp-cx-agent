"""Notification dispatcher (AD-F) — the WhatsApp send path.

DB-ONLY: WHATSAPP_ENABLED is never set here, so services/whatsapp.send_text
returns `skipped` and the dispatcher runs in SIMULATOR mode. No network, no
vendor call — the same discipline as the rest of the suite.

Covers the two things the dispatcher owns that the schedule step does not:
  1. the consent gate at SEND time (no consent → the row is retired, not sent);
  2. rendering + the `scheduled → sent` transition, with the body persisted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    LocationType,
    Notification,
    NotificationKind,
    NotificationStatus,
    Slot,
    SlotStatus,
)
from app.services import booking, consent, customers, dispatcher
from app.services import slots as slots_service

SESSION = "sess-dispatch"


def _customer(db, *, consent_msg: bool):
    cid = customers.find_or_create(db, mobile="+971501234567", name="Ravi")["customer"]["customer_id"]
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=consent_msg)
    return cid


def _far_future_slot(db):
    """Earliest open slot >24h out, so both reminders are future rows."""
    now = datetime.now()
    return db.scalars(
        select(Slot)
        # SHOWROOM ONLY. Without this the helper could land on a doorstep slot,
        # which booking.create rightly refuses without an address — turning every
        # test in this file red depending on the time of day the suite ran.
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.start > now + timedelta(hours=25),
        )
        .order_by(Slot.start)
    ).first()


def _make_booking(db, cid, slot):
    slots_service.lock(db, session_id=SESSION, slot_id=slot.id)
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)
    out = booking.create(
        db, session_id=SESSION, customer_id=cid,
        vehicle_id=slot.vehicle_id, slot_id=slot.id, lang="en",
    )
    assert out["created"] is True
    db.commit()
    return out["booking"]["booking_ref"]


def _rows(db, ref):
    return list(
        db.scalars(select(Notification).where(Notification.ref == ref)).all()
    )


def test_confirmation_sent_when_consented(db, slots):
    """A consented customer's confirmation (send_at = now) goes `sent` on dispatch,
    with the rendered body persisted — simulator mode, no network."""
    cid = _customer(db, consent_msg=True)
    slot = _far_future_slot(db)
    ref = _make_booking(db, cid, slot)

    result = dispatcher.dispatch_due(db, now=datetime.now())
    assert result["sent"] >= 1

    confirmation = next(r for r in _rows(db, ref) if r.kind == NotificationKind.confirmation)
    assert confirmation.status == NotificationStatus.sent
    assert "Aisha" in confirmation.body
    assert confirmation.payload.get("simulated") is True  # WhatsApp disabled in tests


def test_no_consent_retires_the_row(db, slots):
    """No messaging consent → the row is `cancelled` at send time, never `sent`."""
    cid = _customer(db, consent_msg=False)
    slot = _far_future_slot(db)
    ref = _make_booking(db, cid, slot)

    result = dispatcher.dispatch_due(db, now=datetime.now())
    assert result["sent"] == 0
    assert result["skipped"] >= 1

    confirmation = next(r for r in _rows(db, ref) if r.kind == NotificationKind.confirmation)
    assert confirmation.status == NotificationStatus.cancelled
    assert confirmation.payload.get("not_sent_reason") == "no_consent"


def test_t2_reminder_not_due_until_two_hours_before(db, slots):
    """The T-2h row must not fire early: at booking time it is still `scheduled`,
    and only becomes due when now >= slot.start - 2h."""
    cid = _customer(db, consent_msg=True)
    slot = _far_future_slot(db)
    ref = _make_booking(db, cid, slot)

    # Dispatch "now" — the confirmation is due, the T-2h reminder is not.
    dispatcher.dispatch_due(db, now=datetime.now())
    t2 = next(r for r in _rows(db, ref) if r.kind == NotificationKind.t2)
    assert t2.status == NotificationStatus.scheduled

    # Advance the clock to 2h before the slot — now it sends.
    dispatcher.dispatch_due(db, now=slot.start - timedelta(hours=2))
    t2 = next(r for r in _rows(db, ref) if r.kind == NotificationKind.t2)
    assert t2.status == NotificationStatus.sent
    assert "2 hours" in t2.body


def test_t24_is_not_sent_by_default(db, slots):
    """The day-before reminder is scheduled (FR-CF) but excluded from sending by
    default (product decision 2026-07-22): once due it is retired, never sent."""
    cid = _customer(db, consent_msg=True)
    slot = _far_future_slot(db)
    ref = _make_booking(db, cid, slot)

    # Advance past the t24 send time (start - 24h); it must not go `sent`.
    dispatcher.dispatch_due(db, now=slot.start - timedelta(hours=23))
    t24 = next(r for r in _rows(db, ref) if r.kind == NotificationKind.t24)
    assert t24.status == NotificationStatus.cancelled
    assert t24.payload.get("not_sent_reason") == "kind_not_enabled"
