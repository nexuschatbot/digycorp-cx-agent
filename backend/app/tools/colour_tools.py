"""get_colours (read-only) for the SALES sub-agent.

A thin wrapper over services/stock.colours_available, in the house style. The
service existed, fully tested, since T0.8 — and was called by nothing but its
own tests, so "what colours does the X50 come in?" had no tool and was answered
"we don't hold that" live (2026-09-01) while both halves of the answer sat in
the database. check_stock never covered this: it names colours only on its
colour_not_offered path, so the open question could only be answered by
guessing a wrong colour first.

SUB-AGENT TOOL, NOT AISHA'S. Bound through SUBAGENT_REGISTRY and deliberately
absent from REGISTRY, so TOOL_SCHEMAS and the single agent's cached prefix stay
byte-stable while the single-agent path is live. See finance_tools.py.

NO UNIT COUNTS EVER LEAVE THE SERVICE — same contract as check_stock. Offered
and available are lists of colour names, never quantities.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import stock

GET_COLOURS_SCHEMA = {
    "name": "get_colours",
    "description": (
        "Which exterior colours one car comes in, and which of those can be "
        "had right now. Use it for the OPEN colour question — 'what colours "
        "does it come in', 'how many colours are there', 'what colours do you "
        "have it in' — where the customer has not named a colour. When they "
        "HAVE named one ('do you have it in black at Deira'), check_stock is "
        "the tool.\n\n"
        "PASS variant_id FOR A TRIM, model_id FOR A WHOLE MODEL. Pass one or "
        "the other, not both.\n\n"
        "Read the response:\n"
        "- 'offered' is every colour Elite makes it in — the palette. That is "
        "the answer to 'what colours does it come in'.\n"
        "- 'available' is the subset in stock right now (at the one branch, "
        "if showroom_id was passed). 'We make it in six, four are on the "
        "ground today' is both lists in one honest sentence.\n"
        "- 'status': 'coming_soon' means the car is not orderable yet and "
        "both lists are empty — that is a fact about the car, not missing "
        "data.\n"
        "- These are EXTERIOR colours. Interior palettes come from "
        "get_vehicle_details, are model-level, and are never paired with an "
        "exterior colour.\n"
        "- NO UNIT COUNTS ARE RETURNED, by design. Report colours and "
        "presence, never how many."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variant_id": {
                "type": "integer",
                "description": (
                    "One trim's id, from get_variants. Use when the customer "
                    "has settled on a specific version."
                ),
            },
            "model_id": {
                "type": "integer",
                "description": (
                    "A model's vehicle id, to answer across all its trims. "
                    "Use for 'what colours does the G700 come in'."
                ),
            },
            "showroom_id": {
                "type": "integer",
                "description": (
                    "Restrict 'available' to one branch, only if the customer "
                    "named one. 'offered' is branch-independent either way."
                ),
            },
        },
        "required": [],
    },
}


def get_colours(
    db: Session,
    session_id: str,
    *,
    variant_id=None,
    model_id=None,
    showroom_id=None,
    **_,
) -> dict:
    """The colour palette and its current availability. Never raises."""
    if variant_id is None and model_id is None:
        return {
            "error": "missing_vehicle",
            "detail": "Pass a variant_id for one trim, or a model_id for a model.",
        }

    def _int_or_none(value, field):
        if value is None:
            return None, None
        try:
            return int(value), None
        except (TypeError, ValueError):
            return None, {"error": f"invalid_{field}", field: value}

    vid, err = _int_or_none(variant_id, "variant_id")
    if err:
        return err
    mid, err = _int_or_none(model_id, "model_id")
    if err:
        return err
    sid, err = _int_or_none(showroom_id, "showroom_id")
    if err:
        return err

    return stock.colours_available(
        db, variant_id=vid, model_id=mid, showroom_id=sid
    )
