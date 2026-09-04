"""Customer identity + consent tools (schema + thin fn).

TWO IDENTITY TOOLS, ONE PER JOURNEY (2026-08-05). find_or_create_customer is how
a NEW booking starts and still takes name + mobile. identify_by_reference_or_
mobile is how an EXISTING booking is reached, and takes the TD reference OR the
mobile, either alone, with no name at all. Both return the same envelope, so the
tool node binds the session identically from either (see _IDENTITY_TOOLS there).


Every function here is a THIN wrapper: validate, call the service in-process
(AD-J), return JSON. No business logic, no DB access — the mobile rules, the
never-overwrite rule and the consent audit all live in the services.

`db`, `session_id`, and (for record_consent) `customer_id` + `correlation_id` are
injected by the tool node and MUST NOT appear in any input_schema. session_id
matters here: it is what stamps every consent change in the audit trail with the
conversation it came from (FR-AD-02).

record_consent's customer_id is INJECTED, not declared (done — agent-loop phase,
AD-H), per the trusted-context rule ("any tool argument that must be trusted
(session identity, authenticated customer id) follows the same rule — injected,
never declared"). A model that passed an arbitrary customer_id here would record
one customer's consent decision against another's profile — a PDPL problem, not
just a bug. find_or_create_customer binds session_id -> customer_id in the graph
state server-side; the tool node resolves it and passes it here. If no customer
is identified yet, the tool node short-circuits with `customer_not_identified`.
find_or_create_customer itself needs no customer_id — it MINTS the binding, so it
keeps declaring only the mobile. See the same pattern in booking_tools.py.

correlation_id ties each consent audit row back to the choke-point `tool_call`
event for the same record_consent invocation (rule 15) — see audit.record.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import config
from app.services import booking as booking_service
from app.services import consent as consent_service
from app.services import customers as customers_service

# --------------------------------------------------------------------------- #
# find_or_create_customer (LOC-09, LOC-07)
# --------------------------------------------------------------------------- #
# The mobile framing depends on config.STRICT_UAE_MOBILE. In the default (strict)
# mode the tool accepts UAE mobiles only, and the description says so — a load-
# bearing signal (CLAUDE.md: "description IS prompt engineering"). When the demo
# relaxes the rule to accept any number — with a country code or without one —
# the description must relax TOO: otherwise the model reads "UAE mobile" here and
# pre-emptively refuses a +92 number before ever calling the tool, which is exactly
# the rule-2 inversion we are trying to avoid (validity is the tool's fact to
# state, not the model's).
#
# THE RELAXED TEXT BANS THE QUESTION EXPLICITLY, not just the refusal (2026-08-04).
# Told only that international numbers are accepted, the model asked "which country
# is that?" and stalled the booking there — a customer should not have to explain
# their own phone number, and asking is the same rule-2 inversion wearing a
# politer face.
if config.STRICT_UAE_MOBILE:
    _MOBILE_INTRO = (
        "Identify the customer by their UAE mobile number. Every booking starts "
        "here: you cannot create, reschedule or cancel a test drive for someone "
        "we have not identified.\n\n"
        "Call this once you have their mobile — naturally, as part of the "
        "conversation, not as a form. Pass their name and title in the SAME call "
        "if they have already told you; never ask again for something the "
        "conversation has already revealed.\n\n"
        "Pass the number exactly as the customer typed it. '050 123 4567', "
        "'+971 50 123 4567' and '971501234567' are all fine — the tool "
        "normalises them. Do NOT tidy it up, and never invent or complete a "
        "digit you were not given.\n\n"
        "If the response is {'error': 'invalid_uae_mobile'}, read 'reason':\n"
        "- 'landline': they gave a UAE landline. We confirm by WhatsApp, so ask "
        "warmly for a mobile.\n"
        "- 'too_short' / 'too_long': ask them to read the number back. Never "
        "guess the missing digits.\n"
        "- 'unknown_prefix': the number is not a UAE mobile; ask them to check.\n"
        "The 'hint' is guidance for you, not a script — say it in your own words, "
        "in the customer's language, and never read it out verbatim.\n\n"
    )
else:
    _MOBILE_INTRO = (
        "Identify the customer by their mobile number. Every booking starts "
        "here: you cannot create, reschedule or cancel a test drive for someone "
        "we have not identified.\n\n"
        "Call this once you have their mobile — naturally, as part of the "
        "conversation, not as a form. Pass their name and title in the SAME call "
        "if they have already told you; never ask again for something the "
        "conversation has already revealed.\n\n"
        "ANY number is accepted, in any shape the customer writes it: with a "
        "country code (+92..., +44..., +971...) or without one at all "
        "('0340929775', '050 123 4567'). NEVER ask which country a number is "
        "from and NEVER ask for a country code — a customer should not have to "
        "explain their own number to book a test drive. NEVER decide yourself "
        "that a number is the wrong country or otherwise unusable; that is the "
        "tool's judgement, not yours. Pass the number exactly as the customer "
        "typed it and let the tool answer. Do NOT tidy it up, and never invent "
        "or complete a digit you were not given.\n\n"
        "If, and ONLY if, the response is {'error': 'invalid_uae_mobile'}, read "
        "'reason':\n"
        "- 'landline': they gave a landline; we confirm by WhatsApp, so ask "
        "warmly for a mobile.\n"
        "- 'too_short' / 'too_long': ask them to read the number back. Never "
        "guess the missing digits.\n"
        "The 'hint' is guidance for you, not a script — say it in your own words, "
        "in the customer's language, and never read it out verbatim.\n\n"
    )

FIND_OR_CREATE_CUSTOMER_SCHEMA = {
    "name": "find_or_create_customer",
    "description": (
        _MOBILE_INTRO
        +
        "The response tells you what we already know: 'created': false means a "
        "returning customer. 'consent_messaging' and 'consent_memory' are their "
        "recorded consent — treat them as fact and never assume either is true. "
        "Do not greet a returning customer with what we remember about them; "
        "recall is reactive, used to shorten what they are asking for now.\n\n"
        "A 'memory' key ALONGSIDE 'customer' (never inside it) is what we "
        "remember: their usual models, showroom, time of day, and a note. It "
        "appears ONLY when they have consented and we actually hold something, "
        "so if the key is missing there is simply nothing to recall — that is "
        "normal, and not something to mention. This is the only place memory "
        "ever reaches you; there is no separate tool to look it up.\n\n"
        "The name comes back as 'name_en' and 'name_ar', and either may be null. "
        "Use whichever fits the language you are speaking THIS turn — if they "
        "switch to Arabic mid-conversation, switch to name_ar. Never "
        "transliterate one into the other yourself, and if the script you need "
        "is null, simply use the one you have.\n\n"
        "THIS TOOL IS FOR MAKING A NEW BOOKING, and here identity is NAME + "
        "NUMBER: a number alone will not bind a session, so a returning "
        "customer's supplied name must MATCH and a new customer must GIVE one. "
        "A customer who wants to SEE, MOVE or CANCEL a booking they already have "
        "does NOT come through here — that is identify_by_reference_or_mobile, "
        "which takes their reference or their number and never needs a name. "
        "Read the response:\n"
        "- {'error': 'name_required'}: you did not give a name — the number is "
        "either on file under a name, or new to us and a booking needs one. Ask "
        "for their full name, then call this again with both, before creating "
        "the booking. If what they actually want is to change or cancel an "
        "existing booking, stop and use identify_by_reference_or_mobile "
        "instead — do not ask them for a name.\n"
        "- {'error': 'identity_unconfirmed'}: the name does not match this number. "
        "Tell them honestly that you could not find a booking under those details; "
        "do NOT say whether the number is on file, and offer to re-check the name "
        "and number or a human follow-up. Never guess or try other spellings.\n"
        "A NEW customer (a number we have never seen) has nothing to match, so "
        "identity_unconfirmed never applies to a first-time booking."
    ),
    "input_schema": {
        "type": "object",
        "required": ["mobile"],
        "properties": {
            "mobile": {
                "type": "string",
                "description": (
                    "The customer's UAE mobile, exactly as they gave it. Any "
                    "common format is accepted."
                    if config.STRICT_UAE_MOBILE
                    else "The customer's mobile number, exactly as they gave it. "
                    "Any format is accepted, with or without a country code; "
                    "do not reject, complete or alter it."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "The customer's name if they have given it, in the script "
                    "they wrote it in — do not transliterate Arabic into Latin "
                    "or vice versa. Omit if they have not said."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Respectful title if they have given or clearly indicated "
                    "one: Mr, Ms, Mrs, Dr, H.E., Sheikh, Sheikha. Omit if not "
                    "stated — never infer a title, and never guess from a name."
                ),
            },
        },
    },
}


def find_or_create_customer(
    db: Session,
    session_id: str,
    mobile: str | None = None,
    name: str | None = None,
    title: str | None = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over customers.find_or_create (AD-J: in-process)."""
    if mobile is None:
        return {
            "error": "invalid_uae_mobile",
            "reason": "missing",
            "hint": "No mobile number was given. Ask for it.",
        }
    # verify_name=True (developer decision 2026-07-23): the name+number second
    # factor. For a RETURNING customer we hold a name for, the supplied name must
    # match or the session is NOT bound (name_required / identity_unconfirmed).
    # require_name_for_create=True (2026-07-31): a NEW customer cannot be
    # created nameless from the agent path — the voice-call model once skipped
    # the name question and booked a drive for a customer named "—".
    return customers_service.find_or_create(
        db, mobile=mobile, name=name, title=title, verify_name=True,
        require_name_for_create=True,
    )


