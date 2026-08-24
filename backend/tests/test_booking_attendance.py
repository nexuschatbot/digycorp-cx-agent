"""Attendance — did the customer turn up? (2026-07-23, admin console)

The second axis on a booking: `status` says whether the booking is alive,
`attendance` says whether the drive happened. Set by a human in the console and
by nobody else — there is deliberately no tool, so nothing here goes through the
agent.

What is pinned:

  * pending is the default, and a cancelled booking can never leave it
  * exactly one WhatsApp notice per booking, on the way INTO noshow only
  * a customer who never consented to WhatsApp is not messaged either
  * every change is audited as actor=admin, never system or agent

DB-only, like the rest of the suite: WhatsApp is off in conftest, so the
dispatcher short-circuits to `skipped` and no vendor is called.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    Actor,
    AuditEvent,
    Booking,
    BookingAttendance,
    BookingStatus,
    Language,
    Notification,
    NotificationKind,
    NotificationStatus,
    Slot,
    SlotStatus,
)
from app.services import booking, consent, customers
from app.services import notifications as notif_service
from app.services import slots as slots_service

SESSION = "sess-attend"
MOBILE = "+971501112244"


# --------------------------------------------------------------------------- #
# Helpers — a real booking through the real guards, never an INSERT
# --------------------------------------------------------------------------- #
def _customer(db, mobile=MOBILE, **kw) -> int:
    return customers.find_or_create(db, mobile=mobile, **kw)["customer"]["customer_id"]


def _far_slot(db) -> Slot:
    cutoff = datetime.now() + timedelta(hours=25)
    return db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == "showroom",
            Slot.start > cutoff,
        )
        .order_by(Slot.start, Slot.id)
    ).first()


def _book(db, cid: int, *, lang=None) -> dict:
    slot = _far_slot(db)
    assert slots_service.lock(db, session_id=SESSION, slot_id=slot.id)["locked"] is True
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)
    out = booking.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
        lang=lang,
    )
    assert out.get("created") is True, out
    return out["booking"]


def _noshow_rows(db, ref: str) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification).where(
                Notification.ref == ref, Notification.kind == NotificationKind.noshow
            )
        ).all()
    )


def _attendance_audit(db) -> list[AuditEvent]:
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    return [r for r in rows if (r.payload or {}).get("intent") == "set_attendance"]


# --------------------------------------------------------------------------- #
# The default
# --------------------------------------------------------------------------- #
def test_a_new_booking_starts_pending(db, slots):
    b = _book(db, _customer(db))
    assert b["attendance"] == BookingAttendance.pending.value

    row = db.scalar(select(Booking).where(Booking.ref == b["booking_ref"]))
    assert row.attendance == BookingAttendance.pending
    assert row.noshow_notified_at is None


def test_attendance_is_an_enum_value_not_a_label(db, slots):
    """LOC-01: "No show" / "لم يحضر" is display text and is built per language
    at render time, never stored."""
    b = _book(db, _customer(db))
    out = booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="noshow"
    )
    value = out["booking"]["attendance"]
    assert value in {a.value for a in BookingAttendance}
    assert value.islower() and " " not in value


# --------------------------------------------------------------------------- #
# The two axes are independent
# --------------------------------------------------------------------------- #
def test_completed_leaves_the_booking_confirmed(db, slots):
    """Attendance must not disturb `status` — that column drives the partial
    unique index and every active-booking lookup. A completed drive is still a
    confirmed booking.
    """
    b = _book(db, _customer(db))
    booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="completed"
    )

    row = db.scalar(select(Booking).where(Booking.ref == b["booking_ref"]))
    assert row.attendance == BookingAttendance.completed
    assert row.status == BookingStatus.confirmed

    # Still reachable through the ordinary active-only front door.
    assert booking.find(db, ref=b["booking_ref"])["count"] == 1


def test_completed_opens_a_feedback_survey(db, slots):
    """REVERSED 2026-07-26. This test previously asserted that `completed` was
    "recorded, and nothing else" (product decision 2026-07-23, when the meaning of
    `completed` was deliberately left open). It now has one: it opens the
    post-drive feedback survey, whose opening rides the ordinary notification
    rails so it inherits the consent gate and the audit trail.

    Kept as a test of the ATTENDANCE contract rather than moved wholesale into
    test_feedback_survey.py, because "what does marking completed do?" is a
    question about this module — and the previous answer was pinned here, so the
    change belongs here too. The survey's own behaviour is covered in full by
    tests/test_feedback_survey.py.
    """
    b = _book(db, _customer(db))
    before = len(db.scalars(select(Notification)).all())
    out = booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="completed"
    )

    assert out["feedback"]["opened"] is True
    rows = db.scalars(select(Notification)).all()
    assert len(rows) == before + 1
    assert rows[-1].kind == NotificationKind.feedback

    # Attendance itself is unchanged by any of this: `status` still says the
    # booking is alive, `attendance` still says the drive happened, and neither
    # derives from the other.
    row = db.scalar(select(Booking).where(Booking.ref == b["booking_ref"]))
    assert row.attendance == BookingAttendance.completed
    assert row.status == BookingStatus.confirmed


# --------------------------------------------------------------------------- #
# A cancelled booking is refused (the ONE restriction)
# --------------------------------------------------------------------------- #
def test_cancelled_booking_refuses_attendance(db, slots):
    """Nobody failed to attend a drive that was called off. Without this, one
    mis-click texts "we missed you today" to a customer who cancelled politely.
    """
    cid = _customer(db)
    b = _book(db, cid)
    ref = b["booking_ref"]
    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)

    for value in ("noshow", "completed", "pending"):
        out = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance=value)
        assert out["error"] == "booking_cancelled"

    row = db.scalar(select(Booking).where(Booking.ref == ref))
    assert row.attendance == BookingAttendance.pending
    assert _noshow_rows(db, ref) == []


def test_cancelled_booking_is_refused_not_reported_missing(db, slots):
    """It must be findable in order to be refused for the honest reason —
    booking_not_found would send an operator hunting for a typo."""
    cid = _customer(db)
    b = _book(db, cid)
    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=b["booking_ref"])

    out = booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="noshow"
    )
    assert out["error"] != "booking_not_found"
    assert out["booking"]["booking_ref"] == b["booking_ref"]


# --------------------------------------------------------------------------- #
# No-show — exactly one message, ever
# --------------------------------------------------------------------------- #
def test_noshow_writes_one_notice_due_now(db, slots):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    ref = _book(db, cid)["booking_ref"]

    out = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    assert out["changed"] is True and out["notified"] is True

    rows = _noshow_rows(db, ref)
    assert len(rows) == 1
    assert rows[0].status == NotificationStatus.scheduled
    assert rows[0].send_at <= datetime.now() + timedelta(seconds=1)
    # Language-neutral at rest (LOC-01): the text is rendered at dispatch.
    assert rows[0].body == ""
    assert rows[0].payload["booking_ref"] == ref


def test_a_correction_round_trip_does_not_text_twice(db, slots):
    """noshow -> pending -> noshow is an operator correcting themselves. The
    second entry must send nothing."""
    ref = _book(db, _customer(db))["booking_ref"]

    first = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    assert first["notified"] is True

    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="pending")
    again = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    assert again["changed"] is True
    assert again["notified"] is False
    assert len(_noshow_rows(db, ref)) == 1


def test_setting_the_same_value_is_idempotent(db, slots):
    ref = _book(db, _customer(db))["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    out = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    assert out["changed"] is False and out["notified"] is False
    assert len(_noshow_rows(db, ref)) == 1


def test_moving_off_noshow_does_not_retract_the_message(db, slots):
    """A sent message cannot be un-sent, and pretending otherwise would be worse
    than leaving it. The row stays; only the attendance moves."""
    ref = _book(db, _customer(db))["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="completed")

    rows = _noshow_rows(db, ref)
    assert len(rows) == 1
    assert rows[0].status == NotificationStatus.scheduled


def test_noshow_leaves_the_slot_alone(db, slots):
    """Unlike cancel, there is nothing to release — the drive's time has passed
    and the slot was consumed."""
    b = _book(db, _customer(db))
    booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="noshow"
    )
    assert db.get(Slot, b["slot_id"]).status == SlotStatus.booked


# --------------------------------------------------------------------------- #
# The consent gate is the dispatcher's, and it applies here too
# --------------------------------------------------------------------------- #
def test_no_consent_means_the_notice_is_never_sent(db, slots):
    """A customer who declined WhatsApp is not messaged about a missed drive
    either. The row is written and then retired with the reason — the same path
    every other kind takes."""
    from app.services import dispatcher

    ref = _book(db, _customer(db))["booking_ref"]  # no consent recorded
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    db.commit()

    dispatcher.dispatch_due(db)

    row = _noshow_rows(db, ref)[0]
    assert row.status == NotificationStatus.cancelled
    assert row.payload["not_sent_reason"] == "no_consent"


def test_a_consented_customer_gets_it_through_the_normal_dispatcher(db, slots):
    """WhatsApp is off in conftest, so this lands as a SIMULATOR send — which is
    exactly how the demo runs with no credentials."""
    from app.services import dispatcher

    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    ref = _book(db, cid)["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    db.commit()

    dispatcher.dispatch_due(db)

    row = _noshow_rows(db, ref)[0]
    assert row.status == NotificationStatus.sent
    assert row.payload["simulated"] is True
    assert row.body  # rendered at dispatch, not at write time


# --------------------------------------------------------------------------- #
# The copy (rule 14, rule 17, LOC-01)
# --------------------------------------------------------------------------- #
def _render(db, ref: str, lang: Language) -> str:
    row = _noshow_rows(db, ref)[0]
    row.lang = lang
    return notif_service.render_body(row)


def test_the_notice_renders_in_both_languages(db, slots):
    b = _book(db, _customer(db))
    ref = b["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    en = _render(db, ref, Language.en)
    assert "Aisha" in en and ref in en
    assert b["vehicle_name"] in en

    ar = _render(db, ref, Language.ar)
    assert "عايشة" in ar and ref in ar
    # Natively written, not a translation of the English (FR-CX-02/03).
    assert any("؀" <= ch <= "ۿ" for ch in ar)


def test_the_notice_offers_rather_than_reproaches(db, slots):
    """Rule 14 — warm Gulf hospitality, no scarcity, no scolding."""
    ref = _book(db, _customer(db))["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    en = _render(db, ref, Language.en).lower()
    assert "reply" in en
    for scold in ("failed", "you did not", "you didn't", "missed appointment", "hurry"):
        assert scold not in en


def test_the_notice_carries_no_markdown_and_no_dash(db, slots):
    """Rule 17 — WhatsApp renders none of it. The em dash ban (2026-07-22)
    applies to every customer-facing string, not just the agent's."""
    ref = _book(db, _customer(db))["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")

    for lang in (Language.en, Language.ar):
        body = _render(db, ref, lang)
        for token in ("**", "##", "- ", "—", "–", "`"):
            assert token not in body, (lang, token)


