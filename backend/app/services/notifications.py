"""Notification service — WhatsApp-simulator rows (FR-CF).

NOT A TOOL, AND THAT IS THE POINT
---------------------------------
Notifications are a SIDE EFFECT of the booking service, never something the
agent decides to do. A `send_confirmation` tool would be a write the model has
to remember: it could be forgotten (a booking with no confirmation), double-fired
(two messages), or called before the booking exists (a confirmation for a
reference that was never created). A side effect that must ALWAYS happen cannot
depend on the model remembering to trigger it — so booking.create() calls this
directly, and the agent has no say in the matter.

This phase writes ROWS ONLY. There is no scheduler here: the accelerated demo
clock (AD-F, in-process APScheduler) is a later phase and will read `send_at`.

NO CUSTOMER-FACING COPY LIVES IN THIS MODULE
--------------------------------------------
`body` is left "" and the structured facts go to `payload`. Writing "Your test
drive is confirmed for..." here would put display text in the data layer and,
worse, hard-code English into a row that may be read by an Arabic customer
(LOC-01) — Arabic that is a machine translation of English source copy is
exactly what FR-CX-02/03 forbids. Templates arrive with Layla's Arabic in a
later phase and render `body` from `payload`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Actor,
    AuditType,
    Booking,
    Language,
    LocationType,
    Notification,
    NotificationKind,
    NotificationStatus,
)
from app import config
from app.services import audit

# Reminder lead times (FR-CF). T-24h and T-2h before the slot starts.
REMINDER_LEAD_TIMES: tuple[tuple[NotificationKind, timedelta], ...] = (
    (NotificationKind.t24, timedelta(hours=24)),
    (NotificationKind.t2, timedelta(hours=2)),
)


# --------------------------------------------------------------------------- #
# Body rendering — customer-facing copy, per language, FROM the payload facts
# --------------------------------------------------------------------------- #
# Rendered at DISPATCH (services/dispatcher), never at schedule time: the row is
# stored language-neutral (`body=""`, facts in `payload`, LOC-01) and the text is
# composed only when the message actually goes out. Persona "Aisha" (rule 1),
# warm Gulf register (rule 14), plain chat text — no markdown/emoji (rule 17).
_AR_DAYS = {
    0: "الإثنين", 1: "الثلاثاء", 2: "الأربعاء", 3: "الخميس",
    4: "الجمعة", 5: "السبت", 6: "الأحد",
}
_AR_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل", 5: "مايو", 6: "يونيو",
    7: "يوليو", 8: "أغسطس", 9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}


def _fmt_time(dt: datetime, lang: Language) -> str:
    hour12 = dt.strftime("%I").lstrip("0") or "12"
    minute = f":{dt.minute:02d}" if dt.minute else ":00"
    if lang == Language.ar:
        ampm = "ص" if dt.hour < 12 else "م"
        return f"{hour12}{minute} {ampm}"
    return f"{hour12}{minute} {dt.strftime('%p')}"


def _fmt_when(dt: datetime, lang: Language) -> str:
    """Day name + date + 12-hour time (LOC-09), in the message's language."""
    if lang == Language.ar:
        return (
            f"{_AR_DAYS[dt.weekday()]} {dt.day} {_AR_MONTHS[dt.month]} "
            f"الساعة {_fmt_time(dt, lang)}"
        )
    return f"{dt.strftime('%A, %-d %B')} at {_fmt_time(dt, lang)}"


def _where(payload: dict, lang: Language) -> str:
    if payload.get("location_type") == LocationType.doorstep.value:
        area = payload.get("service_area") or ("دبي" if lang == Language.ar else "Dubai")
        return f"عنوانك في {area}" if lang == Language.ar else f"your address in {area}"
    name = payload.get("showroom_name") or (
        "معرض إيليت" if lang == Language.ar else "our showroom"
    )
    return f"معرض {name}" if lang == Language.ar else name


