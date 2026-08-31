"""Post-drive feedback survey over WhatsApp (2026-07-26).

The first stateful conversation this system holds on WhatsApp — and the reason
it is safe to hold one is that no model is involved anywhere in it. These tests
pin exactly that: fixed strings in, list-reply ids out, `step` advanced by
ordinary Python.

What is pinned:

  * a survey opens only on the transition INTO completed, exactly once per
    booking, guarded by feedback_requested_at
  * the opening rides the notification rails, so it inherits the CONSENT gate —
    a customer who never agreed to WhatsApp is never surveyed
  * the tap id names its own question, so answers cannot be misfiled
  * a real booking intent mid-survey ABANDONS it and falls through to handoff —
    nobody is ever trapped in a questionnaire while trying to cancel
  * later is honoured once, skip closes without touching whatsapp_consent
  * partial answers survive skip, expiry and abandonment
  * one open survey per customer; a second queues and is promoted on close
  * rule 12 erasure takes the survey and counts it in would_delete

DB-only, like the rest of the suite: WhatsApp is off in conftest, so both
senders short-circuit to `skipped` and NO vendor is ever called. That also
proves the flow is fully rehearsable with no credentials, which is what demo
morning depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import config
from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    Booking,
    Customer,
    FeedbackStatus,
    FeedbackSurvey,
    Language,
    LocationType,
    Notification,
    NotificationKind,
    NotificationStatus,
    Slot,
    SlotStatus,
)
from app.services import booking, consent, customers, dispatcher, erasure
from app.services import feedback as fb
from app.services import slots as slots_service
from app.services import whatsapp_inbound

SESSION = "sess-feedback"
MOBILE = "+971501119911"


# --------------------------------------------------------------------------- #
# Helpers — real bookings through the real guards, never an INSERT
# --------------------------------------------------------------------------- #
def _customer(db, mobile=MOBILE, **kw) -> int:
    return customers.find_or_create(db, mobile=mobile, **kw)["customer"]["customer_id"]


def _far_slots(db, count=2, *, location=LocationType.showroom) -> list[Slot]:
    cutoff = datetime.now() + timedelta(hours=25)
    return list(
        db.scalars(
            select(Slot)
            .where(
                Slot.status == SlotStatus.open,
                Slot.location_type == location,
                Slot.start > cutoff,
            )
            .order_by(Slot.start, Slot.id)
            .limit(count)
        ).all()
    )


def _book(db, cid: int, *, slot: Slot | None = None, lang=None, session=SESSION) -> dict:
    slot = slot or _far_slots(db, 1)[0]
    assert slots_service.lock(db, session_id=session, slot_id=slot.id)["locked"] is True
    slots_service.acknowledge_licence_reminder(db, session_id=session)
    out = booking.create(
        db,
        session_id=session,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
        # A doorstep drive cannot be booked without one (FR-SL-07), so the helper
        # supplies it rather than each doorstep test remembering to.
        doorstep_address=(
            "Villa 12, Jumeirah 1, Dubai"
            if slot.location_type == LocationType.doorstep
            else None
        ),
        lang=lang,
    )
    assert out.get("created") is True, out
    return out["booking"]


def _complete(db, ref: str, *, now=None) -> dict:
    return booking.set_attendance(
        db, session_id=SESSION, ref=ref, attendance="completed", now=now
    )


def _survey(db, ref: str) -> FeedbackSurvey | None:
    return db.scalar(select(FeedbackSurvey).where(FeedbackSurvey.booking_ref == ref))


def _feedback_rows(db, ref: str) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification).where(
                Notification.ref == ref,
                Notification.kind == NotificationKind.feedback,
            )
        ).all()
    )


def _consented(db, mobile=MOBILE) -> int:
    """A customer who agreed to WhatsApp — the dispatcher gate needs this."""
    cid = _customer(db, mobile=mobile)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    return cid


def _deliver(db, *, now=None) -> dict:
    """Run one dispatcher tick, i.e. actually send the opening."""
    return dispatcher.dispatch_due(db, now=now or datetime.now())


def _open_and_send(db, cid: int, **kw) -> tuple[str, FeedbackSurvey]:
    """The common setup: book, complete, deliver the opening, return the survey."""
    b = _book(db, cid, **kw)
    _complete(db, b["booking_ref"])
    db.commit()
    _deliver(db)
    survey = _survey(db, b["booking_ref"])
    assert survey.status == FeedbackStatus.sent
    return b["booking_ref"], survey


def _reply(db, cid: int, text=None, *, list_reply_id=None, now=None):
    cust = db.get(Customer, cid)
    return fb.handle_reply(
        db, customer=cust, text=text, list_reply_id=list_reply_id, now=now
    )


# --------------------------------------------------------------------------- #
# Opening — only on completed, exactly once
# --------------------------------------------------------------------------- #
def test_no_survey_until_the_drive_is_marked_completed(db, slots):
    b = _book(db, _consented(db))
    assert _survey(db, b["booking_ref"]) is None

    # A no-show is not a completed drive: it gets the "we missed you" notice, and
    # asking someone how their test drive went when they never came would be a
    # claim about a thing that did not happen (rule 2).
    booking.set_attendance(
        db, session_id=SESSION, ref=b["booking_ref"], attendance="noshow"
    )
    assert _survey(db, b["booking_ref"]) is None


def test_completed_opens_a_survey_and_queues_the_opening(db, slots):
    b = _book(db, _consented(db))
    out = _complete(db, b["booking_ref"])

    assert out["feedback"]["opened"] is True
    assert out["feedback"]["queued"] is False

    survey = _survey(db, b["booking_ref"])
    assert survey.status == FeedbackStatus.pending
    assert survey.step == 0
    assert survey.sent_at is None

    rows = _feedback_rows(db, b["booking_ref"])
    assert len(rows) == 1
    assert rows[0].status == NotificationStatus.scheduled
    # Language-neutral at write time, rendered at dispatch (LOC-01).
    assert rows[0].body == ""


def test_the_survey_fires_exactly_once_per_booking(db, slots):
    """An operator correcting a mis-click must not survey the customer twice."""
    cid = _consented(db)
    b = _book(db, cid)
    ref = b["booking_ref"]

    _complete(db, ref)
    stamped = db.scalar(select(Booking).where(Booking.ref == ref)).feedback_requested_at
    assert stamped is not None

    # completed -> pending -> completed: a correction, then a re-correction.
    booking.set_attendance(db, session_id=SESSION, ref=ref, attendance="pending")
    again = _complete(db, ref)

    assert "feedback" not in again  # the guard short-circuited it
    assert len(_feedback_rows(db, ref)) == 1
    # Moving off `completed` retracts nothing — a WhatsApp already sent cannot be
    # recalled, and pretending otherwise is worse than leaving the stamp.
    assert db.scalar(select(Booking).where(Booking.ref == ref)).feedback_requested_at == stamped


def test_a_cancelled_booking_is_never_surveyed(db, slots):
    cid = _consented(db)
    b = _book(db, cid)
    # session_customer_id is the identity guard: a guessed ref cannot cancel a
    # stranger's drive.
    out = booking.cancel(
        db, session_id=SESSION, session_customer_id=cid, ref=b["booking_ref"]
    )
    assert out.get("cancelled") is True, out
    out = _complete(db, b["booking_ref"])
    assert out.get("error") == "booking_cancelled"
    assert _survey(db, b["booking_ref"]) is None


# --------------------------------------------------------------------------- #
# The consent gate — inherited from the notification rails
# --------------------------------------------------------------------------- #
def test_a_customer_without_whatsapp_consent_is_not_surveyed(db, slots):
    """The whole reason the opening goes through the dispatcher: this gate is
    checked at SEND time, so consent withdrawn between the drive and the survey
    still stops the message."""
    cid = _customer(db)  # deliberately NO consent
    b = _book(db, cid)
    _complete(db, b["booking_ref"])
    db.commit()

    _deliver(db)

    row = _feedback_rows(db, b["booking_ref"])[0]
    assert row.status == NotificationStatus.cancelled
    assert row.payload["not_sent_reason"] == "no_consent"

    # And the survey is CLOSED, not left at `pending`. This is the important half:
    # a survey stuck at `pending` blocks promotion, so one undeliverable opening
    # would silently stop this customer ever being surveyed again.
    survey = _survey(db, b["booking_ref"])
    assert survey.status == FeedbackStatus.expired
    assert survey.closed_at is not None
    # "We never asked" is still distinguishable from "they never answered" — the
    # reason lives on the notification row and in the audit event.
    events = [
        (r.payload or {}).get("event")
        for r in db.scalars(select(AuditEvent)).all()
        if r.type == AuditType.feedback
    ]
    assert "opening_not_sent" in events


def test_an_undeliverable_opening_does_not_block_the_next_survey(db, slots):
    """The regression the abort path exists for: without it, a customer whose
    first survey could not be sent would never be surveyed again."""
    cid = _customer(db)  # no consent — the first opening is retired
    first, second = _far_slots(db, 2)

    a = _book(db, cid, slot=first)
    _complete(db, a["booking_ref"])
    db.commit()
    _deliver(db)
    assert _survey(db, a["booking_ref"]).status == FeedbackStatus.expired

    # Now they consent, and a second drive completes.
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    b = _book(db, cid, slot=second, session="sess-second")
    out = _complete(db, b["booking_ref"])
    db.commit()

    assert out["feedback"]["queued"] is False  # nothing is blocking it
    _deliver(db)
    assert _survey(db, b["booking_ref"]).status == FeedbackStatus.sent


def test_delivering_the_opening_marks_the_survey_sent(db, slots):
    cid = _consented(db)
    b = _book(db, cid)
    _complete(db, b["booking_ref"])
    db.commit()

    result = _deliver(db)
    assert result["sent"] >= 1

    survey = _survey(db, b["booking_ref"])
    assert survey.status == FeedbackStatus.sent
    assert survey.sent_at is not None
    # Rendered text is persisted onto the row, so the console feed shows exactly
    # what went out.
    row = _feedback_rows(db, b["booking_ref"])[0]
    assert row.status == NotificationStatus.sent
    assert row.body


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #
def test_the_opening_names_the_car_and_derives_the_day(db, slots):
    """The source screenshot says "yesterday", but the survey is sent minutes
    after the drive is marked complete. Saying "yesterday" about something that
    happened at lunchtime is a false claim about a fact we hold (rule 2), so the
    word is derived from the slot."""
    cid = _consented(db)
    ref, survey = _open_and_send(db, cid)

    body = _feedback_rows(db, ref)[0].body
    assert survey.booking.vehicle.name in body
    assert "four quick taps" in body
    assert "'later'" in body and "'skip'" in body
    # The slot is >25h in the FUTURE in this fixture, so "yesterday" would be
    # doubly wrong. The derivation must not produce it.
    assert "yesterday" not in body


def test_no_markdown_and_no_emoji_anywhere_in_the_copy(db, slots):
    """Rule 17: this channel renders none of it, and every other WhatsApp
    message in the system is plain text."""
    cid = _consented(db)
    ref, survey = _open_and_send(db, cid)

    texts = [_feedback_rows(db, ref)[0].body, fb.render_comment_prompt(survey),
             fb.render_thanks(survey), fb.render_skip_ack(survey),
             fb.render_later_ack(survey)]
    texts += [fb.render_question(survey, s) for s in fb.QUESTION_COLUMNS]

    for text in texts:
        assert "**" not in text
        assert "##" not in text
        assert not any(ord(c) > 0x2600 for c in text), text


def test_arabic_customers_get_arabic_copy(db, slots):
    cid = _consented(db, mobile="+971501119912")
    db.get(Customer, cid).language_pref = Language.ar
    db.flush()

    ref, survey = _open_and_send(db, cid)
    assert survey.lang == Language.ar

    body = _feedback_rows(db, ref)[0].body
    assert any("؀" <= c <= "ۿ" for c in body), body
    assert "مرحبًا" in body
    for step in fb.QUESTION_COLUMNS:
        assert any("؀" <= c <= "ۿ" for c in fb.render_question(survey, step))


def test_q4_asks_doorstep_customers_about_the_right_person(db, slots):
    """Asking a doorstep customer about the team who "welcomed" them is plainly
    wrong — nobody welcomed them anywhere."""
    cid = _consented(db)
    showroom = _book(db, cid, slot=_far_slots(db, 1)[0])
    _complete(db, showroom["booking_ref"])
    showroom_survey = _survey(db, showroom["booking_ref"])

    door_slot = _far_slots(db, 1, location=LocationType.doorstep)[0]
    cid2 = _consented(db, mobile="+971501119913")
    door = _book(
        db, cid2, slot=door_slot, session="sess-door"
    )
    _complete(db, door["booking_ref"])
    door_survey = _survey(db, door["booking_ref"])

    assert "welcomed you" in fb.render_question(showroom_survey, 4)
    assert "brought the car to you" in fb.render_question(door_survey, 4)
    # Both phrased about a ROLE, never a named individual — the answer must not
    # be able to read as a complaint about one identifiable person.
    assert (showroom_survey.customer.name_en or "x") not in fb.render_question(
        showroom_survey, 4
    )


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #
def test_any_reply_starts_the_survey_then_taps_advance_it(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)

    # "sure why not" is agreement. Insisting on the word "yes" would strand a
    # customer who answered like a person.
    out = _reply(db, cid, "sure why not")
    assert out["event"] == "started"
    assert _survey(db, ref).step == 1
    assert _survey(db, ref).status == FeedbackStatus.in_progress

    for step, score in ((1, 5), (2, 4), (3, 5), (4, 3)):
        out = _reply(db, cid, list_reply_id=f"q{step}:{score}")
        assert out["event"] == "answer"

    survey = _survey(db, ref)
    assert (survey.q1_overall, survey.q2_ease, survey.q3_vehicle, survey.q4_team) == (
        5, 4, 5, 3
    )
    assert survey.step == fb.COMMENT_STEP

    out = _reply(db, cid, "Salesman was excellent, car felt solid.")
    assert out["event"] == "comment"

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.completed
    assert survey.comment == "Salesman was excellent, car felt solid."
    assert survey.closed_at is not None


def test_a_tap_is_filed_against_its_own_question_not_the_current_one(db, slots):
    """The id carries the question AND the score, so scrolling back to an older
    message and tapping it cannot corrupt the current answer."""
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:5")
    _reply(db, cid, list_reply_id="q2:4")
    assert _survey(db, ref).step == 3

    # Re-answer Q1 from an older message.
    _reply(db, cid, list_reply_id="q1:2")

    survey = _survey(db, ref)
    assert survey.q1_overall == 2  # updated
    assert survey.q2_ease == 4  # untouched
    assert survey.step == 3  # NOT dragged backwards


def test_a_typed_digit_counts_but_prose_does_not(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")

    out = _reply(db, cid, "5")
    assert out["event"] == "answer"
    assert _survey(db, ref).q1_overall == 5

    # "5 stars, great car" is a COMMENT. Mining a digit out of prose would file a
    # rating nobody meant to give, so it re-prompts instead.
    out = _reply(db, cid, "5 stars, great car")
    assert out["event"] == "reprompt"
    assert _survey(db, ref).q2_ease is None
    assert _survey(db, ref).step == 2


def test_arabic_indic_digits_are_accepted(db, slots):
    cid = _consented(db, mobile="+971501119914")
    db.get(Customer, cid).language_pref = Language.ar
    db.flush()
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "نعم")
    _reply(db, cid, "٤")
    assert _survey(db, ref).q1_overall == 4


# --------------------------------------------------------------------------- #
# later / skip
# --------------------------------------------------------------------------- #
def test_later_defers_once_then_a_second_later_is_a_polite_no(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    now = datetime.now()

    out = _reply(db, cid, "later please", now=now)
    assert out["event"] == "later"

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.pending  # re-queued
    assert survey.sent_at is None
    assert survey.later_count == 1

    rows = sorted(_feedback_rows(db, ref), key=lambda r: r.send_at)
    assert len(rows) == 2
    # The resend goes through the rails again, so it re-checks consent — which
    # can be withdrawn in the intervening hours.
    assert rows[1].send_at >= now + timedelta(hours=config.FEEDBACK_LATER_HOURS - 1)
    assert rows[1].status == NotificationStatus.scheduled

    # Deliver the resend, then decline again.
    db.commit()
    _deliver(db, now=now + timedelta(hours=config.FEEDBACK_LATER_HOURS + 1))
    assert _survey(db, ref).status == FeedbackStatus.sent

    out = _reply(db, cid, "later")
    assert out["event"] == "skipped"
    assert _survey(db, ref).status == FeedbackStatus.skipped


def test_skip_closes_the_survey_and_keeps_whatsapp_consent(db, slots):
    """Declining one survey must never cost a customer their booking reminders."""
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:2")

    out = _reply(db, cid, "skip")
    assert out["event"] == "skipped"

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.skipped
    # PARTIAL ANSWERS KEPT: a 2 followed by silence is the most useful thing in
    # the survey, and discarding it to keep the data tidy would throw away the
    # only signal worth acting on.
    assert survey.q1_overall == 2
    assert db.get(Customer, cid).whatsapp_consent is True


def test_skip_words_do_not_swallow_the_open_comment(db, slots):
    """At the comment step the customer was ASKED for free prose, so "I'd skip
    the panoramic roof" is a comment, not a command."""
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    for step in fb.QUESTION_COLUMNS:
        _reply(db, cid, list_reply_id=f"q{step}:4")
    assert _survey(db, ref).step == fb.COMMENT_STEP

    _reply(db, cid, "Great car, though I'd skip the panoramic roof next time")

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.completed
    assert "skip the panoramic roof" in survey.comment


