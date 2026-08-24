"""Post-drive feedback survey, conducted over WhatsApp (2026-07-26).

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
An operator marks a booking `completed` in the console; the customer gets a
WhatsApp asking how it went, answers four 1-5 questions by TAPPING a list, adds
an optional comment, and the scores land on the booking in the console.

It is the first STATEFUL conversation this system holds on WhatsApp. Until now
inbound WhatsApp only ever handed off to the web — mint a token, reply with a
link — so nothing on that channel had a "where were we". A survey does: a bare
"5" is meaningless without knowing which question is outstanding.

IT IS STILL NOT A SECOND AISHA, and that distinction is the whole reason this is
safe to build. No model is involved at any point. The questions are fixed
strings, the answers arrive as list-reply ids we ourselves minted, and `step` is
advanced by ordinary Python. So:
  - AD-C is untouched — no WebSockets, no streaming, no agent loop;
  - rule 2 holds trivially — nothing here can invent a fact, because nothing here
    generates language;
  - the web agent cannot see, mention, or request any of it. There is no tool and
    no prompt change. A satisfaction score the model could READ is a score it
    could VOLUNTEER, which is exactly the behaviour rule 12 restrains for memory.

ONE OPEN SURVEY PER CUSTOMER
----------------------------
Enforced here rather than by a DB constraint, because "open" spans three
statuses. A second completed booking queues as `pending` and its opening is only
written once the first survey closes. Without that rule, a bare "5" from someone
with two live surveys is genuinely unattributable — and guessing files real
feedback against the wrong drive, which is worse than asking a day later.

A REAL REQUEST ALWAYS BEATS A SURVEY
------------------------------------
If a mid-survey message parses as a booking intent ("cancel my booking"), the
survey is ABANDONED and the message falls through to the normal handoff flow.
Trapping someone in a satisfaction questionnaire while they are trying to cancel
is the single worst thing this feature could do. `abandoned` is its own status
precisely so that case never reads as a refusal to answer.

PARTIAL ANSWERS ARE ALWAYS KEPT
-------------------------------
On skip, on expiry, on abandonment. Someone who rated the car a 2 and stopped has
told us the most useful thing in the survey; discarding it to keep the data tidy
would throw away the only signal worth acting on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import (
    Actor,
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
)
from app.services import audit
from app.services import whatsapp as whatsapp_service
from app.services.notifications import _AR_DAYS

log = logging.getLogger("app.services.feedback")

# Every non-terminal status. Used to decide whether a NEW survey has to queue: a
# customer with any of these already has one in the system.
OPEN_STATUSES = (
    FeedbackStatus.queued,
    FeedbackStatus.pending,
    FeedbackStatus.sent,
    FeedbackStatus.in_progress,
)
# The subset that has actually been RELEASED onto the notification rails. This —
# not OPEN_STATUSES — is what the promotion check tests: including `queued` there
# would make each queued survey the thing blocking its own release, so nothing
# would ever come off the queue.
RELEASED_STATUSES = (
    FeedbackStatus.pending,
    FeedbackStatus.sent,
    FeedbackStatus.in_progress,
)

# step -> the column that step's answer lands in. Also defines the order and the
# count: adding a question is a line here plus its copy, nothing else. Q5
# (recommendation / NPS) was dropped by product decision before this shipped.
QUESTION_COLUMNS = {
    1: "q1_overall",
    2: "q2_ease",
    3: "q3_vehicle",
    4: "q4_team",
}
LAST_QUESTION = max(QUESTION_COLUMNS)
COMMENT_STEP = LAST_QUESTION + 1  # the open close


# --------------------------------------------------------------------------- #
# Keywords — matched on the RAW reply, both languages
# --------------------------------------------------------------------------- #
# Substring matching, not equality: real replies are "later please" and "skip
# this one", not bare tokens. Kept deliberately short so they cannot swallow an
# ordinary comment — note that these are only consulted BEFORE the open-comment
# step, so a customer writing "I'd skip the sunroof next time" in their comment
# is stored verbatim rather than read as a command.
_LATER_WORDS = ("later", "not now", "busy", "لاحقا", "لاحقًا", "بعدين", "مشغول")
_SKIP_WORDS = (
    "skip", "stop", "no thanks", "no thank you", "unsubscribe",
    "تخطي", "تخطى", "تخط", "لا شكرا", "لا شكرًا", "توقف", "كفى",
)


def _matches(text: str, words: tuple[str, ...]) -> bool:
    low = (text or "").strip().lower()
    return any(w in low for w in words)


# --------------------------------------------------------------------------- #
# Copy — EN + AR, warm Gulf register (rule 14), no markdown, no emoji (rule 17)
# --------------------------------------------------------------------------- #
# ⚠️ THE ARABIC IS PENDING LAYLA'S REVIEW. It is written natively in the same
# register as the confirmation/reminder copy in services/notifications, NOT
# machine-translated from the English above it (FR-CX-02/03 forbids exactly
# that) — but it has not been signed off by a native reviewer. Flag before demo.

_RATING_ROWS_EN = [
    ("5", "5 — Excellent"),
    ("4", "4 — Very good"),
    ("3", "3 — Good"),
    ("2", "2 — Fair"),
    ("1", "1 — Poor"),
]
_RATING_ROWS_AR = [
    ("5", "٥ — ممتاز"),
    ("4", "٤ — جيد جدًا"),
    ("3", "٣ — جيد"),
    ("2", "٢ — مقبول"),
    ("1", "١ — ضعيف"),
]
_RATE_BUTTON = {"en": "Tap to rate", "ar": "اختر تقييمك"}


def _when_word(start: datetime | None, now: datetime, lang: str) -> str:
    """"today" / "yesterday" / "on Saturday", derived from the slot.

    The source screenshot says "yesterday", but the survey is sent the moment an
    operator marks the drive complete — usually within the hour. Saying
    "yesterday" about something that happened at lunchtime is a false claim about
    a fact we hold, which is the thing rule 2 exists to prevent, so the word is
    derived rather than hardcoded.
    """
    if start is None:
        return ""
    days = (now.date() - start.date()).days
    if lang == "ar":
        if days <= 0:
            return "اليوم"
        if days == 1:
            return "أمس"
        return f"يوم {_AR_DAYS[start.weekday()]}"
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"on {start.strftime('%A')}"


def _first_name(customer: Customer | None, lang: str) -> str:
    if customer is None:
        return ""
    if lang == "ar" and getattr(customer, "name_ar", None):
        return (customer.name_ar or "").split(" ")[0]
    return (getattr(customer, "name_en", None) or "").split(" ")[0]


def render_opening(survey: FeedbackSurvey, *, now: datetime | None = None) -> str:
    """The opening + re-consent message. Plain text — the taps start at Q1."""
    now = now or datetime.now()
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    booking = survey.booking
    customer = survey.customer
    name = _first_name(customer, lang)
    vehicle = (booking.vehicle.name if booking and booking.vehicle else None) or (
        "السيارة" if lang == "ar" else "the car"
    )
    when = _when_word(booking.slot.start if booking and booking.slot else None, now, lang)
    # "everyone" only when the booking actually recorded companions — the
    # screenshot's "the family" is warm precisely because it is TRUE of that
    # customer, and asserting it of a solo visitor would be an invented fact.
    plural = bool(booking and booking.companions)

    if lang == "ar":
        who = "الجميع" if plural else "إعجابك"
        subject = f"نالت إعجاب {who}" if plural else f"نالت {who}"
        greet = f"مرحبًا {name}!" if name else "مرحبًا!"
        return (
            f"{greet} أتمنى أن تكون الـ {vehicle} قد {subject} {when}. "
            f"ممكن دقيقة واحدة من وقتك — أربع ضغطات سريعة — لنسمع رأيك؟ "
            f"(اكتب «لاحقًا» أو «تخطي» في أي وقت.)"
        )

    who = "everyone" if plural else "you"
    greet = f"Marhaba {name}!" if name else "Marhaba!"
    return (
        f"{greet} I hope the {vehicle} impressed {who} {when}. Could I take one "
        f"minute of your time — four quick taps — to hear how it went? "
        f"(Reply 'later' or 'skip' anytime.)"
    )


def render_question(survey: FeedbackSurvey, step: int) -> str:
    """The body text for one rating question."""
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    booking = survey.booking
    vehicle = (booking.vehicle.name if booking and booking.vehicle else None) or (
        "السيارة" if lang == "ar" else "the car"
    )
    doorstep = bool(booking and booking.location == LocationType.doorstep)

    if lang == "ar":
        return {
            1: "بشكل عام، كيف كانت تجربة القيادة؟",
            2: "وكم كان ترتيب كل شيء معي سهلًا؟",
            3: f"والسيارة — هل كانت الـ {vehicle} بالمستوى الذي كنت تتوقعه؟",
            4: (
                "والمختص الذي أحضر لك السيارة؟"
                if doorstep
                else "وفريقنا الذي استقبلك؟"
            ),
        }[step]

    return {
        1: "Overall, how was your test drive experience?",
        2: "How easy was it to arrange everything with me?",
        3: f"The car — did the {vehicle} live up to what you were hoping for?",
        4: (
            # Asking a doorstep customer about the team who "welcomed" them is
            # plainly wrong — nobody welcomed them anywhere. Both variants are
            # phrased about a ROLE, never a named individual, so the answer can
            # never read as a complaint about one identifiable person.
            "And our specialist who brought the car to you?"
            if doorstep
            else "And our team who welcomed you?"
        ),
    }[step]


def render_comment_prompt(survey: FeedbackSurvey) -> str:
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    if lang == "ar":
        # "إيجابي أو سلبي" invites honesty against courtesy-bias, which is the
        # entire point of the open close — a customer who is being polite needs
        # explicit permission to say the drive was disappointing.
        return "سؤال أخير — أي شيء تحب أن تخبرنا به، إيجابي أو سلبي؟ اكتبه هنا."
    return (
        "Last one — anything you'd like to tell us, good or bad? Just type it here."
    )


def render_thanks(survey: FeedbackSurvey) -> str:
    """Varied phrasing, never over-thanks (rule 14).

    Chosen deterministically from the booking ref rather than at random, so the
    same customer sees the same sign-off if a message is redelivered, and so the
    test suite stays deterministic without monkeypatching randomness.
    """
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    name = _first_name(survey.customer, lang)
    variants_en = [
        f"Shukran {name} — this genuinely helps us improve." if name
        else "Shukran — this genuinely helps us improve.",
        f"That's really useful, {name} — thank you for taking the time." if name
        else "That's really useful — thank you for taking the time.",
        f"Appreciated, {name}. We'll put it to good use." if name
        else "Appreciated. We'll put it to good use.",
    ]
    variants_ar = [
        f"شكرًا {name} — رأيك يساعدنا فعلًا على التحسين." if name
        else "شكرًا لك — رأيك يساعدنا فعلًا على التحسين.",
        f"مفيد جدًا يا {name}، شكرًا على وقتك." if name
        else "مفيد جدًا، شكرًا على وقتك.",
        f"نقدّر لك هذا يا {name}، وسنستفيد منه." if name
        else "نقدّر لك هذا، وسنستفيد منه.",
    ]
    variants = variants_ar if lang == "ar" else variants_en
    return variants[sum(ord(c) for c in (survey.booking_ref or "")) % len(variants)]


def render_skip_ack(survey: FeedbackSurvey) -> str:
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    name = _first_name(survey.customer, lang)
    if lang == "ar":
        return (
            f"لا مشكلة أبدًا{' يا ' + name if name else ''} — شكرًا على وقتك على أي حال. "
            f"نتشرّف بخدمتك في أي وقت."
        )
    return (
        f"No problem at all{', ' + name if name else ''} — shukran for your time "
        f"either way. We're here whenever you need us."
    )


def render_later_ack(survey: FeedbackSurvey) -> str:
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    if lang == "ar":
        return "بكل تأكيد — سأعود إليك لاحقًا. شكرًا لك."
    return "Of course — I'll come back to you a little later. Thank you."


def _rating_rows(survey: FeedbackSurvey, step: int) -> list[dict]:
    """The five tappable rows. The id carries BOTH the question and the score.

    That is what makes a late tap harmless: if a customer opens an older message
    and taps it, the answer is filed against the question it belonged to, not
    against whichever one happens to be current.
    """
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    rows = _RATING_ROWS_AR if lang == "ar" else _RATING_ROWS_EN
    return [{"id": f"q{step}:{value}", "title": title} for value, title in rows]


def parse_list_reply(reply_id: str | None) -> tuple[int, int] | None:
    """"q3:4" -> (3, 4). None if it is not one of ours or is out of range."""
    if not reply_id or ":" not in reply_id:
        return None
    head, _, tail = reply_id.partition(":")
    if not head.startswith("q"):
        return None
    try:
        step, score = int(head[1:]), int(tail)
    except ValueError:
        return None
    if step not in QUESTION_COLUMNS or not 1 <= score <= 5:
        return None
    return step, score


def parse_typed_score(text: str) -> int | None:
    """A bare 1-5 typed instead of tapped.

    Accepted because a customer who types "5" has unambiguously answered, and
    refusing it to insist on the tap would be pedantic. Anything longer is NOT
    treated as a score — "5 stars, great car" is a comment, and mining a digit
    out of prose would file a rating nobody meant to give. Arabic-Indic digits
    are accepted because an Arabic keyboard produces them by default.
    """
    raw = (text or "").strip()
    translated = raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if len(translated) == 1 and translated.isdigit():
        score = int(translated)
        if 1 <= score <= 5:
            return score
    return None


# --------------------------------------------------------------------------- #
# Sending — every outbound message from the survey goes through here
# --------------------------------------------------------------------------- #
def _send_question(survey: FeedbackSurvey, step: int) -> dict:
    """Send one rating question as an interactive list. Never raises."""
    mobile = survey.customer.mobile if survey.customer else None
    if not mobile:
        return {"ok": False, "reason": "no_mobile"}
    lang = survey.lang.value if isinstance(survey.lang, Language) else str(survey.lang)
    return whatsapp_service.send_interactive_list(
        mobile,
        body=render_question(survey, step),
        button=_RATE_BUTTON.get(lang, _RATE_BUTTON["en"]),
        rows=_rating_rows(survey, step),
    )


def _send_text(survey: FeedbackSurvey, body: str) -> dict:
    mobile = survey.customer.mobile if survey.customer else None
    if not mobile:
        return {"ok": False, "reason": "no_mobile"}
    return whatsapp_service.send_text(mobile, body)


def _audit(
    db: Session,
    survey: FeedbackSurvey,
    *,
    actor: Actor,
    event: str,
    extra: dict | None = None,
    correlation_id: str | None = None,
) -> None:
    """One audit row per survey transition (rule 15).

    Keyed on `feedback:<ref>` rather than a web session id, for the same reason
    the notification dispatcher keys on `notify:<ref>`: this conversation has no
    web session, and inventing one would make the trail unfindable from the
    booking it belongs to.
    """
    audit.record(
        db,
        session=f"feedback:{survey.booking_ref}",
        actor=actor,
        type=AuditType.feedback,
        payload={
            "ref": survey.booking_ref,
            "customer_id": survey.customer_id,
            "event": event,
            "status": survey.status.value,
            "step": survey.step,
            **(extra or {}),
        },
        correlation_id=correlation_id,
    )


def _close(
    db: Session,
    survey: FeedbackSurvey,
    status: FeedbackStatus,
    *,
    now: datetime,
    actor: Actor = Actor.customer,
    event: str | None = None,
) -> None:
    """Move a survey to a terminal status and let this customer's next one go.

    Promotion happens HERE rather than in each caller so that no path — skip,
    expiry, abandonment, completion — can close a survey and leave a queued one
    stranded forever behind it.
    """
    survey.status = status
    survey.closed_at = now
    _audit(db, survey, actor=actor, event=event or f"survey_{status.value}")
    _promote_next_queued(db, survey.customer_id, now=now)


# --------------------------------------------------------------------------- #
# Opening a survey
# --------------------------------------------------------------------------- #
def _has_survey_in(
    db: Session,
    customer_id: int,
    statuses: tuple[FeedbackStatus, ...],
    *,
    exclude_ref: str | None = None,
) -> bool:
    stmt = select(FeedbackSurvey).where(
        FeedbackSurvey.customer_id == customer_id,
        FeedbackSurvey.status.in_(statuses),
    )
    if exclude_ref:
        stmt = stmt.where(FeedbackSurvey.booking_ref != exclude_ref)
    return db.scalar(stmt.limit(1)) is not None


def _write_opening_notification(db: Session, survey: FeedbackSurvey, *, now: datetime) -> None:
    """Queue the opening on the ordinary notification rails.

    Deliberately NOT sent straight from here. Going through the dispatcher is
    what buys the consent gate (checked at SEND time, so a customer who withdrew
    consent between the drive and the survey is not messaged), the bounded retry
    for a closed 24h window, simulator mode when WhatsApp is unconfigured, and
    the notification audit trail — all of it already built, none of it worth
    reimplementing here. Only the OPENING rides these rails; Q1..Q4 are replies
    inside a live conversation, so they are sent directly.

    `body` stays "" and the facts go to `payload`, like every other notification
    row: the text is composed at dispatch (LOC-01). No customer NAME is stored
    here — the dispatcher hands the customer to render_opening_for_dispatch
    instead, so this table gains no personal data it did not already hold.
    """
    booking = survey.booking
    slot = booking.slot if booking else None
    db.add(
        Notification(
            ref=survey.booking_ref,
            kind=NotificationKind.feedback,
            lang=survey.lang,
            body="",
            payload={
                "booking_ref": survey.booking_ref,
                "customer_id": survey.customer_id,
                "vehicle_id": booking.vehicle_id if booking else None,
                "vehicle_name": booking.vehicle.name if booking and booking.vehicle else None,
                "slot_start_iso": slot.start.isoformat() if slot else None,
                "location_type": booking.location.value if booking else None,
                "kind": NotificationKind.feedback.value,
            },
            status=NotificationStatus.scheduled,
            send_at=now,
        )
    )


def _promote_next_queued(db: Session, customer_id: int, *, now: datetime) -> FeedbackSurvey | None:
    """Release the oldest `queued` survey for this customer, if nothing is live.

    Called from _close, so no path — skip, expiry, abandonment, completion, or an
    opening that could not be sent — can retire a survey and leave a queued one
    stranded behind it forever.
    """
    if _has_survey_in(db, customer_id, RELEASED_STATUSES):
        return None
    nxt = db.scalar(
        select(FeedbackSurvey)
        .where(
            FeedbackSurvey.customer_id == customer_id,
            FeedbackSurvey.status == FeedbackStatus.queued,
        )
        .order_by(FeedbackSurvey.created_at)
        .limit(1)
    )
    if nxt is None:
        return None
    nxt.status = FeedbackStatus.pending
    _write_opening_notification(db, nxt, now=now)
    _audit(db, nxt, actor=Actor.system, event="survey_promoted")
    log.info("feedback survey promoted from queue: %s", nxt.booking_ref)
    return nxt


def open_for_booking(
    db: Session,
    *,
    booking: Booking,
    session_id: str,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Open the survey for a booking an operator just marked `completed`.

    Called ONLY from booking.set_attendance, and only on the transition INTO
    `completed` — the once-per-booking guard (Booking.feedback_requested_at)
    lives there, alongside the identical guard for the no-show notice, so both
    once-only rules are visible in one place.

    Flushes, never commits: the survey lives or dies with the attendance change
    that caused it.
    """
    now = now or datetime.now()

    if not config.FEEDBACK_ENABLED:
        return {"opened": False, "reason": "feedback_disabled"}

    existing = db.scalar(
        select(FeedbackSurvey).where(FeedbackSurvey.booking_ref == booking.ref)
    )
    if existing is not None:
        return {"opened": False, "reason": "already_exists", "status": existing.status.value}

    lang = booking.customer.language_pref if booking.customer else Language.en
    queued = _has_survey_in(
        db, booking.customer_id, OPEN_STATUSES, exclude_ref=booking.ref
    )
    survey = FeedbackSurvey(
        booking_ref=booking.ref,
        customer_id=booking.customer_id,
        status=FeedbackStatus.queued if queued else FeedbackStatus.pending,
        step=0,
        lang=lang if isinstance(lang, Language) else Language.en,
        # Stamped EXPLICITLY rather than left to server_default=func.now(), which
        # is the DB's clock and therefore UTC — while `now` here, and every
        # comparison in expire_stale, is local. On a +04 deployment that skew
        # would make a brand-new survey look four hours stale the moment it was
        # written, and FEEDBACK_EXPIRY_HOURS would silently mean 20 hours, not 24.
        # Today the sweep only reads `sent`/`in_progress` rows (which always pass
        # through a path that sets updated_at explicitly), so this is not yet a
        # live bug — it is removing the trap rather than fixing a symptom.
        created_at=now,
        updated_at=now,
    )
    db.add(survey)
    db.flush()  # so `survey.booking` / `survey.customer` resolve below

    if not queued:
        _write_opening_notification(db, survey, now=now)

    audit.record(
        db,
        session=session_id,
        actor=Actor.admin,
        type=AuditType.feedback,
        payload={
            "ref": booking.ref,
            "customer_id": booking.customer_id,
            "event": "survey_queued" if queued else "survey_opened",
            "lang": survey.lang.value,
        },
        correlation_id=correlation_id,
    )
    db.flush()
    log.info(
        "feedback survey %s for %s", "queued" if queued else "opened", booking.ref
    )
    return {"opened": True, "queued": queued, "ref": booking.ref}


