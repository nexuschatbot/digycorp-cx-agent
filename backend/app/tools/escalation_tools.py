"""Human-callback escalation tool (schema + thin fn) — rule 13, FR-ES.

A THIN wrapper: validate nothing the service does not, call the service in-process
(AD-J), return JSON. The escalation policy (which reasons exist, dedupe, the audit
row) lives in services/escalation.py.

`db`, `session_id`, `customer_id` and `correlation_id` are INJECTED by the tool
node and MUST NOT appear in the input_schema. customer_id in particular is the
whole point: a callback needs someone to call back, so the tool node resolves it
from the session -> customer binding and short-circuits with
`customer_not_identified` if there is none. That is what forces Aisha to capture
the name + mobile (find_or_create_customer) before she can hand off — "capture
contact before you hand off", made structural instead of hoped for in the prompt.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import escalation as escalation_service

REQUEST_HUMAN_CALLBACK_SCHEMA = {
    "name": "request_human_callback",
    "description": (
        "Arrange for a human specialist to call the customer back, and log it. "
        "Call this the moment something is genuinely outside what you can do, "
        "rather than guessing or dead-ending them.\n\n"
        "Use it when:\n"
        "- the customer asks to speak to a person, in any words or language "
        "(reason 'explicit_request') — do this instantly, never try to talk them "
        "out of it;\n"
        "- they want to negotiate price, or ask about discounts (reason "
        "'price_negotiation'), financing, insurance or trade-ins (reason "
        "'finance') — you book test drives, you do not do deals;\n"
        "- they raise a complaint (reason 'complaint');\n"
        "- they ask about a defect, recall, or accident/safety matter (reason "
        "'safety') — never improvise on these;\n"
        "- you have misunderstood them twice in a row (reason 'repeated_failure');\n"
        "- anything else that is clearly not a test-drive booking (reason 'other').\n\n"
        "BEFORE you call this you must have identified the customer with "
        "find_or_create_customer, because a callback needs a name and mobile to "
        "reach them on. If no one is identified yet, ask for their name and mobile "
        "first, warmly, folded into the same sentence — a colleague cannot call a "
        "blank. If the customer declines to give a number, you may still book them "
        "later, but a callback cannot be arranged without one.\n\n"
        "This does not end the conversation and it is not the last word: once it "
        "is logged, tell the customer plainly that you have passed it to our "
        "team, and that a colleague may join this very chat to help them "
        "directly, or arrange a callback instead — 'may', never a promise of "
        "which or of when. Give them the helpline from the result so they also "
        "have a way to reach us, and then offer to keep helping with the test "
        "drive itself. Never invent a policy, a price, or a promise on the "
        "human's behalf — you are passing it on, not answering it."
    ),
    "input_schema": {
        "type": "object",
        "required": ["reason"],
        "properties": {
            "reason": {
                "type": "string",
                "enum": [
                    "price_negotiation",
                    "finance",
                    "complaint",
                    "repeated_failure",
                    "explicit_request",
                    "safety",
                    "other",
                ],
                "description": "Why you are handing this to a human. Pick the closest.",
            },
            "topic": {
                "type": "string",
                "description": (
                    "The customer's own words for what they want a human for, in "
                    "one short phrase (for example 'discount on the T2 i-DM'). "
                    "Helps the colleague pick up without making the customer "
                    "repeat themselves. Omit if there is nothing specific."
                ),
            },
        },
    },
}


def request_human_callback(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    reason: str | None = None,
    topic: str | None = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Thin wrapper over escalation.create_callback (AD-J: in-process)."""
    if customer_id is None:
        return {
            "error": "customer_not_identified",
            "detail": (
                "No customer is identified yet. Ask for their name and mobile and "
                "call find_or_create_customer before arranging a callback."
            ),
        }
    return escalation_service.create_callback(
        db,
        session_id=session_id,
        customer_id=customer_id,
        reason=reason,
        topic=topic,
        correlation_id=correlation_id,
    )
