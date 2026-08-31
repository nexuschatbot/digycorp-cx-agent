"""Human takeover service — an admin replaces Aisha on an escalated session.

The Handoffs queue records a promise ("a person will follow up"); this is the
service that lets a person actually keep it, live, in the same thread. Once an
admin takes a session over, the AI is out of it for good: the agent gate in
runner.run_turn refuses every later turn on any channel, and there is no
hand-back — the takeover ends only when the conversation closes.

STATE IS DERIVED FROM THE AUDIT TRAIL, exactly like "closed". There is no
Conversation table and no takeover table: starting a takeover writes one
`intent` row (actor=admin, payload action=TAKEOVER_ACTION, the admin's display
name), and "is this session taken over?" is answered by reading it back — the
same pattern, and the same portable payload-scan-in-Python, as
services/conversation.is_closed. One source of truth, already swept by erasure.

ONLY ESCALATED SESSIONS. Takeover is the supervisor's side of rule 13, not a
general barge-in: `start` requires that request_human_callback fired on this
session (an AuditType.escalation row exists) and that the conversation is still
open. First admin wins; a second attempt is told who already holds it.

TRANSCRIPT TURNS, NOT A SIDE CHANNEL. Messages exchanged during a takeover are
ordinary ConversationTurn rows — Speaker.staff for the admin, Speaker.customer
for the customer — so a page reload, the admin Conversations drawer, and
erasure all see them with zero new plumbing. The model's checkpoint never
learns of them, deliberately: the AI does not come back.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import lang as lang_service
from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    ConversationTurn,
    Language,
    Speaker,
)
from app.services import audit, conversation

# The payload marker for a takeover start, in the `intent` audit row. One
# constant shared by the writer and both readers (state here, the queue badge in
# services/console), so they can never drift apart.
TAKEOVER_ACTION = "takeover_started"


def is_escalated(db: Session, *, session_id: str) -> bool:
    """Did request_human_callback fire on this session? Read from the trail."""
    row = db.scalar(
        select(AuditEvent).where(
            AuditEvent.session == session_id,
            AuditEvent.type == AuditType.escalation,
        )
    )
    return row is not None


def state(db: Session, *, session_id: str) -> dict:
    """Who holds this session, if anyone. Derived, never stored.

    Scans this session's `intent` rows in Python rather than querying into the
    JSON payload — the same portability call services/conversation.is_closed
    makes (SQLite vs Postgres JSON dialects), on the same handful of rows.
    """
    rows = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.session == session_id,
            AuditEvent.type == AuditType.intent,
        )
        .order_by(AuditEvent.ts.asc(), AuditEvent.id.asc())
    ).all()
    for r in rows:
        payload = r.payload or {}
        if payload.get("action") == TAKEOVER_ACTION:
            return {
                "taken_over": True,
                "admin_name": payload.get("admin_name"),
                "ts": r.ts.isoformat() if r.ts else None,
            }
    return {"taken_over": False, "admin_name": None, "ts": None}


def start(db: Session, *, session_id: str, admin_name: str) -> dict:
    """Take an escalated, still-open session over for a named admin.

    Flushes but does NOT commit — the caller owns the transaction, like every
    service write here.

    FIRST ADMIN WINS. A second start on a taken session returns the existing
    state with `already: True` instead of stacking a second row; the caller
    decides whether that is a rejoin (same name reconnecting) or a refusal to
    surface. Errors are data, never raises — the WS endpoint turns them into
    frames, not stack traces.
    """
    name = (admin_name or "").strip()
    if not name:
        return {"error": "admin_name_required"}
    if not is_escalated(db, session_id=session_id):
        # Takeover is offered only where the customer asked for a person.
        return {"error": "not_escalated"}
    if conversation.is_closed(db, session_id=session_id):
        return {"error": "conversation_closed"}

    existing = state(db, session_id=session_id)
    if existing["taken_over"]:
        return {**existing, "already": True}

    audit.record(
        db,
        session=session_id,
        actor=Actor.admin,
        type=AuditType.intent,
        payload={"action": TAKEOVER_ACTION, "admin_name": name},
    )
    return {"taken_over": True, "admin_name": name, "already": False}


def append_turn(db: Session, *, session_id: str, speaker: Speaker, text: str) -> dict:
    """Persist one takeover message as an ordinary transcript turn.

    Flushes but does NOT commit — caller owns the transaction.

    customer_id IS COPIED FROM THE SESSION'S ATTRIBUTED TURNS: an escalation
    requires an identified customer (the tool node enforces it), so by the time
    a takeover exists the session's turns carry a customer_id — reusing it keeps
    the erasure edge (T4.6) intact for the human half of the conversation. If
    none is found (defensive), the turn is stored unattributed, which is the
    same honest null a pre-identification turn has.
    """
    body = (text or "").strip()
    if not body:
        return {"error": "empty_message"}
    if speaker not in (Speaker.staff, Speaker.customer):
        return {"error": "invalid_speaker"}

    customer_id = db.scalar(
        select(ConversationTurn.customer_id)
        .where(
            ConversationTurn.session_id == session_id,
            ConversationTurn.customer_id.is_not(None),
        )
        .limit(1)
    )
    lang = lang_service.detect(body)
    turn = ConversationTurn(
        session_id=session_id,
        customer_id=customer_id,
        speaker=speaker,
        lang=Language(lang),
        text=body,
    )
    db.add(turn)
    db.flush()
    return {
        "turn_id": turn.id,
        "speaker": speaker.value,
        "text": body,
        "lang": lang,
        "ts": turn.ts.isoformat() if turn.ts else None,
    }


__all__ = ["TAKEOVER_ACTION", "append_turn", "is_escalated", "start", "state"]