def render_opening_for_dispatch(survey: FeedbackSurvey, *, now: datetime) -> str:
    """Hook the dispatcher calls instead of notifications.render_body.

    Exists so the customer's NAME never has to be written into a Notification
    payload just to be spoken in the opening: the dispatcher already holds the
    Customer row at that point, so the text is composed from live objects. Keeps
    notifications.render_body pure over its payload, and keeps this table free of
    personal data it does not need (the same minimisation reasoning that omits
    doorstep_address from notification payloads).
    """
    return render_opening(survey, now=now)


def abort_opening(
    db: Session, *, ref: str, reason: str, now: datetime | None = None
) -> bool:
    """Close a survey whose opening could never be delivered.

    NOT COSMETIC. Without this, a survey whose opening was retired — no WhatsApp
    consent, the kind disabled in config, five failed send attempts — would sit at
    `pending` forever, and because `pending` blocks promotion it would silently
    prevent EVERY future survey for that customer. The customer would simply never
    be asked about any drive again, with nothing in the console explaining why.

    Recorded as `expired`, which is the honest fit among the terminal statuses: it
    means "closed without an answer", which is exactly what happened. The
    DISTINCTION between "we never asked" and "they never answered" is not lost —
    it lives on the notification row's `not_sent_reason` and in this function's
    audit event, both of which name the actual cause.
    """
    now = now or datetime.now()
    survey = db.scalar(select(FeedbackSurvey).where(FeedbackSurvey.booking_ref == ref))
    if survey is None or survey.status not in OPEN_STATUSES:
        return False
    survey.updated_at = now
    _close(
        db, survey, FeedbackStatus.expired, now=now,
        actor=Actor.system, event="opening_not_sent",
    )
    log.info("feedback survey closed unasked (%s): %s", reason, ref)
    return True


