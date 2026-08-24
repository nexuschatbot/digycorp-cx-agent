"""WhatsApp INBOUND — identify, mint a handoff token, reply with a link (2026-07-22).

The service half of the reply-with-a-link handoff (the routes are api/whatsapp.py).
This is the first inbound path the system has ever had, and it is deliberately
NOT a second Aisha: it identifies the sender, mints a one-time link, and replies.
The actual conversation continues on the web (SSE, AD-B) — the same shape as STT
running before the agent for voice notes, so AD-C (no WebSockets) is untouched.

Division of labour, matching the rest of the codebase (AD-I):
  - THIS module owns identity lookup, intent parsing, token mint/redeem, and the
    reply/greeting COPY. All DB access lives here.
  - api/whatsapp.py is the thin HTTP shell: Meta's verification handshake,
    signature verification, payload parsing, and — on redeem — binding the fresh
    web session to the customer in the agent checkpoint (the same synthetic
    tool-pair mechanism api/intents.py uses).

GROUNDING (rule 2) is preserved. The reply and the greeting directive name the
customer and, at most, their booking REFERENCE — never a slot time or a price.
Every fact the web agent then states still comes from get_booking. We resolve the
booking ref server-side only so the agent can look it up; we never quote its
contents into WhatsApp.

NO-OP WHEN DISABLED. With WHATSAPP_INBOUND_ENABLED off, handle_inbound_text does
nothing and reports it — so the webhook can be wired in Meta and left dormant, and
tests exercise the logic without opening a live channel. The actual send goes
through services/whatsapp.send_text, which is itself a no-op unless WHATSAPP_ENABLED
is on, so nothing leaves the box in dev or tests.

⚠️ D1 PDPL CHECKLIST — a NEW entry, not a footnote to the outbound one. Inbound
customer data (their number, their message) now enters the system, and the link
we send is identity-bearing. Mitigated by single-use + short-TTL tokens
(HANDOFF_TOKEN_TTL_MINUTES), by never quoting booking contents into the WhatsApp
reply, and by resolving identity from the token server-side on redeem (never from
the client). Sandbox / fictitious-data demo only.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import Actor, AuditType, Customer, HandoffToken, Language
from app.services import audit
from app.services import booking as booking_service
from app.services import customers as customers_service
from app.services import feedback as feedback_service
from app.services import whatsapp as whatsapp_service
from app.lang import detect as detect_lang

log = logging.getLogger("app.services.whatsapp_inbound")


# --------------------------------------------------------------------------- #
# Intent parsing — keyword only, deliberately shallow
# --------------------------------------------------------------------------- #
# The WhatsApp side does not need the model to know what the customer wants: it
# only decides which link copy to send and how to seed the greeting. A wrong
# guess costs nothing — the web agent still asks and still grounds every fact —
# so a keyword scan (EN + common Gulf Arabic) is proportionate. Anything we do
# not recognise is `general`, and the greeting simply asks how it can help.
_KEYWORDS = {
    "reschedule": (
        "reschedule", "change", "move", "postpone", "another time", "different time",
        "غيّر", "غير", "تغيير", "أجّل", "اجل", "تأجيل", "موعد ثاني", "وقت ثاني", "تعديل",
    ),
    "cancel": (
        "cancel", "cancelled", "delete", "remove", "call off",
        "إلغاء", "الغاء", "ألغي", "الغي", "احذف", "إلغ",
    ),
    "book": (
        "book", "test drive", "test-drive", "reserve", "appointment",
        "حجز", "احجز", "أحجز", "تجربة قيادة", "موعد",
    ),
}
# Order matters: a "change my booking" mentions "booking" too, but the reschedule
# reading is the specific one. Cancel and reschedule are checked before book.
_INTENT_ORDER = ("cancel", "reschedule", "book")


def parse_intent(text: str) -> str:
    """Best-effort intent from the inbound message. Never the source of truth —
    it shapes copy and the greeting seed, nothing that commits state."""
    low = (text or "").strip().lower()
    for intent in _INTENT_ORDER:
        if any(kw in low for kw in _KEYWORDS[intent]):
            return intent
    return "general"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def _lookup_customer(db: Session, from_msisdn: str) -> Customer | None:
    """Match the sender's WhatsApp number (digits, no '+') to a Customer, or None.

    Meta hands the number as bare digits with the country code; normalise_mobile
    wants a leading '+', so we add it. With STRICT_UAE_MOBILE off a +92 test
    number resolves; with it on, only UAE numbers do — and an unmatched number is
    an honest None (we still hand off, binding no one), never an error.
    """
    normalised = customers_service.normalise_mobile("+" + (from_msisdn or "").lstrip("+"))
    if "error" in normalised:
        return None
    return db.scalar(select(Customer).where(Customer.mobile == normalised["mobile"]))


def _active_ref(db: Session, customer: Customer) -> str | None:
    """The customer's single active booking ref, or None if zero or more than one.

    We only pre-point the greeting at a booking when there is exactly one to point
    at; with none, or with two live bookings, the web agent asks which — the same
    disambiguation booking._resolve_target already does. Read-only, never raises.
    """
    result = booking_service.find(db, mobile=customer.mobile, active_only=True)
    if not isinstance(result, dict):
        return None
    rows = result.get("bookings") or []
    if len(rows) == 1 and isinstance(rows[0], dict):
        ref = rows[0].get("booking_ref")
        return ref if isinstance(ref, str) else None
    return None


# --------------------------------------------------------------------------- #
# Reply + greeting copy (bilingual, chat-register — rule 17: no markdown/emoji)
# --------------------------------------------------------------------------- #
def _display_name(customer: Customer | None, lang: str) -> str:
    """A name to greet with in the given language, or '' if we have none."""
    if customer is None:
        return ""
    name = (customer.name_ar if lang == "ar" else customer.name_en) or customer.name_en or customer.name_ar
    return name or ""


def compose_reply(*, link: str, intent: str, lang: str, name: str) -> str:
    """The WhatsApp text we send back: a warm line + the link. Names the customer
    when we know them; never quotes any booking detail (grounding stays on web)."""
    hi = f"Ahlan {name}".strip() if name else "Ahlan"
    hi_ar = f"أهلاً {name}".strip() if name else "أهلاً"
    if lang == "ar":
        lead = {
            "reschedule": "بكل سرور، نقدر نعدّل موعد تجربة القيادة من هنا ونختار وقتاً يناسبك",
            "cancel": "بكل سرور، نكمل طلبك من هنا",
            "book": "يسعدنا نرتّب لك تجربة القيادة من هنا",
            "general": "يسعدني أساعدك، تفضّل نكمل من هنا",
        }[intent]
        return f"{hi_ar}، أنا عائشة من إيليت. {lead}: {link}"
    lead = {
        "reschedule": "of course — let's change your test-drive booking here and pick a new time together",
        "cancel": "of course — let's take care of that here",
        "book": "I'd be glad to arrange a test drive with you here",
        "general": "I'd be glad to help — let's continue here",
    }[intent]
    return f"{hi}, this is Aisha from Elite. {lead}: {link}"


def compose_greeting_directive(
    *, intent: str, lang: str, name: str, title: str | None, booking_ref: str | None
) -> str:
    """The server-authored kickoff the web sends to /api/chat so Aisha speaks first.

    A bracketed system note in the shape chat.py's _VOICE_FAILED_NOTE uses — the
    model reads it as context and does not read it out. It carries only what the
    agent may safely assume (identity, intent, a booking ref to look up); every
    concrete fact still comes from get_booking. Authored HERE, server-side, from
    resolved data — the frontend only relays it, gaining no privilege it did not
    already have by being able to type a message.
    """
    who = " ".join(p for p in (title or "", name or "") if p).strip() or "the customer"
    language = "Arabic" if lang == "ar" else "English"
    task = {
        "reschedule": "reschedule their test-drive booking",
        "cancel": "cancel their test-drive booking",
        "book": "book a test drive",
        "general": "continue an earlier request",
    }[intent]
    ref_clause = ""
    if booking_ref:
        ref_clause = (
            f' Their booking reference is {booking_ref}; call get_booking with ref '
            f'"{booking_ref}" to confirm the current details BEFORE offering any change '
            f"or new time — never state a date, time or price you have not looked up."
        )
    elif intent in ("reschedule", "cancel"):
        ref_clause = (
            " We could not find a single active booking for them, so ask warmly for "
            "their TD reference or mobile and look it up before doing anything."
        )
    identified = (
        f"They are already identified as {who}, with their mobile on file, so do NOT "
        "ask for their number or make them repeat who they are."
        if name or title
        else "They are not yet identified, so greet them and ask how you can help."
    )
    return (
        "[System note — context for you, do not read this out or quote it. "
        "The customer has just opened this chat from a WhatsApp message they sent us. "
        f"{identified} On WhatsApp they asked to {task}.{ref_clause} "
        "Greet them warmly and by name as Aisha from Elite, let them know you can help, "
        f"and carry on from there. Write your reply in {language}.]"
    )


# --------------------------------------------------------------------------- #
# Token mint / redeem
# --------------------------------------------------------------------------- #
def _existing_for_wamid(db: Session, wamid: str | None) -> HandoffToken | None:
    if not wamid:
        return None
    return db.scalar(select(HandoffToken).where(HandoffToken.wamid == wamid))


def handle_inbound_text(
    db: Session,
    *,
    from_msisdn: str,
    text: str,
    wamid: str | None,
    list_reply_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Process one inbound WhatsApp message: survey reply, or identify -> mint
    token -> reply with a handoff link.

    Idempotent per `wamid` (Meta retries until it gets a 200): a repeat delivery
    finds the token already minted and re-sends nothing. Returns a small result
    dict for logging/tests; never raises — a webhook that 500s makes Meta retry
    forever.
    """
    if not config.WHATSAPP_INBOUND_ENABLED:
        return {"ok": False, "skipped": True, "reason": "inbound_disabled"}

    now = now or datetime.now()

    # Idempotency: one token per Meta message id, no separate dedup store.
    existing = _existing_for_wamid(db, wamid)
    if existing is not None:
        return {"ok": True, "duplicate": True, "token": existing.token}

    customer = _lookup_customer(db, from_msisdn)
    intent = parse_intent(text)

    # ---- Feedback survey (2026-07-26) ---------------------------------------- #
    # Checked BEFORE the token mint, and A REAL REQUEST ALWAYS WINS. If the
    # message parses as a booking intent, the survey is abandoned and we fall
    # through to the ordinary handoff below: someone trying to cancel a booking
    # must never be held inside a satisfaction questionnaire, which is the single
    # worst thing this feature could do to a customer.
    #
    # A tapped rating (`list_reply_id`) is exempt from that check on purpose —
    # its text is a row title we wrote ourselves ("5 — Excellent"), so running it
    # through the keyword matcher could only ever produce a false positive.
    #
    # No token is minted for a consumed survey reply, so `wamid` idempotency does
    # not cover it. Meta retrying a delivery could therefore double-apply an
    # answer — harmless by construction: setting q3 to 4 twice is the same as
    # setting it once, and the step only ever moves forward.
    if customer is not None and config.FEEDBACK_ENABLED:
        if list_reply_id is None and intent in ("cancel", "reschedule", "book"):
            if feedback_service.abandon_for_customer(db, customer_id=customer.id, now=now):
                db.commit()
                log.info("feedback survey abandoned for a %s request from %s", intent, from_msisdn)
        else:
            consumed = feedback_service.handle_reply(
                db, customer=customer, text=text, list_reply_id=list_reply_id, now=now
            )
            if consumed is not None:
                db.commit()
                log.info(
                    "whatsapp inbound consumed by feedback survey: from=%s event=%s ref=%s",
                    from_msisdn, consumed.get("event"), consumed.get("ref"),
                )
                return {"ok": True, "feedback": consumed}
    lang = detect_lang(text) if text else (customer.language_pref.value if customer else "en")

    booking_ref = None
    if customer is not None and intent in ("reschedule", "cancel", "general"):
        booking_ref = _active_ref(db, customer)

    token = secrets.token_urlsafe(24)
    row = HandoffToken(
        token=token,
        wamid=wamid,
        customer_id=customer.id if customer else None,
        booking_ref=booking_ref,
        intent=intent,
        lang=Language(lang),
        expires_at=now + timedelta(minutes=config.HANDOFF_TOKEN_TTL_MINUTES),
    )
    db.add(row)

    # Audit the inbound + the mint as ONE customer-initiated event. actor=customer:
    # a person messaged us; we did not decide anything (rule 15/16). Keyed on the
    # would-be web session so the trail is findable, but that session does not
    # exist until redeem — so we key on the token, which the redeem row echoes.
    audit.record(
        db,
        session=f"wa:{token}",
        actor=Actor.customer,
        type=AuditType.intent,
        payload={
            "via": "whatsapp_inbound",
            "from": from_msisdn,
            "intent": intent,
            "lang": lang,
            "identified": bool(customer),
            "booking_ref": booking_ref,
            "wamid": wamid,
        },
    )
    db.commit()

    link = f"{config.PUBLIC_WEB_BASE_URL}/?h={token}"
    name = _display_name(customer, lang)
    body = compose_reply(link=link, intent=intent, lang=lang, name=name)
    send_result = whatsapp_service.send_text(from_msisdn, body)

    log.info(
        "whatsapp inbound handled: from=%s intent=%s identified=%s ref=%s send_ok=%s",
        from_msisdn, intent, bool(customer), booking_ref, send_result.get("ok"),
    )
    return {
        "ok": True,
        "token": token,
        "identified": bool(customer),
        "intent": intent,
        "booking_ref": booking_ref,
        "send": send_result,
    }


def redeem(db: Session, *, token: str, session_id: str, now: datetime | None = None) -> dict:
    """Validate + SPEND a handoff token, binding it to the fresh web session.

    Single-use: stamps redeemed_at + redeemed_session and commits in ONE step, so
    a spent token can never be replayed. Returns the resolved context
    (customer_id, mobile, intent, ref, lang) or an error dict; does NOT touch the
    checkpoint — that is the route's job (the synthetic tool-pair bind), alongside
    the redeem audit. Never raises.
    """
    now = now or datetime.now()
    row = db.scalar(select(HandoffToken).where(HandoffToken.token == token))
    if row is None:
        return {"error": "invalid_token"}
    if row.redeemed_at is not None:
        return {"error": "already_redeemed"}
    if row.expires_at <= now:
        return {"error": "expired"}

    row.redeemed_at = now
    row.redeemed_session = session_id
    db.commit()

    customer = db.get(Customer, row.customer_id) if row.customer_id else None
    return {
        "customer_id": row.customer_id,
        "mobile": customer.mobile if customer else None,
        "name": _display_name(customer, row.lang.value),
        "title": customer.title if customer else None,
        "intent": row.intent,
        "booking_ref": row.booking_ref,
        "lang": row.lang.value,
    }