# --------------------------------------------------------------------------- #
# A real request always wins
# --------------------------------------------------------------------------- #
def test_a_booking_intent_mid_survey_abandons_it_and_hands_off(db, slots, monkeypatch):
    """The single worst thing this feature could do is trap someone in a
    satisfaction questionnaire while they are trying to cancel."""
    monkeypatch.setattr(config, "WHATSAPP_INBOUND_ENABLED", True)
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:5")
    db.commit()

    out = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn=MOBILE.lstrip("+"), text="actually cancel my booking", wamid="w-cancel"
    )

    # Fell THROUGH to the ordinary handoff: a token was minted and the link sent.
    assert out["ok"] is True
    assert out.get("token")
    assert out["intent"] == "cancel"

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.abandoned
    assert survey.q1_overall == 5  # partial kept — it was freely given
    # NOT `skipped`: they were busy with something that actually mattered, and
    # that must not read as a refusal to answer in the response-rate figures.
    assert survey.status != FeedbackStatus.skipped


def test_a_tapped_rating_is_never_read_as_a_booking_intent(db, slots, monkeypatch):
    """A row title is text WE wrote, so running it through the keyword matcher
    could only ever produce a false positive."""
    monkeypatch.setattr(config, "WHATSAPP_INBOUND_ENABLED", True)
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    db.commit()

    out = whatsapp_inbound.handle_inbound_text(
        db,
        from_msisdn=MOBILE.lstrip("+"),
        text="5 — Excellent",
        wamid="w-tap",
        list_reply_id="q1:5",
    )
    assert out.get("feedback", {}).get("event") == "answer"
    assert _survey(db, ref).q1_overall == 5
    assert _survey(db, ref).status == FeedbackStatus.in_progress