def mark_opening_sent(db: Session, *, ref: str, now: datetime | None = None) -> None:
    """Called by the dispatcher once the opening has actually gone out."""
    now = now or datetime.now()
    survey = db.scalar(select(FeedbackSurvey).where(FeedbackSurvey.booking_ref == ref))
    if survey is None or survey.status != FeedbackStatus.pending:
        return
    survey.status = FeedbackStatus.sent
    survey.sent_at = now
    survey.updated_at = now
    _audit(db, survey, actor=Actor.system, event="opening_sent")


# --------------------------------------------------------------------------- #
# Handling replies — the state machine proper
# --------------------------------------------------------------------------- #
def active_for_customer(db: Session, customer_id: int) -> FeedbackSurvey | None:
    """The one survey currently listening for this customer's replies.

    Only `sent`/`in_progress` qualify: a `pending` survey has not been asked yet,
    so a message arriving now is answering nothing.
    """
    return db.scalar(
        select(FeedbackSurvey)
        .where(
            FeedbackSurvey.customer_id == customer_id,
            FeedbackSurvey.status.in_(
                (FeedbackStatus.sent, FeedbackStatus.in_progress)
            ),
        )
        .order_by(FeedbackSurvey.sent_at.desc())
        .limit(1)
    )


def abandon_for_customer(
    db: Session, *, customer_id: int, now: datetime | None = None, reason: str = "booking_intent"
) -> bool:
    """Drop this customer's open survey because a real request arrived.

    Called by the inbound webhook when a mid-survey message parses as a booking
    intent. Partial answers are kept — they were freely given and are still true.
    """
    now = now or datetime.now()
    survey = active_for_customer(db, customer_id)
    if survey is None:
        return False
    survey.updated_at = now
    _close(
        db, survey, FeedbackStatus.abandoned, now=now,
        event="survey_abandoned",
    )
    log.info("feedback survey abandoned (%s): %s", reason, survey.booking_ref)
    return True


