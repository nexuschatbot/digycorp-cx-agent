"""Vehicle detail for the UI dialog — the DIRECT path (AD-I), no LLM involved.

    GET /api/vehicles/{vehicle_id}  -> full detail, both languages (read-only)

WHY THIS EXISTS. Tapping "View more" on a card used to send a chat message, so
the agent spent a whole turn fetching detail the browser had already been given
and thrown away. Browsing is not a conversation: the answer is fully determined
by the id, so it never needed a language model. This is the same two-caller
pattern as conversation.py — a structured intent posted straight to REST — and
the rule it follows is: a tap that only SHOWS something costs nothing; a tap
that starts something (Book test drive) is where the agent speaks.

WHY REST AND NOT THE TOOL RESULT. The Arabic bullets are deliberately absent
from _vehicle_dict — carrying both languages would grow every search_vehicles
payload by ~29% in tokens the model never speaks, since it picks two or three
bullets and phrases them itself. They live here instead, where they are free.

It also fixes a real gap: GET /api/conversation/{id} does not replay tool_events
(a rehydrated chip would pin a slot_id that may since have been booked), so
after a page reload the cards lose their data. The dialog can re-fetch.

GROUNDING IS UNCHANGED (rule 18, FR-CX-07). Every field is read from the row —
the image URL is derived from the stored path, an unpublished price stays null
and must render as no price line. The UI shows data; it does not narrate. Only
the agent makes claims, and it still gets its facts from the tools.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import catalogue

router = APIRouter(prefix="/api/vehicles")


@router.get("/media-manifest")
def media_manifest(db: Session = Depends(get_db)) -> dict:
    """The nine hero photo URLs, so the client can warm its image cache at boot.

    DECLARED BEFORE /{vehicle_id} ON PURPOSE. FastAPI matches in declaration
    order, and "media-manifest" against an int path parameter is a 422, not a
    fall-through — a route that works only because of where it sits in the file
    deserves the note.

    NOT A CATALOGUE ENDPOINT. It returns URLs and nothing else: no name, no
    price, no id. Anything a customer READS still comes from the agent's tools
    or the detail GET, so this cannot become a second, unnarrated source of
    catalogue facts (rule 18).
    """
    return {"hero_image_urls": catalogue.hero_image_urls(db)}


@router.get("/{vehicle_id}")
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)) -> dict:
    """Full detail for one model, or 404.

    404 rather than an empty body on purpose: unlike the conversation GET —
    where an unknown session means "rotate to a fresh one" and must not look
    like an error — an unknown vehicle id is a genuine client bug. The dialog
    should surface nothing rather than an empty shell.
    """
    detail = catalogue.detail_for_dialog(db, vehicle_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="vehicle_not_found")
    return detail
