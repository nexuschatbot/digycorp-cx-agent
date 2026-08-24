"""Memory tools — save a soft preference, and forget a customer (FR-MEM, rule 12).

Thin wrappers over services/memory.py (AD-I/AD-J: in-process, never over HTTP).

THERE IS NO RECALL TOOL HERE, DELIBERATELY. A consented profile arrives on
find_or_create_customer's result instead. The agent therefore sees memory exactly
once, at identification, and has no way to go looking for something to open the
conversation with — which is how rule 12's "reactive-first, never volunteered to
show off" becomes a capability the agent lacks rather than an instruction we hope
the prompt holds.

`customer_id` and `correlation_id` on both functions are INJECTED by the tool
node and appear in neither input_schema. A customer identity the model supplied
would be an assertion, not a fact — and on forget_me it would be an assertion
that deletes somebody's data.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import memory as memory_service

# --------------------------------------------------------------------------- #
# save_memory (FR-MEM)
# --------------------------------------------------------------------------- #
SAVE_MEMORY_SCHEMA = {
    "name": "save_memory",
    "description": (
        "Record ONE thing worth remembering about this customer for next time — "
        "something they told you that a booking cannot show us on its own.\n\n"
        "The car they book, the showroom they choose and the time of day they "
        "prefer are already learned automatically from the booking itself. Do "
        "NOT record those here; you would only be repeating what we already "
        "know.\n\n"
        "Use it for the softer things they mention: that they prefer a female "
        "consultant, that they usually bring their children, that they like a "
        "call before we arrive.\n\n"
        "Only ever record what the customer actually SAID. Never infer a "
        "preference from a single visit, never guess, and never record anything "
        "sensitive they did not volunteer. Write it in the language they said it "
        "in — do not translate it.\n\n"
        "Call it once, quietly, when something is genuinely worth keeping — not "
        "on every turn and not out of habit. Do not tell the customer you are "
        "saving it and do not ask permission here: whether we may remember them "
        "at all is a separate question, asked with record_consent. If they have "
        "not agreed to be remembered this returns "
        "{'error': 'memory_consent_not_granted'} — that is a normal answer, not "
        "a failure. Carry on with the conversation and never mention it."
    ),
    "input_schema": {
        "type": "object",
        "required": ["notes"],
        "properties": {
            "notes": {
                "type": "string",
                "description": (
                    "One short sentence, in the customer's own words and in the "
                    "script they wrote it in. Do not translate it."
                ),
            },
        },
    },
}


def save_memory(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    notes: object = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over memory.save (AD-J: in-process)."""
    if customer_id is None:
        return {"error": "customer_not_found", "customer_id": None}
    return memory_service.save(
        db,
        session_id=session_id,
        customer_id=customer_id,
        notes=notes,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- #
# forget_me (FR-MEM, rule 12, S-08)
# --------------------------------------------------------------------------- #
FORGET_ME_SCHEMA = {
    "name": "forget_me",
    "description": (
        "The customer has asked us to forget them — in any language and however "
        "they phrase it ('delete my details', 'انسى بياناتي', 'don't keep "
        "anything about me'). Honour it immediately and warmly, and never ask "
        "why.\n\n"
        "THIS DELETES EVERYTHING WE HOLD ABOUT THEM, not just their "
        "preferences: their name and mobile number, their conversations with "
        "us, any voice notes, and EVERY BOOKING THEY HAVE — including test "
        "drives that have not happened yet. Those bookings are cancelled and "
        "cannot be restored.\n\n"
        "TWO STEPS, ALWAYS.\n"
        "First, call this WITHOUT confirmed. Nothing is deleted — you get back "
        "'would_delete', which is what a yes would remove. Tell them plainly "
        "what that is, in your own words, and ask once whether to go ahead.\n\n"
        "IF 'would_delete.bookings' IS NOT EMPTY YOU MUST SAY SO, NAMING THE "
        "DATE AND TIME of each one from 'when_human', before you ask. Someone "
        "agreeing to have their details cleared is not agreeing to lose "
        "Saturday's test drive, and finding that out at the showroom is the "
        "worst way for them to learn it. Say it warmly and without pressure — "
        "you are making sure they know, not talking them out of it. If they "
        "would rather keep the booking, that is a perfectly good answer: do "
        "not erase, and offer to do it after the visit instead.\n\n"
        "Then, ONLY if they say yes, call it again with confirmed set to true. "
        "That erases it permanently. It cannot be undone, and we treat them as a "
        "new customer from then on.\n\n"
        "Do NOT promise more than we can do. Any WhatsApp message we already "
        "sent is on their phone and stays there — we delete our copy, we cannot "
        "unsend it. If they ask, say that honestly.\n\n"
        "If they say no, say nothing further about it and carry on as normal. "
        "Never try to talk them out of it, never delay it, and never make it "
        "feel like a process they have to go through."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {
            "confirmed": {
                "type": "boolean",
                "description": (
                    "true ONLY after you have told them what we hold and they "
                    "have said yes to erasing it. Omit it on the first call."
                ),
            },
        },
    },
}


def forget_me(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    confirmed: object = False,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over memory.forget (AD-J: in-process).

    An unidentified customer is DATA, not a crash: "forget me" before we know
    who they are is a real conversational state, and the agent recovers by
    asking for the mobile.
    """
    if customer_id is None:
        return {
            "error": "customer_not_identified",
            "detail": "Ask for their mobile number and call find_or_create_customer first.",
        }
    return memory_service.forget(
        db,
        session_id=session_id,
        customer_id=customer_id,
        confirmed=confirmed,
        correlation_id=correlation_id,
    )
