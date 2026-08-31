"""Booking write tool (schema + thin fn).

Thin wrapper only: validate, call the service in-process (AD-J), return JSON.
Every guard — lock ownership, idempotency, doorstep address, data minimisation,
audit — lives in services/booking.py, because the one-tap WhatsApp path calls the
same service without ever passing through here.

`db`, `session_id`, `customer_id` and `correlation_id` are injected by the tool
node and MUST NOT appear in the input_schema. session_id is load-bearing: it is
how the service knows this conversation holds the lock. A session identity the
model could set would be an assertion it could change, not a fact.

customer_id is INJECTED, not declared (done — agent-loop phase, AD-H). The
trusted-context rule is explicit: "any tool argument that must be trusted
(session identity, authenticated customer id) follows the same rule — injected,
never declared." find_or_create_customer binds session_id -> customer_id in the
graph state server-side; the tool node resolves that binding and passes it here,
so the schema above no longer carries customer_id and a model cannot book a test
drive under another customer's identity. booking.create() KEEPS customer_id as a
service parameter — the CLI needs it and has no session; only the TOOL stops
declaring it. If no customer is identified yet, the tool node short-circuits with
`customer_not_identified` before this fn is ever called.

correlation_id ties this write's service-level audit rows (intent, doorstep
consent, notifications) back to the choke-point `tool_call` event for the same
invocation (behaviour rule 15, FR-AD-02) — see tools/__init__ and audit.record.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import booking as booking_service

# --------------------------------------------------------------------------- #
# create_booking (FR-BK-05/06/08/11, FR-SL-06/07, LOC-06)
# --------------------------------------------------------------------------- #
CREATE_BOOKING_SCHEMA = {
    "name": "create_booking",
    "description": (
        "Create the test-drive booking. This is the LAST step, and it is real: "
        "it produces a booking reference and sends the customer a confirmation.\n\n"
        "Before you call it, ALL of this must have happened:\n"
        "1. lock_slot succeeded on this slot.\n"
        "2. The personalisation exchange is done (companions, and a neutral "
        "offer to make the visit more comfortable).\n"
        "3. You gave the customer a full recap — model, location, date with the "
        "day name, and time — and they confirmed it.\n"
        "4. You reminded them to bring their driving licence AND recorded that "
        "with acknowledge_licence_reminder — this one is enforced, not trusted: "
        "the booking is refused without it.\n"
        "5. You asked about WhatsApp consent and recorded it with "
        "record_consent.\n"
        "Never call this to 'check' whether a booking would work.\n\n"
        "CONSENT DOES NOT GATE THE BOOKING. If the customer declined messaging, "
        "book them anyway — they get their test drive, we simply do not message "
        "them. Never suggest, or imply, that consent is required to book.\n\n"
        "Pass only what the customer actually told you. Every field but the ids "
        "is optional; omitting one is always better than inventing it. In "
        "particular do NOT fill companions or child_seat with a guess, and do "
        "NOT set consultant_pref unless they asked for it themselves.\n\n"
        "Read the response:\n"
        "- {'booking': {...}, 'created': true}: confirmed. Give them the "
        "booking_ref and tell them the confirmation is on its way (only if they "
        "consented to messaging).\n"
        "- 'idempotent': true means this booking already existed — you already "
        "booked it. Do not apologise, do not book again; just give them the "
        "reference.\n"
        "- {'error': 'slot_not_locked_by_session'}: the hold lapsed or was never "
        "taken. Call get_available_slots and re-offer honestly; do not tell them "
        "it is booked.\n"
        "- {'error': 'slot_unavailable'}: the slot went to someone else. "
        "Apologise briefly, re-query, re-offer.\n"
        "- {'error': 'doorstep_address_required'}: ask for the address you will "
        "bring the car to.\n"
        "- {'error': 'licence_reminder_not_acknowledged'}: you have not reminded "
        "them about their driving licence yet. Give the reminder in your next "
        "reply, call acknowledge_licence_reminder, then book. Do not apologise "
        "for this or mention it — just fold the reminder into what you say.\n"
        "Never announce a booking you did not receive a booking_ref for."
    ),
    "input_schema": {
        "type": "object",
        "required": ["vehicle_id", "slot_id"],
        "properties": {
            "vehicle_id": {
                "type": "integer",
                "description": (
                    "The vehicle being driven. Must be the vehicle the locked "
                    "slot is for — the slot decides the car."
                ),
            },
            "slot_id": {
                "type": "integer",
                "description": "The slot you locked with lock_slot.",
            },
            "driver_name": {
                "type": "string",
                "description": (
                    "Who will actually drive, if that is NOT the person you are "
                    "talking to — 'book it for my husband'. They need their own "
                    "licence on the day. Omit when the customer is the driver."
                ),
            },
            "driver_relationship": {
                "type": "string",
                "description": (
                    "How the driver relates to the customer, in their words: "
                    "'husband', 'wife', 'son', 'colleague'. Only with "
                    "driver_name."
                ),
            },
            "doorstep_address": {
                "type": "string",
                "description": (
                    "REQUIRED for a doorstep slot: the address to bring the car "
                    "to. Never pass this for a showroom booking — they are "
                    "coming to us, so we have no reason to hold their address."
                ),
            },
            "companions": {
                "type": "string",
                "description": (
                    "Who is coming with them, if they said: 'wife and two kids'. "
                    "Omit if they did not say — never guess."
                ),
            },
            "child_seat": {
                "type": "boolean",
                "description": (
                    "true ONLY if a child seat was actually discussed and "
                    "wanted. If the conversation already revealed young "
                    "children, offer one rather than asking whether family is "
                    "coming — but never assume the answer."
                ),
            },
            "consultant_pref": {
                "type": "string",
                "description": (
                    "A consultant preference the customer raised THEMSELVES, "
                    "e.g. 'female'. Honour it immediately and without comment. "
                    "Never suggest, offer, or ask about this — only ever record "
                    "what they asked for unprompted."
                ),
            },
        },
    },
}


def create_booking(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    vehicle_id: int | None = None,
    slot_id: int | None = None,
    driver_name: str | None = None,
    driver_relationship: str | None = None,
    doorstep_address: str | None = None,
    companions: str | None = None,
    child_seat: object = False,
    consultant_pref: str | None = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over booking.create (AD-J: in-process, never HTTP)."""
    if customer_id is None or vehicle_id is None or slot_id is None:
        return {
            "error": "invalid_identifiers",
            "detail": "customer_id, vehicle_id and slot_id are all required.",
        }
    return booking_service.create(
        db,
        session_id=session_id,
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        slot_id=slot_id,
        driver_name=driver_name,
        driver_relationship=driver_relationship,
        doorstep_address=doorstep_address,
        companions=companions,
        child_seat=child_seat,
        consultant_pref=consultant_pref,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- #
# get_booking (FR-RS-01, FR-CN-01) — the read lookup, "when's my booking?"
# --------------------------------------------------------------------------- #
# IDENTITY (developer decision 2026-07-23, tightening the 18 Jul ref-OR-mobile
# rule; the QUESTION ASKED changed again 2026-08-05, the structure did not).
# Looking up a booking requires an IDENTIFIED customer: call
# identify_by_reference_or_mobile FIRST — with their TD reference or their
# mobile, either alone, no name — so the session is bound, then call this. The
# lookup is scoped server-side to that customer's own bookings, so a reference
# can only ever surface bookings belonging to whoever it identified. mobile/ref
# stay DECLARED here — they say WHICH of the customer's bookings to fetch, not
# WHO they are (that is the injected session identity).
GET_BOOKING_SCHEMA = {
    "name": "get_booking",
    "description": (
        "Look up an existing test-drive booking so you can tell the customer "
        "about it, or before you reschedule or cancel it. Read-only — it changes "
        "nothing.\n\n"
        "FIRST identify the customer with identify_by_reference_or_mobile, using "
        "their booking reference OR their mobile — either one alone is enough, "
        "and you do NOT need their name. If you have not done that yet, this "
        "returns {'error': 'identity_required'}: ask for whichever of the two "
        "they have, identify them, then try again.\n\n"
        "Once identified, pass EITHER their mobile OR a TD reference "
        "(TD-YYYY-XXXX) to pick which of their bookings you mean; both can be "
        "exactly as the customer gave them (the tool normalises spacing, dashes "
        "and case).\n\n"
        "Read the response:\n"
        "- 'bookings' is the list of THEIR ACTIVE bookings (a confirmed or "
        "rescheduled test drive). Each has the vehicle, the slot (with a "
        "human-readable day, date and time in 'start_human'), the location "
        "(showroom name, or the doorstep service area), the status, and the "
        "booking_ref.\n"
        "- An EMPTY list means they have no active booking under those details. "
        "Say so honestly — do NOT invent one, and do NOT speculate about whether "
        "some other number or reference exists — and offer a human follow-up.\n"
        "- More than one booking means they have several live; list them briefly "
        "and let them say which they mean.\n"
        "- {'error': 'invalid_uae_mobile'}: the number could not be read; ask "
        "them to repeat it (see the 'reason'/'hint').\n"
        "Only ever describe a booking from what this tool returned."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {
            "mobile": {
                "type": "string",
                "description": (
                    "The customer's UAE mobile, exactly as they gave it. Any "
                    "common format is accepted. Omit if they gave a TD reference "
                    "instead."
                ),
            },
            "ref": {
                "type": "string",
                "description": (
                    "The booking reference, TD-YYYY-XXXX (e.g. TD-2026-HTXE). The "
                    "last four characters are letters AND digits, so never "
                    "assume they are numbers. Case, spaces and missing dashes "
                    "are all fine. Omit if they gave a mobile instead."
                ),
            },
        },
    },
}


def get_booking(
    db: Session,
    session_id: str,
    mobile: str | None = None,
    ref: str | None = None,
    viewer_customer_id: int | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over booking.find (AD-J: in-process). Read-only.

    viewer_customer_id is trusted context (never in the input schema, so the model
    cannot set it) — the VERIFIED session customer, or None.

    IDENTITY GATE (developer decision 2026-07-23; the identifier changed
    2026-08-05, the gate did not). Reads require a bound session: an unidentified
    session used to return a booking off a bare ref (PII stripped), which let
    anyone holding a guessed ref confirm it exists. So None is refused with
    identity_required — the agent must identify the customer first, now with the
    TD reference OR the mobile (identify_by_reference_or_mobile) rather than name
    + mobile — and once identified the lookup is scoped to that customer's OWN
    bookings, so a ref they do not own comes back as an honest empty result,
    never a stranger's booking.
    """
    if viewer_customer_id is None:
        return {
            "error": "identity_required",
            "detail": (
                "Identify the customer first with identify_by_reference_or_mobile, "
                "using their booking reference OR their mobile — either one alone "
                "is enough, and you do not need their name. Then look the booking "
                "up."
            ),
        }
    return booking_service.find(
        db,
        ref=ref,
        mobile=mobile,
        viewer_customer_id=viewer_customer_id,
        owner_customer_id=viewer_customer_id,
    )


# --------------------------------------------------------------------------- #
# reschedule_booking (FR-RS, behaviour rule 10, S-09/S-11)
# --------------------------------------------------------------------------- #
# new_slot_id is the slot the customer just accepted and you locked with
# lock_slot — declared, then validated server-side against this session's lock
# (the same ownership guard create_booking uses). ref/mobile identify WHICH
# booking to move. correlation_id is injected by the tool node so this write's
# service-audit rows share the choke-point tool_call's id (behaviour rule 15).
RESCHEDULE_BOOKING_SCHEMA = {
    "name": "reschedule_booking",
    "description": (
        "Move an existing booking to a new slot. This is real: it releases the "
        "old time, takes the new one, and re-sends the reminders.\n\n"
        "NEVER ask why they are changing — a reschedule is gracious, not "
        "questioned. First find the booking (get_booking by mobile or TD "
        "reference). Then, exactly as for a new booking, offer new times with "
        "get_available_slots, let them choose, and lock_slot the one they pick. "
        "Only then call this, with:\n"
        "- new_slot_id: the slot you just locked.\n"
        "- ref OR mobile: which booking to move (either identifies it).\n\n"
        "Before you call it, recap the NEW details in prose — the day with its "
        "date and time, AND the location if it has changed ('moved to Deira, "
        "Sunday 3 PM'), never just the new time — and let them confirm. The car "
        "stays the same; a reschedule cannot change the model.\n\n"
        "Read the response:\n"
        "- {'rescheduled': true}: done. Give them the same booking_ref (it does "
        "not change) and the new time; the confirmation is on its way if they "
        "have messaging consent.\n"
        "- 'idempotent': true: the booking was already on that slot — you already "
        "moved it. Do not move again; just confirm.\n"
        "- {'error': 'identity_required' or 'customer_not_identified' or "
        "'identity_not_verified'}: you have not confirmed who this is. Identify "
        "them with identify_by_reference_or_mobile first, using their booking "
        "reference OR their mobile (either alone is enough, and no name is "
        "needed) — a booking can only be moved by the person it belongs to. If "
        "nothing matches, tell them honestly no booking was found.\n"
        "- {'error': 'booking_not_found'}: nothing active of theirs under that "
        "identifier. Check the other one (mobile vs reference); never claim a "
        "booking exists.\n"
        "- {'error': 'multiple_bookings'}: they have more than one live booking; "
        "read back the list and ask which TD reference.\n"
        "- {'error': 'slot_not_locked_by_session'}: the new hold lapsed or was "
        "never taken. Re-query availability and re-offer.\n"
        "- {'error': 'vehicle_slot_mismatch'}: that slot is for a different car. "
        "Offer slots for the SAME model as the booking.\n"
        "- {'error': 'doorstep_address_required'}: the new slot is a doorstep "
        "visit; ask for the address to bring the car to.\n"
        "Never announce a move you did not receive 'rescheduled': true for."
    ),
    "input_schema": {
        "type": "object",
        "required": ["new_slot_id"],
        "properties": {
            "new_slot_id": {
                "type": "integer",
                "description": (
                    "The new slot the customer accepted and you locked with "
                    "lock_slot. Must be for the SAME vehicle as the booking."
                ),
            },
            "ref": {
                "type": "string",
                "description": (
                    "The booking to move, TD-YYYY-XXXX (letters and digits). "
                    "Case, spaces and missing dashes are all fine. Give this "
                    "OR mobile."
                ),
            },
            "mobile": {
                "type": "string",
                "description": (
                    "The customer's UAE mobile, if that is how they identified "
                    "the booking. Give this OR ref."
                ),
            },
            "doorstep_address": {
                "type": "string",
                "description": (
                    "Only if the NEW slot is a doorstep visit and we do not "
                    "already hold an address: where to bring the car. Never pass "
                    "it for a showroom slot."
                ),
            },
        },
    },
}


def reschedule_booking(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED — the VERIFIED session customer
    new_slot_id: int | None = None,
    slot_id: int | None = None,  # alias for new_slot_id — see below
    ref: str | None = None,
    mobile: str | None = None,
    doorstep_address: str | None = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over booking.reschedule (AD-J: in-process, never HTTP).

    `slot_id` is read as an ALIAS for new_slot_id (found live, 2026-08-31):
    every other booking action calls its slot `slot_id`, and models reliably
    reach for that name here too — the sub-agent proposed it twice and was
    refused twice for a spelling. The alias is safe because the value is
    guarded either way: the service books only a slot LOCKED BY THIS SESSION,
    so a wrong id (the old slot, a guess) is refused loudly, never used.

    customer_id is the session's verified customer, injected by the tool node
    (never modelled) and passed on as session_customer_id — the service refuses
    the move unless it owns the booking. If no customer is identified the tool
    node short-circuits before we get here, so a None arriving is defensive only.
    """
    if new_slot_id is None:
        new_slot_id = slot_id
    if new_slot_id is None:
        return {
            "error": "invalid_slot_id",
            "detail": "new_slot_id is required — lock the new slot first, then pass it here.",
        }
    if ref is None and mobile is None:
        return {
            "error": "identifier_required",
            "detail": "Give the booking's TD reference or the customer's mobile.",
        }
    return booking_service.reschedule(
        db,
        session_id=session_id,
        session_customer_id=customer_id,
        new_slot_id=new_slot_id,
        ref=ref,
        mobile=mobile,
        doorstep_address=doorstep_address,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- #
# cancel_booking (FR-CN, behaviour rule 11, S-12/S-13)
# --------------------------------------------------------------------------- #
CANCEL_BOOKING_SCHEMA = {
    "name": "cancel_booking",
    "description": (
        "Cancel an existing booking. This is real: it frees the slot and stops "
        "the reminders.\n\n"
        "BEFORE you call this you must have made EXACTLY ONE gentle retention "
        "offer — an alternative time or another model — and had it declined. Make "
        "that offer ONCE. If they still want to cancel, cancel: do NOT offer a "
        "second time, do not keep suggesting alternatives, and never pressure "
        "them. A cancel that turns into an argument is a bad experience. (If, "
        "after the offer, they say they would rather MOVE it, do a reschedule "
        "instead.)\n\n"
        "Identify the booking by mobile OR TD reference (either alone works); "
        "find it with get_booking first if you do not have the reference.\n\n"
        "A reason is OPTIONAL. You may accept one if they volunteer it, but NEVER "
        "ask for it or require it — passing no reason_code is completely fine.\n\n"
        "Read the response:\n"
        "- {'cancelled': true}: done. Confirm it warmly and briefly; a written "
        "cancellation notice is on its way.\n"
        "- 'idempotent': true: it was already cancelled — reassure them it is "
        "done, do not treat it as an error.\n"
        "- {'error': 'identity_required' or 'customer_not_identified' or "
        "'identity_not_verified'}: you have not confirmed who this is. Identify "
        "them with identify_by_reference_or_mobile first, using their booking "
        "reference OR their mobile (either alone is enough, and no name is "
        "needed) — a booking can only be cancelled by the person it belongs to. "
        "If nothing matches, tell them honestly no booking was found.\n"
        "- {'error': 'booking_not_found'}: nothing active of theirs under that "
        "identifier; check the other one, never claim to have cancelled "
        "something.\n"
        "- {'error': 'multiple_bookings'}: they have more than one; ask which TD "
        "reference before cancelling.\n"
        "Never tell a customer their booking is cancelled without 'cancelled': "
        "true (or 'idempotent': true)."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "The booking to cancel, TD-YYYY-XXXX (letters and digits). "
                    "Case, spaces and missing dashes are all fine. Give this "
                    "OR mobile."
                ),
            },
            "mobile": {
                "type": "string",
                "description": (
                    "The customer's UAE mobile, if that is how they identified "
                    "the booking. Give this OR ref."
                ),
            },
            "reason_code": {
                "type": "string",
                "description": (
                    "A short reason ONLY if the customer offered one unprompted "
                    "(e.g. 'changed_mind', 'bought_elsewhere', 'timing'). Never "
                    "ask for it; omit it if they did not say."
                ),
            },
        },
    },
}


def cancel_booking(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED — the VERIFIED session customer
    ref: str | None = None,
    mobile: str | None = None,
    reason_code: str | None = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over booking.cancel (AD-J: in-process, never HTTP).

    customer_id is the verified session customer, injected and passed on as
    session_customer_id — the service refuses to cancel a booking this customer
    does not own. As with reschedule, a None here is defensive: the tool node
    short-circuits an unidentified session before dispatch.
    """
    if ref is None and mobile is None:
        return {
            "error": "identifier_required",
            "detail": "Give the booking's TD reference or the customer's mobile.",
        }
    return booking_service.cancel(
        db,
        session_id=session_id,
        session_customer_id=customer_id,
        ref=ref,
        mobile=mobile,
        reason_code=reason_code,
        correlation_id=correlation_id,
    )
