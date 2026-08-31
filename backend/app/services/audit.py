"""Append-only audit trail writer (FR-AD-02, behaviour rule 15).

ONE writer, so the four write services do not each hand-roll an insert and drift
apart on field names. Insert only — this module deliberately offers no update or
delete, because an audit trail you can edit is not evidence.

WHY WRITE AUDITING LIVES IN THE SERVICES, NOT THE TOOLS
-------------------------------------------------------
The tool registry is the right place to audit READS: the meaningful event there
is "the agent asked", and catalogue.search is also called by the console, where
logging it as agent grounding would be a lie.

Writes are the opposite. Every write service has TWO callers — the agent path
(tool -> service) and the direct path (one-tap WhatsApp reminder buttons -> REST
route -> service, skipping the LLM entirely). Auditing a write inside its tool
would silently lose every reminder-button action from the trail, and FR-AD-02
requires all of it. So writes audit HERE, at the only point both callers pass
through.

`session` is the executor-injected session identity, never a value the model
supplied. The direct path has no conversation, so it passes its own origin
marker (e.g. "cli:create-demo-booking") — an honest label beats a fabricated
uuid that would make a CLI action look like a customer conversation.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Actor, AuditEvent, AuditType


def record(
    db: Session,
    *,
    session: str,
    actor: Actor,
    type: AuditType,
    payload: dict | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    """Append one event. Flushes but does NOT commit.

    The caller owns the transaction: an audit row must live or die with the
    write it describes. Committing here would leave "we cancelled your booking"
    in the trail after the cancellation itself rolled back.

    CORRELATION ID (agent-loop phase) — one tool invocation, several facets.
    A single write produces two audit rows: the choke-point `tool_call` ("the
    agent invoked create_booking") and the service's semantic event ("booking
    TD-2026-0142 created"). Both are stamped with the same `correlation_id`, so
    the console groups them as one action instead of showing what reads like
    double-logging. Stamped here, in the one writer, so every caller — choke
    point and service — threads it identically and none can forget the key. The
    id lands in `payload["correlation_id"]`: AuditEvent has no dedicated column,
    and the payload is the JSON the console already reads.
    """
    body = dict(payload or {})
    if correlation_id is not None:
        body["correlation_id"] = correlation_id
    event = AuditEvent(
        session=session,
        actor=actor,
        type=type,
        payload=body,
    )
    db.add(event)
    db.flush()
    return event
