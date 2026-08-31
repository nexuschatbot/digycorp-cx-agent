"""get_accessories (read-only) for the SALES sub-agent.

A thin wrapper over services/accessories.catalogue, in the house style — see
stock_tools.py and finance_tools.py.

WHY IT IS NEW (2026-08-31). The table has been seeded since T0.7 and no tool
ever read it. Live, a customer asked what the alloy wheel upgrade costs and was
told we did not hold that; the row was in the database the whole time with 4500
in it. This is the wiring that was missing, not new data.

SUB-AGENT TOOL, NOT AISHA'S. Bound through SUBAGENT_ONLY and deliberately
absent from REGISTRY, so TOOL_SCHEMAS and the single agent's cached prefix stay
byte-stable while the single-agent path is live.

IT IS SALES, NOT FINANCE, and the split is the same one the cards already draw:
an accessory is a thing on the price list, like a trim or a colour. What a car
costs to OWN — on-road price, instalments, banks — is Finance's. An accessory
price is not folded into either, and nothing here computes a total.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import accessories

GET_ACCESSORIES_SCHEMA = {
    "name": "get_accessories",
    "description": (
        "The accessories Elite sells and what each one costs. Use it for "
        "'what accessories do you have', 'how much is window tinting', 'can I "
        "add alloy wheels', tinting, ceramic coating, paint protection film, "
        "floor mats, or any add-on priced separately from the car.\n\n"
        "TAKES NO ARGUMENTS BY DEFAULT and returning the whole list is normal — "
        "there are only a handful. Pass model_id ONLY to narrow to what fits "
        "one model; the universal accessories come back either way.\n\n"
        "Read the response:\n"
        "- 'accessories' is one entry per part: name, description, price_aed, "
        "and model_specific.\n"
        "- 'description' is what the thing actually IS. Use it — three of the "
        "names mean nothing on their own, and a price beside a bare name reads "
        "as arbitrary.\n"
        "- 'part_code' is Elite's SKU. NEVER say it to a customer; it is here "
        "only to match the codes promotions name (\"free window tinting "
        "(ACC-002)\").\n"
        "- 'prices_exclude_fitting': true. The figure is the part. Nothing in "
        "Elite's data says whether fitting is included, so never imply it is.\n"
        "- An empty list means we publish no accessories — say so; do not "
        "estimate a price for one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "model_id": {
                "type": "integer",
                "description": (
                    "A model's id, to narrow to what fits that car. Not a "
                    "variant_id. Omit for the whole list, which is usually "
                    "what you want."
                ),
            }
        },
        "required": [],
    },
}


def get_accessories(db: Session, session_id: str, *, model_id=None, **_) -> dict:
    """Elite's priced accessories. Never raises."""
    if model_id is None:
        return accessories.catalogue(db)
    try:
        mid = int(model_id)
    except (TypeError, ValueError):
        return {"error": "invalid_model_id", "model_id": model_id}
    return accessories.catalogue(db, model_id=mid)
