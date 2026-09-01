"""Stock service: what is on the ground, where, and in which colour (T0.8).

THE ONE RULE THIS MODULE IS BUILT AROUND: IT NEVER RETURNS A COUNT.

Not as a policy the agent is asked to remember — as a shape it cannot get one
from. `Stock.units` is read here and converted to a boolean before it leaves;
no function below puts an integer count in its return value, and
tests/test_stock.py walks every returned structure asserting none appears.

The reason is in Stock's own docstring: nothing in this system decrements
`units` when a car sells, so the numbers are only as fresh as the last import. A
stale "twelve at Deira" is a promise we cannot keep and a customer drives across
town on. A stale "yes, we have it" degrades into a conversation at the showroom,
which is where that conversation belonged anyway.

FOUR STATES, NOT TWO, because "no" means four different things and the customer
needs a different sentence for each:

    available          — it is there; here is where.
    out_of_stock       — we sell it in that colour, none on the ground now.
    colour_not_offered — we do not make it in that colour at all. Asking the
                         customer to wait for it would be a lie.
    coming_soon        — the G700 Top Fire. Not sold out; not yet on sale. It
                         cannot be ordered, and "out of stock" would imply it can.

Collapsing those into a boolean is how an agent ends up telling someone to check
back next week for a colour that does not exist.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Showroom, Stock, VehicleModel, VehicleVariant


def _showroom_brief(s: Showroom) -> dict:
    return {
        "showroom_id": s.id,
        "name": s.name,
        "emirate": s.emirate,
        "city": s.city,
    }


def _variant_brief(v: VehicleVariant) -> dict:
    return {
        "variant_id": v.id,
        "model_id": v.model_id,
        "model_name": v.model.name if v.model else None,
        "variant_name": v.name,
        "coming_soon": bool(v.coming_soon),
    }


# --------------------------------------------------------------------------- #
# Colour resolution
# --------------------------------------------------------------------------- #
def resolve_colour(db: Session, variant_ids: list[int], spoken: str) -> dict:
    """Map what the customer said onto a colour Elite actually sells.

    Customers say "black"; the G700's colour is "Night Black". Exact match first
    (the T1 really does have a plain "Black"), then substring.

    AMBIGUITY IS RETURNED, NOT GUESSED. "grey" on a Dashing matches both Dark
    Grey and Light Grey, and picking one would answer confidently about a car
    the customer did not ask for. The candidates come back so the agent can ask.
    """
    needle = (spoken or "").strip().lower()
    if not needle:
        return {"error": "missing_colour"}

    known = sorted({
        r.colour
        for r in db.scalars(
            select(Stock).where(Stock.variant_id.in_(variant_ids))
        ).all()
    })

    exact = [c for c in known if c.lower() == needle]
    if len(exact) == 1:
        return {"colour": exact[0]}

    partial = [c for c in known if needle in c.lower()]
    if len(partial) == 1:
        return {"colour": partial[0]}
    if len(partial) > 1:
        return {"error": "ambiguous_colour", "query": spoken, "candidates": partial}
    return {"error": "colour_not_offered", "query": spoken, "available_colours": known}


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def _variants_for(
    db: Session, variant_id: int | None, model_id: int | None
) -> list[VehicleVariant] | dict:
    if variant_id is not None:
        v = db.get(VehicleVariant, variant_id)
        if v is None:
            return {"error": "variant_not_found", "variant_id": variant_id}
        return [v]
    if model_id is not None:
        m = db.get(VehicleModel, model_id)
        if m is None:
            return {"error": "vehicle_not_found", "vehicle_id": model_id}
        rows = list(
            db.scalars(
                select(VehicleVariant)
                .where(VehicleVariant.model_id == model_id)
                .order_by(VehicleVariant.id)
            ).all()
        )
        if not rows:
            return {"error": "vehicle_not_found", "vehicle_id": model_id}
        return rows
    return {"error": "missing_query", "detail": "Provide a variant_id or a model_id."}


def availability(
    db: Session,
    *,
    variant_id: int | None = None,
    model_id: int | None = None,
    colour: str | None = None,
    showroom_id: int | None = None,
) -> dict:
    """Where a car can be had, and where it cannot.

    BOTH HALVES ARE RETURNED. `showrooms` is where it is; `not_at` is where it is
    not. The second is what makes "not at Deira, but Abu Dhabi has it" possible
    in one turn — and it is the honest shape for the profile-driven flow, where
    the customer's preferred branch is asked about first and widened only when it
    comes back empty.

    A MODEL-LEVEL QUERY AGGREGATES ITS TRIMS. "Do you have the G700 in black?"
    is a fair question with three trims behind it, and the answer names which.
    """
    variants = _variants_for(db, variant_id, model_id)
    if isinstance(variants, dict):
        return variants

    ids = [v.id for v in variants]
    single = variants[0] if len(variants) == 1 else None

    # Coming soon is checked BEFORE stock, because a coming-soon car has no
    # stock rows at all and would otherwise fall out as "out of stock" — which
    # implies it can be ordered, and it cannot.
    if all(v.coming_soon for v in variants):
        return {
            "status": "coming_soon",
            "variant": _variant_brief(single) if single else None,
            "variants": [_variant_brief(v) for v in variants],
            "colour": None,
            "showrooms": [],
            "not_at": [],
        }

    resolved_colour = None
    if colour is not None:
        r = resolve_colour(db, ids, colour)
        if "error" in r:
            if r["error"] == "colour_not_offered":
                return {
                    "status": "colour_not_offered",
                    "variant": _variant_brief(single) if single else None,
                    "query": colour,
                    # What we DO offer, so the turn ends with a choice rather
                    # than a dead end.
                    "available_colours": r["available_colours"],
                    "showrooms": [],
                    "not_at": [],
                }
            return r
        resolved_colour = r["colour"]

    q = select(Stock).where(Stock.variant_id.in_(ids))
    if resolved_colour is not None:
        q = q.where(Stock.colour == resolved_colour)
    if showroom_id is not None:
        q = q.where(Stock.showroom_id == showroom_id)
    rows = list(db.scalars(q).all())

    if not rows:
        # No ROW at all, as opposed to rows that are all zero. Only reachable
        # via a showroom_id that carries no inventory for this car.
        return {
            "status": "out_of_stock",
            "variant": _variant_brief(single) if single else None,
            "colour": resolved_colour,
            "showrooms": [],
            "not_at": [],
        }

    # THE CONVERSION FROM COUNT TO FACT, and the only place it happens. After
    # this line no number from `units` survives into the return value.
    with_stock: set[int] = {r.showroom_id for r in rows if r.units > 0}
    without: set[int] = {r.showroom_id for r in rows} - with_stock

    showrooms = {
        s.id: s
        for s in db.scalars(
            select(Showroom)
            .where(Showroom.id.in_(with_stock | without))
            .order_by(Showroom.id)
        ).all()
    }

    # Which trims are actually available, for a model-level question.
    trims = sorted(
        {r.variant_id for r in rows if r.units > 0}
    )
    by_id = {v.id: v for v in variants}

    return {
        "status": "available" if with_stock else "out_of_stock",
        "variant": _variant_brief(single) if single else None,
        "variants_available": [
            _variant_brief(by_id[t]) for t in trims if t in by_id
        ],
        "colour": resolved_colour,
        "showrooms": [
            _showroom_brief(showrooms[i]) for i in sorted(with_stock) if i in showrooms
        ],
        "not_at": [
            _showroom_brief(showrooms[i]) for i in sorted(without) if i in showrooms
        ],
    }


def colours_available(
    db: Session,
    *,
    variant_id: int | None = None,
    model_id: int | None = None,
    showroom_id: int | None = None,
) -> dict:
    """Which colours can actually be had, optionally at one branch.

    OFFERED AND AVAILABLE ARE DIFFERENT LISTS, and both are returned. "We do it
    in six colours, four of them are here" is a better answer than either half
    alone, and it is the difference between a customer waiting for a colour and
    a customer discovering it does not exist.

    Interior colours are NOT here: they live on VehicleModel.interior_colours,
    are not paired with the exterior colour, and are not stocked. See that
    column's comment before answering with them.
    """
    variants = _variants_for(db, variant_id, model_id)
    if isinstance(variants, dict):
        return variants

    ids = [v.id for v in variants]
    single = variants[0] if len(variants) == 1 else None

    if all(v.coming_soon for v in variants):
        return {
            "status": "coming_soon",
            "variant": _variant_brief(single) if single else None,
            "offered": [],
            "available": [],
        }

    q = select(Stock).where(Stock.variant_id.in_(ids))
    rows = list(db.scalars(q).all())
    # OFFERED IS THE PALETTE, NOT THE WAREHOUSE (2026-09-01). This used to be
    # the distinct stock colours alone, which quietly redefined "we make it in
    # six colours" as "six colours have ever had a stock row" — a colour Elite
    # offers with no units anywhere simply vanished from the answer. The
    # model-level palette from Elite's Models sheet is the existence claim;
    # stock rows stay the availability claim. Union rather than replacement,
    # so a colour present on the ground but missing from the sheet (importers
    # do that) is still offered — we are demonstrably selling it.
    palette: set[str] = set()
    for m in db.scalars(
        select(VehicleModel).where(VehicleModel.id.in_({v.model_id for v in variants}))
    ).all():
        palette.update(m.exterior_colours or [])
    offered = sorted(palette | {r.colour for r in rows})
    scoped = [r for r in rows if showroom_id is None or r.showroom_id == showroom_id]
    available = sorted({r.colour for r in scoped if r.units > 0})

    out = {
        "status": "available" if available else "out_of_stock",
        "variant": _variant_brief(single) if single else None,
        "offered": offered,
        "available": available,
    }
    if showroom_id is not None:
        s = db.get(Showroom, showroom_id)
        out["showroom"] = _showroom_brief(s) if s else None
    return out