# --------------------------------------------------------------------------- #
# identify_by_reference_or_mobile (FR-RS-01, FR-CN-01) — the MANAGE journey
# --------------------------------------------------------------------------- #
# The second front door, added 2026-08-05 (product decision). find_or_create_
# customer above is for MAKING a booking and still demands name + mobile; this one
# is for a booking that ALREADY EXISTS, and asks for the TD reference or the
# mobile, either alone. Two tools rather than one flag on one tool: the two
# journeys ask different questions, and a single tool teaching both would have to
# explain when the name matters and when it does not — the sort of conditional
# instruction a model gets wrong under load.
#
# It CANNOT create anyone (services/booking.identify is a pure lookup), which is
# what makes the relaxed identity safe to attach to it: the worst case is failing
# to find a customer, never minting a nameless one.
IDENTIFY_BY_REFERENCE_OR_MOBILE_SCHEMA = {
    "name": "identify_by_reference_or_mobile",
    "description": (
        "Identify a customer who ALREADY HAS a booking, so you can look it up, "
        "move it, or cancel it. Call this FIRST for any of those three — "
        "get_booking, reschedule_booking and cancel_booking all need the "
        "customer identified, and they will refuse until this has succeeded.\n\n"
        "Ask for the BOOKING REFERENCE (TD-YYYY-XXXX, e.g. TD-2026-HTXE) or the "
        "MOBILE NUMBER on the booking. EITHER ONE ON ITS OWN IS ENOUGH. Ask for "
        "whichever they have, in one short question, and never insist on both. "
        "DO NOT ASK FOR THEIR NAME: it is not needed here and asking for it is "
        "one more thing a customer has to produce before they can cancel "
        "something.\n\n"
        "Pass the reference exactly as they gave it — spacing, dashes and case "
        "are all handled ('td 2026 htxe' is the same reference as "
        "TD-2026-HTXE). Same for the number: pass what they said and let the "
        "tool judge it. Never invent, complete or correct either one.\n\n"
        "Do NOT call this when you are making a NEW booking — that is "
        "find_or_create_customer, which needs their name and mobile. And do not "
        "call it at all if this conversation has already identified them; you "
        "know who you are speaking with and asking again is starting over.\n\n"
        "The response is the customer, exactly as find_or_create_customer "
        "returns one: 'consent_messaging' and 'consent_memory' are recorded "
        "fact, and a 'memory' key ALONGSIDE 'customer' is what we remember — "
        "used to shorten what they are asking for now, never recited back at "
        "them.\n\n"
        "Read the response:\n"
        "- {'error': 'no_booking_found'}: nothing matches. Say so honestly, "
        "never invent a booking, and offer to try the other identifier (the "
        "reference if they gave a number, the number if they gave a reference) "
        "or a human follow-up. Do NOT tell them whether the number itself is on "
        "file.\n"
        "- {'error': 'identifier_required'}: you called this with neither. Ask "
        "for the reference or the mobile.\n"
        "- {'error': 'invalid_uae_mobile'}: the number could not be read. Use "
        "the 'reason' and 'hint' and ask them to read it back; never guess a "
        "missing digit."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "The booking reference, TD-YYYY-XXXX. Case, spaces and "
                    "missing dashes are all fine. Omit if they gave a mobile "
                    "instead."
                ),
            },
            "mobile": {
                "type": "string",
                "description": (
                    "The mobile number on the booking, exactly as the customer "
                    "gave it. Any format is accepted. Omit if they gave a "
                    "reference instead."
                ),
            },
        },
    },
}