def _advance(db: Session, survey: FeedbackSurvey, *, now: datetime) -> dict:
    """Send whatever comes next, given `survey.step` has just been set."""
    if survey.step <= LAST_QUESTION:
        result = _send_question(survey, survey.step)
        survey.status = FeedbackStatus.in_progress
    elif survey.step == COMMENT_STEP:
        result = _send_text(survey, render_comment_prompt(survey))
        survey.status = FeedbackStatus.in_progress
    else:  # finished — thank them and close
        result = _send_text(survey, render_thanks(survey))
        _close(db, survey, FeedbackStatus.completed, now=now, event="survey_completed")
    survey.updated_at = now
    return result


def handle_reply(
    db: Session,
    *,
    customer: Customer,
    text: str | None = None,
    list_reply_id: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Feed one inbound WhatsApp message into this customer's open survey.

    Returns None when there is NO open survey, which is the signal for the
    inbound webhook to fall through to its normal handoff-link behaviour. A dict
    means the survey consumed the message and nothing else should act on it.

    Commits nothing — the caller owns the transaction.
    """
    now = now or datetime.now()
    survey = active_for_customer(db, customer.id)
    if survey is None:
        return None

    # ---- A tapped rating ---------------------------------------------------- #
    # Checked FIRST and independently of `step`: the id names its own question,
    # so a customer scrolling back to an earlier message and tapping it files the
    # answer correctly rather than against whatever is current.
    tapped = parse_list_reply(list_reply_id)
    if tapped is not None:
        step, score = tapped
        setattr(survey, QUESTION_COLUMNS[step], score)
        _audit(db, survey, actor=Actor.customer, event="answer", extra={
            "question": step, "score": score, "via": "tap",
        })
        # Only move forward. Re-answering an earlier question updates the score
        # without dragging the customer back through the questions they have
        # already done.
        if step >= survey.step:
            survey.step = step + 1
            _advance(db, survey, now=now)
        else:
            survey.updated_at = now
        return {"handled": True, "event": "answer", "ref": survey.booking_ref}

    body = (text or "").strip()

    # ---- The open comment --------------------------------------------------- #
    # Before the keyword checks, deliberately: at this step the customer has been
    # asked for free prose, so "I'd skip the panoramic roof" is a COMMENT, not a
    # command to abandon the survey.
    if survey.step == COMMENT_STEP:
        survey.comment = body
        survey.step = COMMENT_STEP + 1
        _audit(db, survey, actor=Actor.customer, event="comment", extra={
            "length": len(body),
        })
        _advance(db, survey, now=now)
        return {"handled": True, "event": "comment", "ref": survey.booking_ref}

    # ---- "skip" -------------------------------------------------------------- #
    if _matches(body, _SKIP_WORDS):
        _send_text(survey, render_skip_ack(survey))
        survey.updated_at = now
        _close(db, survey, FeedbackStatus.skipped, now=now, event="survey_skipped")
        # NOTE: whatsapp_consent is deliberately NOT touched. Declining one
        # survey must never cost a customer their booking reminders — they asked
        # to stop answering questions, not to stop being told when their next
        # test drive is.
        return {"handled": True, "event": "skipped", "ref": survey.booking_ref}

    # ---- "later" ------------------------------------------------------------- #
    if _matches(body, _LATER_WORDS):
        survey.later_count += 1
        survey.updated_at = now
        if survey.later_count > 1:
            # A second "later" is a polite no. Chasing a third time is nagging.
            _send_text(survey, render_skip_ack(survey))
            _close(db, survey, FeedbackStatus.skipped, now=now, event="survey_later_declined")
            return {"handled": True, "event": "skipped", "ref": survey.booking_ref}
        _send_text(survey, render_later_ack(survey))
        # Re-queue the opening on the dispatcher rails, so the resend inherits
        # the same consent gate as the original — consent can be withdrawn in
        # the intervening hours, and a deferred message must respect that.
        survey.status = FeedbackStatus.pending
        survey.sent_at = None
        _write_opening_notification(
            db, survey, now=now + timedelta(hours=config.FEEDBACK_LATER_HOURS)
        )
        _audit(db, survey, actor=Actor.customer, event="survey_deferred", extra={
            "later_count": survey.later_count,
        })
        return {"handled": True, "event": "later", "ref": survey.booking_ref}

    # ---- A typed score ------------------------------------------------------- #
    if survey.step in QUESTION_COLUMNS:
        typed = parse_typed_score(body)
        if typed is not None:
            setattr(survey, QUESTION_COLUMNS[survey.step], typed)
            _audit(db, survey, actor=Actor.customer, event="answer", extra={
                "question": survey.step, "score": typed, "via": "typed",
            })
            survey.step += 1
            _advance(db, survey, now=now)
            return {"handled": True, "event": "answer", "ref": survey.booking_ref}
        # Anything else mid-question: re-send the same question once. The list is
        # re-sent rather than a scolding line, because the likeliest cause is
        # that they typed instead of tapping and simply need the list again.
        _send_question(survey, survey.step)
        survey.updated_at = now
        return {"handled": True, "event": "reprompt", "ref": survey.booking_ref}

    # ---- Agreement to start (step 0) ----------------------------------------- #
    # ANY reply that is not later/skip/a booking intent counts as yes. Insisting
    # on the word "yes" would strand a customer who replied "sure why not".
    survey.step = 1
    _audit(db, survey, actor=Actor.customer, event="survey_started")
    _advance(db, survey, now=now)
    return {"handled": True, "event": "started", "ref": survey.booking_ref}


# --------------------------------------------------------------------------- #
# Expiry sweep — run from the dispatcher tick
# --------------------------------------------------------------------------- #
def expire_stale(db: Session, *, now: datetime | None = None) -> int:
    """Retire surveys idle for FEEDBACK_EXPIRY_HOURS. Partial answers kept.

    Measured from `updated_at`, so a customer answering slowly gets a fresh
    window on every reply rather than being cut off mid-survey because the
    opening went out yesterday. No reminder is sent before expiring: chasing
    someone for a satisfaction score is the kind of pressure rule 14 rules out.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(hours=config.FEEDBACK_EXPIRY_HOURS)
    stale = list(
        db.scalars(
            select(FeedbackSurvey).where(
                FeedbackSurvey.status.in_(
                    (FeedbackStatus.sent, FeedbackStatus.in_progress)
                ),
                FeedbackSurvey.updated_at <= cutoff,
            )
        ).all()
    )
    for survey in stale:
        _close(
            db, survey, FeedbackStatus.expired, now=now,
            actor=Actor.system, event="survey_expired",
        )
    if stale:
        log.info("feedback: expired %s stale survey(s)", len(stale))
    return len(stale)


# --------------------------------------------------------------------------- #
# Read side — the console
# --------------------------------------------------------------------------- #
def survey_dict(survey: FeedbackSurvey | None) -> dict | None:
    """The console's view of one survey. Scores stay NULL when unanswered.

    A zero would be a score nobody gave and would drag every average down with
    it; the UI renders null as an em-dash instead. `average` is over the answered
    questions only, and is None when there are none.
    """
    if survey is None:
        return None
    scores = {
        "overall": survey.q1_overall,
        "ease": survey.q2_ease,
        "vehicle": survey.q3_vehicle,
        "team": survey.q4_team,
    }
    answered = [v for v in scores.values() if v is not None]
    return {
        "booking_ref": survey.booking_ref,
        "status": survey.status.value,
        "lang": survey.lang.value,
        "step": survey.step,
        "scores": scores,
        "answered": len(answered),
        "average": round(sum(answered) / len(answered), 1) if answered else None,
        "comment": survey.comment,
        "created_at": survey.created_at.isoformat() if survey.created_at else None,
        "sent_at": survey.sent_at.isoformat() if survey.sent_at else None,
        "closed_at": survey.closed_at.isoformat() if survey.closed_at else None,
        "updated_at": survey.updated_at.isoformat() if survey.updated_at else None,
    }


def for_ref(db: Session, ref: str) -> FeedbackSurvey | None:
    return db.scalar(select(FeedbackSurvey).where(FeedbackSurvey.booking_ref == ref))


def for_refs(db: Session, refs: list[str]) -> dict[str, FeedbackSurvey]:
    """Bulk lookup, so the Bookings list does not issue one query per row."""
    if not refs:
        return {}
    rows = db.scalars(
        select(FeedbackSurvey).where(FeedbackSurvey.booking_ref.in_(refs))
    ).all()
    return {r.booking_ref: r for r in rows}


__all__ = [
    "OPEN_STATUSES",
    "open_for_booking",
    "mark_opening_sent",
    "render_opening_for_dispatch",
    "handle_reply",
    "active_for_customer",
    "abandon_for_customer",
    "expire_stale",
    "survey_dict",
    "for_ref",
    "for_refs",
    "parse_list_reply",
    "parse_typed_score",
]