def test_inbound_falls_through_normally_when_no_survey_is_open(db, slots, monkeypatch):
    """The survey must be invisible to every customer who does not have one."""
    monkeypatch.setattr(config, "WHATSAPP_INBOUND_ENABLED", True)
    _consented(db)
    db.commit()

    out = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn=MOBILE.lstrip("+"), text="hello there", wamid="w-plain"
    )
    assert out.get("token")
    assert "feedback" not in out


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def test_a_stale_survey_expires_and_keeps_its_partial(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:3")

    later = datetime.now() + timedelta(hours=config.FEEDBACK_EXPIRY_HOURS + 1)
    assert fb.expire_stale(db, now=later) == 1

    survey = _survey(db, ref)
    assert survey.status == FeedbackStatus.expired
    assert survey.q1_overall == 3
    assert survey.closed_at is not None


def test_a_fresh_survey_is_not_immediately_stale(db, slots):
    """Guards the UTC-vs-local trap: created_at/updated_at are stamped from the
    same clock the sweep compares against, so a brand-new survey on a +04
    deployment is not four hours old the moment it is written."""
    cid = _consented(db)
    now = datetime.now()
    b = _book(db, cid)
    _complete(db, b["booking_ref"], now=now)
    db.commit()
    _deliver(db, now=now)

    survey = _survey(db, b["booking_ref"])
    assert abs((survey.updated_at - now).total_seconds()) < 60
    assert abs((survey.created_at - now).total_seconds()) < 60
    # Nothing is stale one second after being asked.
    assert fb.expire_stale(db, now=now + timedelta(seconds=1)) == 0


def test_answering_re_arms_the_expiry_window(db, slots):
    """Measured from the last activity, so a slow answerer is never cut off
    mid-question because the opening went out yesterday."""
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)

    almost = datetime.now() + timedelta(hours=config.FEEDBACK_EXPIRY_HOURS - 1)
    _reply(db, cid, "yes", now=almost)

    # The original window has now passed, but activity was recent.
    assert fb.expire_stale(db, now=almost + timedelta(hours=1)) == 0
    assert _survey(db, ref).status == FeedbackStatus.in_progress

    assert fb.expire_stale(
        db, now=almost + timedelta(hours=config.FEEDBACK_EXPIRY_HOURS + 1)
    ) == 1


