"""Booking detail for the UI card — the DIRECT path (AD-I), no LLM involved.

    GET /api/booking/{ref}?session_id=<uuid>  -> one booking (read-only)

WHY THIS EXISTS. GET /api/conversation/{id} replays a turn's CARD ANCHORS, not a
frozen copy of the cards (see ConversationTurn.cards): the transcript records
that a turn drew booking TD-2026-0142, and the client rebuilds the card live.
That needs somewhere to rebuild it FROM. This is the booking equivalent of
GET /api/vehicles/{id}, and it exists for the same reason — a reload must redraw
what the conversation actually showed, without spending an agent turn to do it.

WHY THE ANCHOR IS A REF AND NOT A SNAPSHOT — and why that matters more here than
it does for vehicles. A vehicle snapshot would merely go stale (a stored English
name cannot be redrawn in an Arabic thread; a stored price ages). A BOOKING
snapshot would additionally freeze a customer's NAME and MOBILE into the
transcript table — data that no gate is ever consulted about again. Re-fetching
means services/booking._booking_dict re-applies its PII gate on every reload,
forever. The rule that keeps display text out of data fields and the rule that
keeps personal data behind identification happen to want the same design here.

SESSION_ID IS THE VIEWER, NOT THE KEY. `ref` alone selects the booking, exactly
as the agent's get_booking tool does (FR-RS-01: ref OR mobile, either alone is
enough). `session_id` decides only WHOSE EYES this is through, and it is read
from the LangGraph checkpoint — never trusted from the query string beyond
naming the thread whose state to load. A client that invents a session_id gets
whatever that session actually identified, which for a made-up one is nobody,
which means no PII. The same reasoning as api/intents.py: an id supplied by the
caller is an assertion, and only the checkpoint turns it into a fact.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agent import build_graph, new_sqlite_checkpointer
from app.config import AGENT_CHECKPOINT_PATH
from app.db import get_db
from app.services import booking as booking_service

router = APIRouter(prefix="/api/booking")


def _viewer_customer_id(session_id: str | None) -> int | None:
    """The session's identified customer, from the checkpoint. None if the
    session is unknown, unidentified, or absent — all of which are ordinary
    states, not errors: the card simply renders without the personal fields."""
    if not session_id:
        return None
    try:
        graph = build_graph(new_sqlite_checkpointer(AGENT_CHECKPOINT_PATH))
        snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
        state = snapshot.values if snapshot else {}
    except Exception:  # noqa: BLE001 — no checkpoint is "no viewer", never a 500
        return None
    return state.get("customer_id") if isinstance(state, dict) else None


@router.get("/{ref}")
def get_booking_detail(
    ref: str,
    session_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """One booking by TD reference, or 404.

    404 rather than an empty body, matching GET /api/vehicles/{id}: a card
    anchored on a ref that no longer resolves must render NOTHING, never a
    shell (FR-CX-07). A cancelled booking is exactly that case — it is no
    longer active, so it 404s here and the card silently disappears from the
    rebuilt thread, which is the honest outcome.
    """
    result = booking_service.find(
        db, ref=ref, viewer_customer_id=_viewer_customer_id(session_id)
    )
    bookings = result.get("bookings") if isinstance(result, dict) else None
    if not bookings:
        raise HTTPException(status_code=404, detail="booking_not_found")
    return bookings[0]
