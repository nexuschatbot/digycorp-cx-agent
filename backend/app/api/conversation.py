"""Conversation resume + close — the DIRECT path (AD-I), no LLM involved.

Two thin routes over services/conversation, the same service the agent's
close_conversation tool calls. This is the two-caller pattern CLAUDE.md describes
for the one-tap WhatsApp reminder buttons, arriving early: a structured intent
posted straight to REST, skipping the model entirely. Tapping "New chat" must
never invoke a language model, and it never does.

    GET  /api/conversation/{session_id}        -> transcript + status (read-only)
    POST /api/conversation/{session_id}/close  -> close it (idempotent)

WHY THE GET EXISTS: session_id is persisted in the browser's localStorage, so a
page reload resumes the SAME conversation — same id, therefore the same
checkpointer thread, so the agent keeps its full context. This endpoint rebuilds
what the customer can SEE (the text turns); the model's own memory rides in the
checkpoint and needs nothing from here.

KNOWN PRODUCTION GAP (D1, accepted deliberately for the demo)
-------------------------------------------------------------
The GET serves a full transcript to anyone holding the session uuid, with no
further check. That is safe enough HERE — session ids are unguessable uuid4, the
data is fictitious, and the demo is short-lived — and it is the same demo-only
relaxation already accepted for the reschedule/cancel booking lookup. In
production the session must be bound to an authenticated identity before its
transcript is served. Logged on the D1 hardening checklist, not forgotten.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Actor
from app.services import conversation

router = APIRouter(prefix="/api/conversation")


@router.get("/{session_id}")
def get_conversation(session_id: str, db: Session = Depends(get_db)) -> dict:
    """This session's transcript and status. Read-only; never creates anything.

    An unknown session_id is NOT an error: it returns an empty `turns` list, and
    the frontend treats that as "unusable, start fresh" (see the zero-turns guard
    in useChat). A 404 would say the same thing in a way every caller then has to
    special-case, and this is a resume hint, not a resource lookup.
    """
    return conversation.history(db, session_id=session_id)


@router.post("/{session_id}/close")
def close_conversation_route(session_id: str, db: Session = Depends(get_db)) -> dict:
    """End this conversation from the UI ("New chat"), skipping the LLM.

    Goes through the SAME service as the agent's close_conversation tool, which
    is what guarantees the two exits behave identically — in particular that a
    New chat pressed mid-booking releases the held slot instead of stranding it
    for the lock TTL. Actor.customer because a person pressed the button; the
    audit trail should not credit that to Aisha.
    """
    result = conversation.close(db, session_id=session_id, actor=Actor.customer)
    db.commit()  # the route owns the transaction; the service only flushes
    return result
