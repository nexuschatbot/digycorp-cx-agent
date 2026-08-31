"""check_stock (read-only) for the SALES sub-agent.

A thin wrapper over services/stock.availability, in the house style. All the
inventory logic — trim aggregation, colour resolution, coming-soon before
out-of-stock — lives in the service and is tested there.

SUB-AGENT TOOL, NOT AISHA'S. Bound through SUBAGENT_REGISTRY and deliberately
absent from REGISTRY, so TOOL_SCHEMAS and the single agent's cached prefix stay
byte-stable while the single-agent path is live. See finance_tools.py.

NO UNIT COUNTS EVER LEAVE THE SERVICE, and the tool must not reintroduce them.
`availability` converts counts to the FACT of presence and returns showroom
lists, never numbers. "We have two left" is a promise about inventory that moves
between the answer and the visit; "Deira has it" is not.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import stock

CHECK_STOCK_SCHEMA = {
    "name": "check_stock",
    "description": (
        "Where a car can actually be had right now, and where it cannot. Use "
        "it when the customer asks whether something is available, in stock, "
        "at a particular branch, or in a particular colour.\n\n"
        "PASS variant_id FOR A TRIM, model_id FOR A WHOLE MODEL. A model-level "
        "question aggregates its trims and the answer names which ones are "
        "available. Pass one or the other, not both.\n\n"
        "Read the response:\n"
        "- 'status': 'available' means at least one branch has it; "
        "'out_of_stock' means none does; 'coming_soon' means the car is not "
        "orderable at all yet, which is different from being sold out and must "
        "not be reported as 'out of stock'.\n"
        "- 'showrooms' is where it IS. 'not_at' is where it is NOT. Both "
        "matter: 'not at Deira, but Abu Dhabi has it' is one answer, and you "
        "can only give it because both lists come back.\n"
        "- 'variants_available' names the trims in stock, for a model-level "
        "question.\n"
        "- 'status': 'colour_not_offered' means we do not make it in that "
        "colour; 'available_colours' then lists what we do offer, so the "
        "finding ends with a choice rather than a dead end.\n"
        "- NO UNIT COUNTS ARE RETURNED, by design. Report presence and branch, "
        "never a quantity, and never say how many are left."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variant_id": {
                "type": "integer",
                "description": (
                    "One trim's id, from get_variants. Use for 'is the T2 "
                    "Luxury Plus available'."
                ),
            },
            "model_id": {
                "type": "integer",
                "description": (
                    "A model's vehicle id, to ask across all its trims. Use "
                    "for 'do you have the G700'."
                ),
            },
            "colour": {
                "type": "string",
                "description": (
                    "Exterior colour as the customer said it. Omit unless they "
                    "named one; it is matched against the colours we offer."
                ),
            },
            "showroom_id": {
                "type": "integer",
                "description": (
                    "Restrict to one branch, only if the customer named one. "
                    "Omit to see every branch, which is usually better."
                ),
            },
        },
        "required": [],
    },
}


def check_stock(
    db: Session,
    session_id: str,
    *,
    variant_id=None,
    model_id=None,
    colour=None,
    showroom_id=None,
    **_,
) -> dict:
    """Where a car is in stock, and where it is not. Never raises."""
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

    text = (colour or "").strip() or None

    return stock.availability(
        db, variant_id=vid, model_id=mid, colour=text, showroom_id=sid
    )
