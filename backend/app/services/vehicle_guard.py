"""Refuse a vehicle_id the conversation never asked for (2026-09-03).

WHAT WENT WRONG, LIVE. A customer opened with "i want to book a test drive of
g700" and every tool call that followed carried `vehicle_id: 6` — the Jetour
Dashing. The G700 is 1. Nothing had ever resolved the name to an id: the
catalogue tools were never called that session, and the assembled system prompt
carries no id anywhere (66,921 characters, and `vehicle_id` never appears beside
a digit). The number was invented, `nearest_showroom` ranked branches stocking
the wrong car, and `get_available_slots` returned three Dashing slots — which
the frontend drew as chips under a sentence saying there were no G700 slots.

THE SCHEMAS ALREADY SAY NOT TO. get_available_slots' vehicle_id reads "Get this
from search_vehicles or get_vehicle_details first — never guess", and variant_id
says it again. Both were ignored. A rule the model can silently skip is not a
guard; a tool that refuses is. That is the whole argument for putting this in
the tool layer rather than in more prompt prose.

THIS REFUSES; IT NEVER CHOOSES. It would be a two-line change to read the name
out of the transcript and substitute the right id, and that is exactly what
retrieval/vehicle_mentions forbids: "a HINT for a retrieval query, NOT a filter
… nothing here chooses a car for the customer." Choosing needs judgement a regex
does not have — "a 7-seater for my family" is a search, not a name match — and a
matcher that quietly picked cars would book somebody into the wrong test drive
with nobody able to see it happen. A refusal is visible, recoverable, and cannot
book anything. So the answer is "this contradicts the conversation, go and look
it up", never "I fixed it for you".

DELIBERATELY PERMISSIVE: it fires only when NOBODY — customer or Aisha — has
named that car anywhere in the session. Customers switch cars mid-conversation
("actually, show me the T2"), Aisha names cars while comparing them, and a guard
that fired on a legitimate switch would be worse than the bug it prevents. The
set is every model named all session, not the latest one. On the live failure
that is still decisive: "Dashing" appears nowhere in that transcript.

SILENT ON ANYTHING IT CANNOT PROVE. No turns, no names recognised, an id that
does not parse or does not exist — every one of those returns None and leaves
the existing validators (invalid_vehicle_id, vehicle_not_found) to answer. A
guard that guesses is the thing being fixed.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import ConversationTurn, VehicleModel

log = logging.getLogger("app.services.vehicle_guard")

# Turns scanned back from the newest. A whole session is normally far shorter;
# the cap only bounds the query on a long one. Larger than
# vehicle_mentions._MAX_LOOKBACK on purpose — that answers "which car are we
# talking about NOW", this answers "was this car ever mentioned at all".
MAX_TURNS = 60


def _discussed_names(db, session_id: str) -> set[str]:
    """Catalogue model names anyone named this session, newest turns first.

    THE NAMES COME FROM `db`, NOT FROM THE PROCESS CATALOGUE (found in CI,
    2026-09-03). vehicle_mentions.mentions() defaults to opening its own
    SessionLocal, and where there is no database to open it matches nothing and
    says so by returning [] — which for a retrieval hint is a harmless
    degradation and for this guard is the difference between guarding and
    silently not. Locally it passed on the developer's own elite_demo.db; in CI
    all three guard assertions failed. We already hold the Session the write is
    happening in, so it is the one thing here that is certainly right.
    """
    from app.retrieval import vehicle_mentions

    names = vehicle_mentions.model_names(db)
    if not names:
        return set()

    rows = db.scalars(
        select(ConversationTurn.text)
        .where(ConversationTurn.session_id == session_id)
        .order_by(ConversationTurn.id.desc())
        .limit(MAX_TURNS)
    ).all()
    found: set[str] = set()
    for text in rows:
        if text:
            found.update(vehicle_mentions.mentions(text, names=names))
    return found


def contradiction(db, session_id: str, vehicle_id) -> dict | None:
    """`{passed, passed_name, discussed}` when `vehicle_id` names a car nobody
    in this session ever mentioned, else None.

    NEVER RAISES. This runs inside a read the customer is waiting on, and a
    guard that breaks the turn it was meant to protect is a worse bug than the
    one it catches — any failure returns None and the read proceeds unchanged.
    """
    try:
        if vehicle_id is None or not session_id:
            return None
        try:
            vid = int(vehicle_id)
        except (TypeError, ValueError):
            return None          # invalid_vehicle_id already covers this
        vehicle = db.get(VehicleModel, vid)
        if vehicle is None or not vehicle.name:
            return None          # vehicle_not_found already covers this

        discussed = _discussed_names(db, session_id)
        if not discussed or vehicle.name in discussed:
            return None

        log.warning(
            "vehicle_id %s (%s) contradicts the conversation (discussed: %s) "
            "— session=%s",
            vid, vehicle.name, sorted(discussed), session_id,
        )
        return {
            "passed": vid,
            "passed_name": vehicle.name,
            "discussed": sorted(discussed),
        }
    except Exception:  # noqa: BLE001 — a guard must never cost the read
        log.warning("vehicle guard failed; allowing the read", exc_info=True)
        return None


def detail(clash: dict) -> str:
    """The recovery sentence handed to the agent.

    LEADS WITH WHAT TO DO. The gate keeps the precise reason in its own fields;
    this is the part that gets acted on, and the same lesson as
    orchestrator/write's `next_step` — told only what was wrong, the agent
    announces a technical problem and escalates a customer who was ready to
    book.
    """
    wanted = clash["discussed"]
    names = wanted[0] if len(wanted) == 1 else " or ".join(
        (", ".join(wanted[:-1]), wanted[-1])
    )
    return (
        f"vehicle_id {clash['passed']} is the {clash['passed_name']}, which "
        f"nobody has mentioned. This conversation has been about the {names}. "
        f"Call get_vehicle_details (or search_vehicles) for the right "
        f"vehicle_id and try again — do NOT tell the customer anything is "
        f"unavailable."
    )