def render_body(notification) -> str:
    """The customer-facing WhatsApp text for one notification row.

    Pure over the row's `payload` + `kind` + `lang` — no DB access, no booking
    join (the payload already carries vehicle name, slot start, location, ref).
    Falls back to English on any unexpected kind so a dispatch never sends empty.
    """
    payload = notification.payload or {}
    lang = notification.lang if isinstance(notification.lang, Language) else Language.en
    kind = notification.kind
    ar = lang == Language.ar

    vehicle = payload.get("vehicle_name") or ("سيارتك" if ar else "your vehicle")
    ref = payload.get("booking_ref") or notification.ref
    where = _where(payload, lang)
    start_iso = payload.get("slot_start_iso")
    dt = datetime.fromisoformat(start_iso) if start_iso else None
    when = _fmt_when(dt, lang) if dt else ""
    time_only = _fmt_time(dt, lang) if dt else ""

    # C3: a brochure carries no slot, no place and no reference, so it is
    # answered before the booking-shaped locals above are consulted. The link is
    # relative in the payload and absolutised here — the row should not store a
    # hostname that changes with the deployment.
    if kind == NotificationKind.brochure:
        doc = payload.get("document") or ""
        url = f"{config.PUBLIC_WEB_BASE_URL.rstrip('/')}{doc}" if doc else ""
        price = payload.get("price") or ""
        if ar:
            return (
                f"مرحبًا، أنا عايشة من مجموعة إيليت. هذه نبذة عن {vehicle}"
                + (f" — {price}" if price else "")
                + (f"\n{url}" if url else "")
                + "\nسعيدة بحجز تجربة قيادة لك في أي وقت."
            )
        return (
            f"Hi, it's Aisha from Elite Group. Here's the summary for the {vehicle}"
            + (f" — {price}" if price else "")
            + (f"\n{url}" if url else "")
            + "\nHappy to book you a test drive whenever you're ready."
        )

    if ar:
        head = "مرحبًا، أنا عايشة من مجموعة إيليت."
        if kind == NotificationKind.t2:
            return (
                f"{head} تذكير بسيط — تجربة القيادة لسيارة {vehicle} بعد حوالي "
                f"ساعتين، الساعة {time_only} في {where}. نراك قريبًا! رقم الحجز {ref}."
            )
        if kind == NotificationKind.t24:
            return (
                f"{head} تذكير بموعد تجربة القيادة لسيارة {vehicle} غدًا، {when} "
                f"في {where}. لا تنسَ إحضار رخصة القيادة. رقم الحجز {ref}."
            )
        if kind == NotificationKind.reschedule:
            return (
                f"{head} تم تحديث موعد تجربة القيادة لسيارة {vehicle} إلى {when} "
                f"في {where}. رقم الحجز {ref}، ونتطلع للقائك."
            )
        if kind == NotificationKind.cancellation:
            return (
                f"{head} تم إلغاء حجز تجربة القيادة لسيارة {vehicle} ({ref}). "
                f"يسعدنا استقبالك في وقت آخر متى ما رغبت."
            )
        if kind == NotificationKind.noshow:
            return (
                f"{head} كان لدينا موعد لتجربة قيادة سيارة {vehicle} يوم {when}، "
                f"ولم نتشرّف بلقائك. نتمنى أن يكون كل شيء بخير. إذا ما زلت "
                f"ترغب في التجربة، ردّ على هذه الرسالة وسنرتب لك موعدًا "
                f"يناسبك أكثر. رقم الحجز {ref}."
            )
        # confirmation (default)
        return (
            f"{head} تم تأكيد حجز تجربة القيادة لسيارة {vehicle} يوم {when} في "
            f"{where}. لا تنسَ إحضار رخصة القيادة، ويسعدنا لقاؤك هناك! رقم الحجز {ref}."
        )

    head = "Hi, this is Aisha from Elite Group."
    if kind == NotificationKind.t2:
        return (
            f"{head} A quick reminder — your test drive of the {vehicle} is in "
            f"about 2 hours, at {time_only} at {where}. See you soon! Ref {ref}."
        )
    if kind == NotificationKind.t24:
        return (
            f"{head} A reminder that your test drive of the {vehicle} is tomorrow, "
            f"{when} at {where}. Please remember your driving licence. Ref {ref}."
        )
    if kind == NotificationKind.reschedule:
        return (
            f"{head} Your test drive of the {vehicle} has been moved to {when} at "
            f"{where}. Your reference is still {ref} — we look forward to seeing you."
        )
    if kind == NotificationKind.cancellation:
        return (
            f"{head} Your test drive of the {vehicle} ({ref}) has been cancelled. "
            f"We'd be happy to welcome you another time whenever suits you."
        )
    if kind == NotificationKind.noshow:
        # Rule 14: an offer, never a reproach. It asks after the customer first
        # and mentions the drive second, because the likeliest reason someone
        # missed an appointment is that something went wrong in their day.
        return (
            f"{head} We had you down for the {vehicle} on {when} and we're sorry we "
            f"missed you. Is everything alright? If you'd still like the drive, "
            f"just reply here and we'll find a time that suits you better. "
            f"Ref {ref}."
        )
    # confirmation (default)
    return (
        f"{head} Your test drive of the {vehicle} is booked for {when} at "
        f"{where}. Please remember to bring your driving licence — we're happy to "
        f"see you there! Your reference is {ref}."
    )


