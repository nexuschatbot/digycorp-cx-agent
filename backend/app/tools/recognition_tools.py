"""Recognition tool — bind the session AFTER the customer says it is still them.

Thin wrapper over services/recognition.py (AD-I/AD-J: in-process, never HTTP).

THIS TOOL IS THE CONSENT STEP MADE STRUCTURAL. The device cookie already told
the agent a name before this is called; what it cannot do on its own is act on
it. Recognising somebody is a guess about who is holding a browser, and the only
thing that turns a guess into a fact is the person answering. So the binding —
the thing that makes `customer_id` real for this session, unlocks their memory
profile and stops the agent re-asking for a mobile — lives behind a tool the
agent can only reach after it has asked, and the customer has answered.

`device_token` is INJECTED by the tool node from the HttpOnly cookie and appears
in no input_schema, for the same reason `customer_id` does not appear in
forget_me's: an identity the model supplied would be an assertion, and here the
assertion would be "I am the person this browser belongs to".

THERE IS NO `customer_id` PARAMETER, deliberately. The model never learns the id
— only a name — so it has nothing to pass and no way to name somebody else. The
id is resolved server-side from the cookie, which is the whole point.

WHAT A YES BUYS, AND THE RISK THAT CAME WITH IT (client decision, 2026-08-14).
A confirmed recognition is FULL identification: the customer may view, move and
cancel their bookings without a reference or a mobile, exactly as if they had
given one. This was built the other way first — recognition for new bookings
only, reference-or-mobile still required for changes — and the client chose this
instead, having been shown the trade-off.

The trade-off, stated plainly so nobody has to rediscover it: on a SHARED device,
whoever answers "yes, that's me" gets the absent person's bookings and can cancel
them. The cookie is possession of a browser and the yes is an unverified claim;
neither is proof. What stands between the two is the question itself — which is
why the question is not skippable, is enforced by the customer-turn guard in
tool_node rather than by wording, and must never be softened into a greeting.
If this is ever revisited, cancellation is the half worth taking back first: it
is irreversible and it frees the slot, while a reschedule is neither.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import customers as customers_service
from app.services import recognition as recognition_service
from app.models import Customer

# --------------------------------------------------------------------------- #
# confirm_returning_customer (C10)
# --------------------------------------------------------------------------- #
CONFIRM_RETURNING_CUSTOMER_SCHEMA = {
    "name": "confirm_returning_customer",
    "description": (
        "Call this ONLY after you have asked a returning customer whether they "
        "are still the person we recognised, AND THEY HAVE SAID YES.\n\n"
        "You are told a name at the start of a conversation when this browser "
        "has been used before. That is a hint about a DEVICE, not a fact about "
        "the person in front of you — phones are shared, borrowed and handed "
        "down. So you ask, lightly and in one line, and you wait for the "
        "answer.\n\n"
        "If they say yes, call this. It picks up who they are, along with "
        "anything they previously asked us to remember, so you do not make them "
        "give their name and number again.\n\n"
        "IF THEY SAY NO, OR IT IS SOMEONE ELSE, DO NOT CALL THIS. Say something "
        "warm and short, drop the name entirely, and carry on as a completely "
        "new conversation — find_or_create_customer as usual. Never argue with "
        "them and never ask a second time.\n\n"
        "Do not call it speculatively, do not call it to 'check' who they are, "
        "and never call it before you have asked. If the answer is ambiguous, "
        "treat that as a no.\n\n"
        "Their yes IDENTIFIES them. Once this returns you know who they are, so "
        "you may show, move and cancel their bookings without asking for a "
        "reference or a mobile — do not call identify_by_reference_or_mobile "
        "afterwards. All the care lives in the question you asked before this, "
        "which is exactly why that question is never skipped."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {},
    },
}


def confirm_returning_customer(
    db: Session,
    session_id: str,
    device_token: str | None = None,  # INJECTED by the tool node, never modelled
    **_ignored: object,
) -> dict:
    """Resolve the device's customer and return the standard identity envelope.

    Returns the SAME shape as find_or_create_customer and
    identify_by_reference_or_mobile — `{"customer": ..., "created": false,
    "memory": ...}` — so the tool node's binding, session_facts, the identity
    anchor and the consent-gated memory recall all read one shape and none of
    them needs to know which of the three doors the customer came through. That
    is also why this returns nothing bespoke: a fourth envelope shape would be a
    fourth thing for the prompt to learn.

    A device we no longer recognise is DATA, not a crash. It happens for ordinary
    reasons — the customer withdrew memory consent between visits, or asked to be
    forgotten, or the cookie is from a wiped demo database — and the agent
    recovers by simply not mentioning it and asking for a name as usual.
    """
    hit = recognition_service.lookup(db, token=device_token)
    if hit is None:
        return {
            "error": "device_not_recognised",
            "detail": (
                "This browser is not linked to anyone. Do not mention "
                "recognition at all — greet them as a new customer and use "
                "find_or_create_customer when you have their name and mobile."
            ),
        }

    customer = db.get(Customer, hit["customer_id"])
    if customer is None:  # deleted between the lookup and here
        return {"error": "device_not_recognised", "detail": "Greet them as a new customer."}

    # Refresh last_seen: this is the visit that proves the link is still live, and
    # it is what picks the right person on a device two people share.
    recognition_service.link(
        db, token=device_token, customer_id=customer.id, session_id=session_id
    )
    return customers_service.envelope_for(db, customer)
