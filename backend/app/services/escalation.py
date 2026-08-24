"""Human-callback escalation service (rule 13, FR-ES).

The minimal, in-scope escalation stub: Aisha cannot resolve everything herself —
price negotiation, financing, a complaint, or simply "get me a human" is out of
her lane, and so is a conversation where she has misunderstood twice in a row.
For those she offers a human specialist a callback and we LOG it, so the handoff
is auditable (FR-AD-02) and the console dashboard's `handoffs` block finally has
real data behind it. There is no supervisor UI and no real callback is placed —
the audit row IS the escalation, exactly as the signed scope describes ("agent
offers a human callback and logs it — no supervisor UI").

WHY AN AUDIT ROW, NOT A NEW TABLE. An escalation has no lifecycle we track in the
demo (no assignment, no resolution, no reply) — it is a single recorded fact:
"this conversation was handed to a human, for this reason". The append-only audit
trail is precisely the store for a recorded fact, it is already swept by erasure
(rule 12), and the dashboard already reads it. A dedicated table would add a
schema, a migration and an erasure branch to hold one row that never changes.

IDENTITY IS REQUIRED, and that is a flow requirement, not an accident. A callback
needs someone to call back: the tool node injects `customer_id` from the session
binding and short-circuits with `customer_not_identified` when there is none, so
Aisha must capture the name + mobile (find_or_create_customer) BEFORE she can
escalate. That is the "capture contact before you hand off" rule made structural.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import Actor, AuditEvent, AuditType
from app.services import audit

# The reasons an escalation may carry. A closed set so the dashboard's `reasons`
# breakdown is stable and the model cannot invent a category — an unknown value
# is coerced to `other` rather than rejected, because a logged handoff with a
# fuzzy reason is worth more than a lost one (rule 13: never dead-end a customer
# who asked for a person).
REASONS = frozenset(
    {
        "price_negotiation",
        "finance",
        "complaint",
        "repeated_failure",
        "explicit_request",
        "safety",
        "other",
    }
)


def create_callback(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    reason: str | None = None,
    topic: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Log a human-callback escalation for an identified customer. Never raises.

    Flushes but does NOT commit — the tool node owns the turn's transaction, so
    the escalation row lives or dies with the rest of the turn's writes (audit.py
    makes the same guarantee for the row it appends).

    IDEMPOTENT PER (session, reason): if this conversation already logged an
    escalation for the same reason, we return that one rather than stacking a
    second row. An LLM that calls the tool twice for one complaint must not read
    as two separate handoffs on the dashboard. A DIFFERENT reason in the same
    conversation is a genuinely new escalation and is logged.

    Returns the helpline so the number Aisha gives the customer is a tool-grounded
    fact (rule 2), not something recalled from the prompt.
    """
    if customer_id is None:
        # Defensive: the tool node already short-circuits an unidentified write,
        # so this is a belt, not the primary gate. Data, never a raise.
        return {
            "error": "customer_not_identified",
            "detail": (
                "A callback needs a customer to call back. Identify them with "
                "find_or_create_customer first."
            ),
        }

    resolved_reason = reason if reason in REASONS else "other"

    # Dedupe within this conversation on the same reason (see docstring). Match on
    # the JSON payload's reason via the same row shape audit.record writes.
    existing = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.session == session_id,
            AuditEvent.type == AuditType.escalation,
        )
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
    )
    if existing is not None and (existing.payload or {}).get("reason") == resolved_reason:
        return {
            "logged": True,
            "already_logged": True,
            "reason": resolved_reason,
            "helpline": config.ELITE_HELPLINE,
        }

    audit.record(
        db,
        session=session_id,
        actor=Actor.agent,
        type=AuditType.escalation,
        payload={
            "reason": resolved_reason,
            # `topic` is the customer's own words for what they want a human for
            # ("discount on the T2"), kept for the operator reading the trail. It
            # is never spoken back as policy — rule 13's "no fabricated policy".
            "topic": (topic or "").strip() or None,
            "customer_id": customer_id,
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {
        "logged": True,
        "already_logged": False,
        "reason": resolved_reason,
        "helpline": config.ELITE_HELPLINE,
    }