def _payload_for(booking: Booking) -> dict:
    """The language-neutral FACTS a template will render from.

    Deliberately omits doorstep_address even for a doorstep booking. The address
    already lives on the booking; copying it here would create a second place to
    hold — and, on "forget me", a second place to erase — a piece of personal
    data, for no gain (PDPL data minimisation, FR-MEM). A template that needs it
    joins on `ref`.
    """
    slot = booking.slot
    payload = {
        "booking_ref": booking.ref,
        "customer_id": booking.customer_id,
        "vehicle_id": booking.vehicle_id,
        "vehicle_name": booking.vehicle.name if booking.vehicle else None,
        "slot_start_iso": slot.start.isoformat() if slot else None,
        "slot_end_iso": slot.end.isoformat() if slot else None,
        "location_type": booking.location.value,
    }
    if booking.location == LocationType.doorstep:
        payload["service_area"] = slot.service_area if slot else None
    else:
        payload["showroom_id"] = slot.showroom_id if slot else None
        payload["showroom_name"] = slot.showroom.name if slot and slot.showroom else None
    return payload


def schedule_for_booking(
    db: Session,
    *,
    session_id: str,
    booking: Booking,
    lang: Language | str | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
    primary_kind: NotificationKind = NotificationKind.confirmation,
) -> dict:
    """Write the primary notice + T-24h/T-2h reminder rows for a booking.

    `lang` defaults to the customer's stored language_pref, but is a parameter
    because that default is only an APPROXIMATION: FR-CF-01 says the confirmation
    goes out in the language of the CONVERSATION, which may differ from the
    stored preference (a customer whose pref is Arabic may be chatting in
    English). The agent loop passes the live language through this parameter —
    the signature is here now so that lands without a change to this contract.

    `primary_kind` (FR-CF-01 / FR-RS-04) is the "receipt" notice sent NOW. It is
    `confirmation` for a new booking and `reschedule` for a moved one — a
    reschedule's receipt says "your test drive has moved", not "confirmed", but
    it rides the SAME rails (fresh reminders re-anchored to the new slot). The
    T-24h/T-2h reminder kinds never change; only the receipt does. Default keeps
    create_booking's behaviour identical.

    Flushes but does not commit: these rows must live or die with the booking
    that caused them.
    """
    now = now or datetime.now()

    if lang is None:
        lang = booking.customer.language_pref if booking.customer else Language.en
    if isinstance(lang, str):
        try:
            lang = Language(lang)
        except ValueError:
            lang = Language.en

    payload = _payload_for(booking)
    scheduled: list[dict] = []
    skipped: list[dict] = []

    def _add(kind: NotificationKind, send_at: datetime) -> None:
        row = Notification(
            ref=booking.ref,
            kind=kind,
            lang=lang,
            body="",  # rendered later, by the template phase — see the docstring
            payload={**payload, "kind": kind.value},
            status=NotificationStatus.scheduled,
            send_at=send_at,
        )
        db.add(row)
        scheduled.append({"kind": kind.value, "send_at": send_at.isoformat()})
        audit.record(
            db,
            session=session_id,
            actor=Actor.system,
            type=AuditType.notification,
            payload={
                "ref": booking.ref,
                "kind": kind.value,
                "lang": lang.value,
                "status": NotificationStatus.scheduled.value,
                "send_at": send_at.isoformat(),
            },
            correlation_id=correlation_id,
        )

    # The primary notice goes now, always (FR-CF-01) — it is the receipt for a
    # booking (or move) that just happened, so it can never be "overdue".
    _add(primary_kind, now)

    start = booking.slot.start if booking.slot else None
    for kind, lead in REMINDER_LEAD_TIMES:
        if start is None:  # pragma: no cover — a booking always has a slot
            continue
        send_at = start - lead
        if send_at <= now:
            # Someone booked a slot less than `lead` away. Writing the row with a
            # send_at already in the past would hand the future scheduler a
            # reminder that fires instantly, reminding a customer about a drive
            # they are already on their way to. Skip it, and REPORT the skip so
            # the absence is visible rather than looking like a bug.
            #
            # FLAG FOR THE SCHEDULER PHASE (AD-F): revisit this comparison under
            # the accelerated demo clock. With a time-scale factor, T-24h/T-2h
            # fire inside a ~60-minute session, so "in the past" in wall-clock
            # time is not necessarily "in the past" in demo time. The demo books
            # days out, so both reminders are future and this edge only bites
            # same-day bookings — but it will need a decision then, not now.
            skipped.append(
                {
                    "kind": kind.value,
                    "reason": "send_at_in_the_past",
                    "send_at": send_at.isoformat(),
                }
            )
            continue
        _add(kind, send_at)

    db.flush()
    return {"scheduled": scheduled, "skipped": skipped}