def identify_by_reference_or_mobile(
    db: Session,
    session_id: str,
    ref: str | None = None,
    mobile: str | None = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over booking.identify (AD-J: in-process). Read-only.

    Returns the SAME envelope find_or_create_customer returns, which is what lets
    the tool node bind the session from either tool without knowing which journey
    the customer is on (see _IDENTITY_TOOLS in app/agent/tool_node.py).
    """
    return booking_service.identify(db, ref=ref, mobile=mobile)


# --------------------------------------------------------------------------- #
# record_consent (FR-CF-05, PDPL)
# --------------------------------------------------------------------------- #
RECORD_CONSENT_SCHEMA = {
    "name": "record_consent",
    "description": (
        "Record what the customer has decided about consent. Call this as soon "
        "as they answer — a consent decision that is not recorded did not "
        "happen.\n\n"
        "There are TWO SEPARATE decisions and they are never the same question:\n"
        "- 'messaging': may we send WhatsApp confirmations and reminders about "
        "this test drive?\n"
        "- 'memory': may we remember them for next time?\n"
        "A customer can accept one and refuse the other, and very often does. "
        "Pass ONLY the decision they actually made; never infer one from the "
        "other, and never pass a value they have not given you.\n\n"
        "Consent can be WITHDRAWN. If they say 'stop messaging me' or 'don't "
        "keep my details', pass false — that is a real decision to record, not a "
        "reason to do nothing. Honour it immediately and without argument; do "
        "not try to talk them out of it or ask why.\n\n"
        "Ask for messaging consent BEFORE creating the booking, and ask it as "
        "its OWN question, in its own message, AFTER the customer has confirmed "
        "the recap. It is never folded into the recap and never shares a "
        "message with any other question. The recap asks whether the details "
        "are right; this asks whether we may message them; a customer who says "
        "yes to one has not answered the other. Pass 'messaging' ONLY when the "
        "question they were answering was this one. If you are not certain what "
        "a yes was about, pass nothing: ask again, plainly and on its own, and "
        "record the answer to that.\n\n"
        "NEVER PASS A VALUE YOU SUPPLIED YOURSELF. If you asked the messaging "
        "question in this same message, the answer has not arrived — a customer "
        "cannot have replied to a message you are still writing. Nothing you "
        "write counts as their answer, however obvious it seems: stop, let the "
        "message go, and record what they actually say next.\n\n"
        "A refusal never blocks the booking: if they decline, book the test "
        "drive anyway and simply do not message them. Never imply they must "
        "consent to be booked, and never present consent as a formality."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {
            "messaging": {
                "type": "boolean",
                "description": (
                    "true if they agreed to WhatsApp confirmations/reminders, "
                    "false if they declined or withdrew. Omit if they have not "
                    "been asked. A yes that answered the recap, the licence "
                    "reminder, or any other question is NOT a messaging "
                    "decision and must not be passed here."
                ),
            },
            "memory": {
                "type": "boolean",
                "description": (
                    "true if they agreed to be remembered for next time, false "
                    "if they declined or withdrew. Omit if they have not been "
                    "asked."
                ),
            },
        },
    },
}


def record_consent(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    messaging: object = None,
    memory: object = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over consent.record (AD-J: in-process)."""
    if customer_id is None:
        return {"error": "customer_not_found", "customer_id": None}
    return consent_service.record(
        db,
        session_id=session_id,
        customer_id=customer_id,
        messaging=messaging,
        memory=memory,
        correlation_id=correlation_id,
    )
