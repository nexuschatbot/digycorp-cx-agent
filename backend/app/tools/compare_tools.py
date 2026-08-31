"""compare_vehicles — two Jetour models side by side, on the facts.

WHY A TOOL AND NOT PROSE. Aisha can already describe two cars in a paragraph, and
that is exactly the problem: a customer choosing between the X70 FL and the X70
Plus has to hold six numbers in their head and diff them. A table does the
diffing. The tool exists so the UI has something structured to draw, and so the
figures come from the catalogue rather than from the model's memory of a card it
rendered four turns ago.

FACTS, NEVER A VERDICT. It returns rows and a `differs` flag per row, and no
"winner", no score, no recommendation — deliberately. A comparison that always
concludes the more expensive car is better is not a comparison, and a customer
spots that in one exchange. Where a real difference matters, Aisha says so in her
own words from these rows; what she must not do is announce an overall winner,
because the catalogue does not know who is asking or what they need.

TWO MODELS, NOT THREE. Three columns on a phone is a horizontal scroll and a
squint, and the decision a customer is actually stuck on is almost always
binary — this one or that one. Asking for a third is a signal they are still
browsing, which is what search_vehicles and the cards are for.

SAME-BRAND ONLY, TODAY. Every figure here comes from our own catalogue, so
nothing is asserted about anyone else's car. Comparing against another
manufacturer is a different feature with a different risk profile — it needs
figures Elite Group has approved and a source-and-date line — and it is
deliberately not smuggled in here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import VehicleModel
# THE SAME URL BUILDER THE CARDS USE. Reading v.images[0] directly gave a raw
# seed path ("seed/media/vehicles/jet-x70fl.jpeg") which renders as a broken
# image — the catalogue already owns turning that list into something a
# browser can fetch, and two ways of doing it is one way to get it wrong.
from app.services.catalogue import _image_url

# The rows a customer actually decides on. Ordered by how often they settle it:
# money, then who fits, then running costs, then the shape of the thing.
_FUEL = {"petrol": "Petrol", "phev": "Plug-in hybrid"}

COMPARE_VEHICLES_SCHEMA = {
    "name": "compare_vehicles",
    "description": (
        "Put two Jetour models side by side — price, seats, powertrain, body "
        "type and what stands out about each.\n\n"
        "Use it the moment a customer is weighing two cars against each other: "
        "'what's the difference', 'which of those two', 'X70 FL or X70 Plus?', "
        "or when they have gone quiet between two you have shown them. Offer it "
        "yourself, once, when somebody is clearly stuck between two.\n\n"
        "If you already have both vehicle_ids from earlier tool results, pass "
        "those. If you do not — the customer has just named two cars — pass "
        "`names` instead and let this resolve them. DO NOT look each model up "
        "separately first: that draws a card for each on top of the table, so "
        "the customer sees the same two cars three times.\n\n"
        "WHEN IT COMES BACK, SAY WHAT ACTUALLY SEPARATES THEM, in one or two "
        "sentences, using the rows marked as differing. The table shows "
        "everything; your job is to name the one or two things that would "
        "change someone's mind.\n\n"
        "NEVER DECLARE A WINNER. Do not say one is better, do not rank them, "
        "and do not push the dearer one. You do not know their budget, their "
        "family or their commute unless they told you — and if they did, say "
        "which suits THAT, and why, and stop there. If they ask outright which "
        "they should buy, it is fair to say it depends on what matters most to "
        "them, ask the one question that would settle it, or offer to have a "
        "colleague call them.\n\n"
        "Two models only. For a third, show cars instead and let them narrow."
    ),
    "input_schema": {
        "type": "object",
        "required": ["vehicle_ids"],
        "properties": {
            "vehicle_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Two vehicle_ids from tool results you already have. Use "
                    "this when you have them."
                ),
            },
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "The two model names AS THE CUSTOMER SAID THEM, when you do "
                    "not already have their ids. Use this instead of looking "
                    "each one up first — one call, one table."
                ),
            },
        },
    },
}


def _price(v: VehicleModel) -> str:
    lo, hi = v.price_aed_min, v.price_aed_max
    if lo and hi and lo != hi:
        return f"AED {lo:,}–{hi:,}"
    return f"AED {lo:,}" if lo else "On request"


def _seats(v: VehicleModel) -> str:
    lo, hi = v.seats_min, v.seats_max
    if lo and hi and lo != hi:
        return f"{lo}–{hi}"
    return str(lo) if lo else "—"


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def compare_vehicles(
    db: Session,
    session_id: str,
    vehicle_ids: object = None,
    names: object = None,
    **_ignored: object,
) -> dict:
    """Two vehicles as labelled rows, with the differing ones flagged. Read-only.

    Accepts ids OR names — names so a cold "compare the X70 FL and the X70
    Plus" is a single call rather than two lookups and a table.

    `differs` is computed HERE rather than left to the model, because "are these
    the same?" is a string comparison and the model getting it wrong is how a
    table ends up highlighting a row that is identical in both columns.
    """
    ids = [_coerce_int(x) for x in (vehicle_ids or [])]
    ids = [i for i in ids if i is not None]

    # NAMES ARE RESOLVED HERE so comparing is ONE call. Looking each model up
    # separately first drew a vehicle card per lookup ON TOP of the table — the
    # same two cars, three times, which is what the first recording showed.
    if len(ids) != 2 and names:
        resolved: list[int] = []
        for raw in list(names)[:2]:
            needle = str(raw or "").strip().lower()
            if not needle:
                continue
            hits = [
                v for v in db.query(VehicleModel).all()
                if needle in (v.name or "").lower()
                or needle in (v.model_code or "").lower()
            ]
            # An ambiguous half-name ("T2" matches the T2 and the T2 i-DM) is
            # not something to guess at inside a comparison: say so and let the
            # agent ask, exactly as get_vehicle_details does.
            if len(hits) != 1:
                return {
                    "error": "ambiguous_or_unknown_name",
                    "name": raw,
                    "matched": [v.name for v in hits],
                    "detail": (
                        "That name matches more than one model, or none. Ask "
                        "which they mean, then compare."
                    ),
                }
            resolved.append(hits[0].id)
        ids = resolved

    if len(ids) != 2 or ids[0] == ids[1]:
        return {
            "error": "need_two_distinct_vehicles",
            "detail": (
                "Pass two different vehicle_ids, or two model names as the "
                "customer said them."
            ),
        }

    left, right = (db.get(VehicleModel, i) for i in ids)
    missing = [i for i, v in zip(ids, (left, right)) if v is None]
    if missing:
        return {
            "error": "vehicle_not_found",
            "vehicle_ids": missing,
            "detail": "Look the models up first and pass the ids those results carry.",
        }

    rows = []
    for label, fn in (
        ("Price", _price),
        ("Seats", _seats),
        ("Powertrain", lambda v: _FUEL.get(getattr(v.fuel_type, "value", v.fuel_type), "—")),
        ("Body type", lambda v: v.body_type or "—"),
    ):
        a, b = fn(left), fn(right)
        rows.append({"label": label, "left": a, "right": b, "differs": a != b})

    return {
        "compared": True,
        "left": {
            "vehicle_id": left.id,
            "name": left.name,
            "image_url": _image_url(left.images),
            "features": [str(f) for f in (left.headline_features or [])][:4],
        },
        "right": {
            "vehicle_id": right.id,
            "name": right.name,
            "image_url": _image_url(right.images),
            "features": [str(f) for f in (right.headline_features or [])][:4],
        },
        "rows": rows,
        # Named for the agent's convenience so she does not have to re-scan the
        # rows to find what to talk about. Empty means two genuinely similar
        # cars — which is itself worth saying out loud.
        "differences": [r["label"] for r in rows if r["differs"]],
    }