# --------------------------------------------------------------------------- #
# One open survey per customer
# --------------------------------------------------------------------------- #
def test_a_second_survey_queues_and_is_promoted_when_the_first_closes(db, slots):
    """A bare "5" from a customer with two live surveys is unattributable, and
    guessing files real feedback against the wrong drive."""
    cid = _consented(db)
    first_slot, second_slot = _far_slots(db, 2)

    a = _book(db, cid, slot=first_slot)
    _complete(db, a["booking_ref"])
    db.commit()
    _deliver(db)

    b = _book(db, cid, slot=second_slot, session="sess-second")
    out = _complete(db, b["booking_ref"])
    db.commit()

    assert out["feedback"]["queued"] is True
    assert _feedback_rows(db, b["booking_ref"]) == []  # nothing to send yet
    # `queued`, NOT `pending`: the two must stay distinct or the promotion check
    # sees this very survey as the thing blocking its own release.
    assert _survey(db, b["booking_ref"]).status == FeedbackStatus.queued

    # Every reply routes to A, unambiguously.
    _reply(db, cid, "yes")
    assert fb.active_for_customer(db, cid).booking_ref == a["booking_ref"]

    # Close A -> B is released onto the rails.
    _reply(db, cid, "skip")
    assert _survey(db, a["booking_ref"]).status == FeedbackStatus.skipped
    assert len(_feedback_rows(db, b["booking_ref"])) == 1

    db.commit()
    _deliver(db)
    assert _survey(db, b["booking_ref"]).status == FeedbackStatus.sent