# --------------------------------------------------------------------------- #
# supersede — reschedule/cancel retire the pending reminders (FR-RS-04, FR-CN-03)
# --------------------------------------------------------------------------- #
def supersede_for_booking(
    db: Session,
    *,
    session_id: str,
    ref: str,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Retire every still-PENDING notification for `ref`. Returns how many.

    A reschedule and a cancel both invalidate the reminders a booking scheduled:
    after a move, a T-2h reminder anchored to the OLD slot would tell the
    customer to come at a time they no longer have (FR-RS-04); after a cancel,
    every reminder is for a visit that will not happen (FR-CN-03). Both cases are
    the same operation — cancel the scheduled rows — so it lives here once.

    Scoped to status == scheduled, ON PURPOSE. A row already `sent` is history
    (the customer really did get that message) and must not be rewritten; a row
    already `cancelled` is idempotently left alone. This is what makes S-09
    testable: after a reschedule, the ONLY `scheduled` rows for the ref are the
    new slot's, because the old ones are now `cancelled`.

    Each retirement is audited as a `notification` event (status cancelled),
    sharing the caller's correlation_id so the console groups it with the
    reschedule/cancel intent (behaviour rule 15). Flushes, never commits.
    """
    now = now or datetime.now()

    pending = list(
        db.scalars(
            select(Notification).where(
                Notification.ref == ref,
                Notification.status == NotificationStatus.scheduled,
            )
        ).all()
    )
    superseded: list[dict] = []
    for row in pending:
        row.status = NotificationStatus.cancelled
        superseded.append({"kind": row.kind.value, "send_at": row.send_at.isoformat()})
        audit.record(
            db,
            session=session_id,
            actor=Actor.system,
            type=AuditType.notification,
            payload={
                "ref": ref,
                "kind": row.kind.value,
                "lang": row.lang.value,
                "status": NotificationStatus.cancelled.value,
                "at": now.isoformat(),
            },
            correlation_id=correlation_id,
        )
    if pending:
        db.flush()
    return {"superseded": superseded, "count": len(superseded)}


# --------------------------------------------------------------------------- #
# cancellation notice — "confirm in writing" that the cancel happened (FR-CN-03)
# --------------------------------------------------------------------------- #
def notify_cancellation(
    db: Session,
    *,
    session_id: str,
    booking: Booking,
    lang: Language | str | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Write ONE `cancellation` notice, sent now. No reminders — the visit is off.

    FR-CN-03 requires the cancellation be confirmed in writing: silently
    suppressing the reminders would leave the customer with no record the cancel
    actually took. Like every other row here, `body` stays "" (the template phase
    renders it per language, LOC-01) and the facts live in `payload`.

    Flushes but does not commit — this row lives or dies with the cancellation.
    """
    now = now or datetime.now()

    if lang is None:
        lang = booking.customer.language_pref if booking.customer else Language.en
    if isinstance(lang, str):
        try:
            lang = Language(lang)
        except ValueError:
            lang = Language.en

    payload = {**_payload_for(booking), "kind": NotificationKind.cancellation.value}
    row = Notification(
        ref=booking.ref,
        kind=NotificationKind.cancellation,
        lang=lang,
        body="",
        payload=payload,
        status=NotificationStatus.scheduled,
        send_at=now,
    )
    db.add(row)
    audit.record(
        db,
        session=session_id,
        actor=Actor.system,
        type=AuditType.notification,
        payload={
            "ref": booking.ref,
            "kind": NotificationKind.cancellation.value,
            "lang": lang.value,
            "status": NotificationStatus.scheduled.value,
            "send_at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {"sent": [{"kind": NotificationKind.cancellation.value, "send_at": now.isoformat()}]}


def notify_noshow(
    db: Session,
    *,
    session_id: str,
    booking: Booking,
    lang: Language | str | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Write ONE `noshow` notice, sent now (2026-07-23).

    Called only by booking.set_attendance, and only on the transition INTO
    `noshow` — the once-only guard lives there (Booking.noshow_notified_at), not
    here, because this function's job is to write a row and it must stay usable
    without re-deriving that decision.

    Like every other row: `body` stays "" and the facts go to `payload`; the
    text is rendered per language at DISPATCH (LOC-01). The consent gate is the
    dispatcher's, as it is for every kind — a customer who never agreed to
    WhatsApp is not messaged about a missed drive either.

    Flushes but does not commit — this row lives or dies with the attendance
    change that caused it.
    """
    now = now or datetime.now()

    if lang is None:
        lang = booking.customer.language_pref if booking.customer else Language.en
    if isinstance(lang, str):
        try:
            lang = Language(lang)
        except ValueError:
            lang = Language.en

    payload = {**_payload_for(booking), "kind": NotificationKind.noshow.value}
    row = Notification(
        ref=booking.ref,
        kind=NotificationKind.noshow,
        lang=lang,
        body="",
        payload=payload,
        status=NotificationStatus.scheduled,
        send_at=now,
    )
    db.add(row)
    audit.record(
        db,
        session=session_id,
        actor=Actor.system,
        type=AuditType.notification,
        payload={
            "ref": booking.ref,
            "kind": NotificationKind.noshow.value,
            "lang": lang.value,
            "status": NotificationStatus.scheduled.value,
            "send_at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {"sent": [{"kind": NotificationKind.noshow.value, "send_at": now.isoformat()}]}
