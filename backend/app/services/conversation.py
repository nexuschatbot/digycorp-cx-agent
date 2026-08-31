"""Conversation lifecycle service — closing a chat (behaviour rule 19).

Closing is an ACTION, not a UI gesture. A conversation that ended should be as
auditable as one that booked (rule 15, FR-AD-02), so the close runs through the
service layer like every other write: it leaves an `intent` row in the trail with
the same correlation_id as the choke-point `tool_call`, and the console gets
close-events for free.

NO SERVER-SIDE TIMER (rule 19). The backend acts only on inbound messages: this
service runs when the agent calls close_conversation on a turn the customer sent.
The other exit — the ~3-minute idle close — is FRONTEND-owned by design (a canned
client-side line cannot hang mid-demo, and an abandoned session should not
trigger a round-trip). Nothing here polls, schedules, or expires anything.

WHY THE AUDIT TRAIL IS THE STATE
--------------------------------
There is no Conversation model — `session_id` is a bare string (the checkpointer
thread_id, the ConversationTurn key, the AuditEvent session, the slot lock
owner). Rather than invent a table for one boolean, "has this session closed?" is
answered from the append-only trail that already records it. The trail is the
evidence; reading the fact from the same place we wrote it keeps one source of
truth. If a Conversation row ever lands (persisted status for the console), this
function is the only place that changes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Actor, AuditEvent, AuditType, ConversationTurn
from app.services import audit, slots

# The payload marker for a close, in the `intent` audit row. One constant so the
# writer below and the idempotency read below can never drift apart.
CLOSE_ACTION = "conversation_closed"


def is_closed(db: Session, *, session_id: str) -> bool:
    """Has this session already been closed? Read from the audit trail.

    Scans this session's `intent` rows in Python rather than querying into the
    JSON payload — a session holds a handful of them, and a portable read beats a
    SQLite-vs-Postgres JSON dialect split (AD: DATABASE_URL swaps between them).
    """
    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == session_id,
            AuditEvent.type == AuditType.intent,
        )
    ).all()
    return any((r.payload or {}).get("action") == CLOSE_ACTION for r in rows)


def history(db: Session, *, session_id: str) -> dict:
    """This session's transcript, oldest first, plus whether it has closed.

    READ-ONLY — no side effects, deliberately. It backs page-reload resume: the
    frontend persists session_id in localStorage and rehydrates the thread from
    here on load.

    TEXT + VEHICLE-CARD ANCHORS. ConversationTurn is the human-readable
    transcript (native script, per-turn language), and each agent turn also
    carries the vehicle cards it rendered — as bare ids (see
    ConversationTurn.cards), which the frontend resolves through
    GET /api/vehicles/{id}. So a reload redraws the thread as the customer left
    it, cars included.

    SLOT CHIPS ARE STILL NEVER REPLAYED, and the asymmetry is the point. A
    rehydrated chip pins a slot_id that may have been booked by someone else in
    the meantime — the same staleness the "only the newest offer is tappable"
    rule already exists to prevent. A vehicle_id has no such failure mode: the
    catalogue row is still there, and it is re-read live rather than restored
    from a frozen copy. Chips regenerate on the next relevant turn.

    NOTE THE MODEL'S MEMORY IS ELSEWHERE. This is the transcript, not the
    checkpoint: the agent resumes its own context from the LangGraph checkpointer
    (thread_id = session_id), which holds the tool_use/tool_result blocks this
    never sees. The two must be cleared together — see `python -m app.cli
    reset-demo`, and the zero-turns guard in the frontend for stragglers.
    """
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.session_id == session_id)
        .order_by(ConversationTurn.id)
    ).all()

    # Human takeover (2026-08-31): a reload must rebuild human mode, not just
    # the bubbles — `escalated` tells the client to reopen its takeover WS
    # listener and hold the idle ladder; `takeover` tells it a person already
    # holds the session (whose name the staff bubbles carry). Imported here
    # rather than at module top: takeover imports this module for is_closed,
    # and the late import keeps the cycle one-way at import time.
    from app.services import takeover as takeover_service

    return {
        "session_id": session_id,
        "status": "closed" if is_closed(db, session_id=session_id) else "active",
        "escalated": takeover_service.is_escalated(db, session_id=session_id),
        "takeover": takeover_service.state(db, session_id=session_id),
        "turns": [
            {
                # The handle read-aloud points at (2026-07-21). Exposed so the
                # client can ask "read turn 41" rather than sending text back —
                # what Aisha said is ours to look up, not the client's to assert.
                "turn_id": t.id,
                "speaker": t.speaker.value,
                "lang": t.lang.value,
                "text": t.text,
                "ts": t.ts.isoformat() if t.ts else None,
                # Always present, [] when the turn drew none, so the client never
                # has to distinguish "no cards" from "old row written before this
                # column existed".
                "cards": t.cards or [],
                # The recording this turn was spoken as, or null when typed
                # (voice notes, 2026-07-21). A POINTER, like `cards`: the client
                # rebuilds the player through GET /api/audio/{id}, which
                # re-applies the session gate and the retention check on every
                # reload. An id that has expired or been erased 404s and the
                # player simply vanishes, leaving the transcript text — the same
                # "drop it, never render a shell" rule the cards follow.
                "audio_id": t.audio_id,
            }
            for t in turns
        ],
    }


def close(
    db: Session,
    *,
    session_id: str,
    correlation_id: str | None = None,
    actor: Actor = Actor.agent,
) -> dict:
    """Close this conversation. Never raises; failure comes back as data.

    Two things happen, in this order:

    1. STATE IS NOT STRANDED. A clean close leaves clean inventory. If the
       customer got as far as accepting a slot and then walked away from the
       booking ("actually never mind, thanks — bye"), that slot is `locked` to
       this session and would otherwise sit out the full TTL, invisible to every
       other customer for no reason. Closing releases it immediately.
       release_lock is scoped to `session_id` (injected, never modelled), so a
       close can only ever free THIS session's hold — never anyone else's.

    2. IT IS AUDITED (rule 15). One `intent` row, carrying how many locks the
       close returned to inventory, correlated with the tool_call.

    IDEMPOTENT, like every other write here: closing an already-closed
    conversation returns the current state, not an error (LLMs retry). The repeat
    releases nothing — the first close already did — and appends no second row,
    so the trail shows one close per conversation rather than a stutter.

    WHO CLOSED IT (`actor`). Two callers, two honest answers: the agent path
    (close_conversation, after its farewell) is Actor.agent, and the direct path
    — the customer tapping "New chat", which skips the LLM entirely — is
    Actor.customer. The trail should say who ended the conversation, not assume
    it was always Aisha.
    """
    if is_closed(db, session_id=session_id):
        return {"closed": True, "already_closed": True, "released_locks": 0}

    released = slots.release_lock(db, session_id=session_id)

    audit.record(
        db,
        session=session_id,
        actor=actor,
        type=AuditType.intent,
        payload={"action": CLOSE_ACTION, "released_locks": released},
        correlation_id=correlation_id,
    )

    return {"closed": True, "already_closed": False, "released_locks": released}


__all__ = ["CLOSE_ACTION", "close", "history", "is_closed"]