# --------------------------------------------------------------------------- #
# Audit (rule 15)
# --------------------------------------------------------------------------- #
def test_every_transition_is_audited_under_its_own_type(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:5")

    rows = [
        r for r in db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        if r.type == AuditType.feedback
    ]
    events = [(r.payload or {}).get("event") for r in rows]
    assert "survey_opened" in events
    assert "opening_sent" in events
    assert "survey_started" in events
    assert "answer" in events

    # The operator's click is actor=admin; the customer's taps are actor=customer;
    # the dispatcher's send is actor=system. The trail must not read as though
    # the agent decided any of it.
    by_event = {(r.payload or {}).get("event"): r.actor for r in rows}
    assert by_event["survey_opened"] == Actor.admin
    assert by_event["opening_sent"] == Actor.system
    assert by_event["answer"] == Actor.customer
    assert Actor.agent not in by_event.values()


# --------------------------------------------------------------------------- #
# Rule 12 — erasure
# --------------------------------------------------------------------------- #
def test_forget_me_names_the_feedback_and_then_deletes_it(db, slots):
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:2")
    _reply(db, cid, "skip")
    db.commit()

    # The unconfirmed call must NAME it: a comment in the customer's own words
    # can identify both them and a member of staff.
    inventory = erasure.inventory(db, cid)
    assert inventory["feedback"] == 1

    erasure.purge(db, customer_id=cid, session_id=SESSION)
    db.commit()

    assert db.scalar(select(FeedbackSurvey).where(FeedbackSurvey.customer_id == cid)) is None
    assert _survey(db, ref) is None
    assert db.get(Customer, cid) is None


def test_the_dispatcher_retires_an_opening_whose_survey_was_erased(db, slots):
    """An opening with no survey behind it would ask a question no reply could
    ever be filed against."""
    cid = _consented(db)
    b = _book(db, cid)
    _complete(db, b["booking_ref"])
    db.commit()

    db.execute(
        FeedbackSurvey.__table__.delete().where(
            FeedbackSurvey.booking_ref == b["booking_ref"]
        )
    )
    db.commit()

    _deliver(db)
    row = _feedback_rows(db, b["booking_ref"])[0]
    assert row.status == NotificationStatus.cancelled
    assert row.payload["not_sent_reason"] == "survey_missing"


# --------------------------------------------------------------------------- #
# The read side
# --------------------------------------------------------------------------- #
def test_unanswered_scores_stay_null_and_the_average_ignores_them(db, slots):
    """A zero would be a score nobody gave, and it would drag every average
    down with it."""
    cid = _consented(db)
    ref, _ = _open_and_send(db, cid)
    _reply(db, cid, "yes")
    _reply(db, cid, list_reply_id="q1:5")
    _reply(db, cid, list_reply_id="q2:4")

    out = fb.survey_dict(_survey(db, ref))
    assert out["scores"] == {"overall": 5, "ease": 4, "vehicle": None, "team": None}
    assert out["answered"] == 2
    assert out["average"] == 4.5

    empty = fb.survey_dict(_survey(db, ref))
    assert empty["average"] is not None
    # A survey with nothing answered has no average to report.
    fresh_cid = _consented(db, mobile="+971501119915")
    fresh_ref, _ = _open_and_send(db, fresh_cid)
    assert fb.survey_dict(_survey(db, fresh_ref))["average"] is None


def test_the_agent_has_no_feedback_tool(db):
    """Rule 2 / rule 12: a satisfaction score the model could READ is a score it
    could VOLUNTEER. There is deliberately no tool and no prompt change."""
    from app.tools import REGISTRY

    names = " ".join(REGISTRY.keys()).lower()
    assert "feedback" not in names
    assert "survey" not in names
    assert "rating" not in names