# --------------------------------------------------------------------------- #
# Audit (rule 15) — a PERSON did this
# --------------------------------------------------------------------------- #
def test_every_change_is_audited_as_admin(db, slots):
    ref = _book(db, _customer(db))["booking_ref"]

    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="noshow")
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="completed")

    events = _attendance_audit(db)
    assert len(events) == 2
    assert all(e.actor == Actor.admin for e in events)
    assert [(e.payload["from"], e.payload["to"]) for e in events] == [
        ("pending", "noshow"),
        ("noshow", "completed"),
    ]
    assert [e.payload["notified"] for e in events] == [True, False]


def test_a_no_op_writes_no_audit_row(db, slots):
    ref = _book(db, _customer(db))["booking_ref"]
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="completed")
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="completed")
    assert len(_attendance_audit(db)) == 1


# --------------------------------------------------------------------------- #
# Bad input is data, never an exception (the house rule for every tool/service)
# --------------------------------------------------------------------------- #
def test_unknown_ref_is_an_error_dict(db, slots):
    out = booking.set_attendance(
        db, session_id=SESSION, ref="TD-2026-9999", attendance="noshow"
    )
    assert out["error"] == "booking_not_found"


def test_unknown_value_is_refused_not_coerced(db, slots):
    ref = _book(db, _customer(db))["booking_ref"]
    out = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="attended")
    assert out["error"] == "invalid_attendance"

    row = db.scalar(select(Booking).where(Booking.ref == ref))
    assert row.attendance == BookingAttendance.pending


def test_console_casing_is_tolerated(db, slots):
    """It arrives from a UI, so "No Show" resolves — but only to a real value."""
    ref = _book(db, _customer(db))["booking_ref"]
    out = booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="No Show")
    assert out["booking"]["attendance"] == BookingAttendance.noshow.value


def test_missing_ref_is_an_error_dict(db, slots):
    out = booking.set_attendance(db, session_id=SESSION, ref=None, attendance="noshow")
    assert out["error"] == "identifier_required"
