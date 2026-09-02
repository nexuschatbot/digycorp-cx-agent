"""Catalogue facts as retrievable passages (RETRIEVAL_FIRST, 2026-09-02).

WHY THIS EXISTS. The retrieval index used to hold documents only — brochures
and the four policy pages — so a colour, a price or a trim question could not
be answered from it and fell through to the specialist cascade: Aisha briefs,
a judge picks Sales, Haiku calls get_vehicle_details, Aisha composes. Measured
on the test VM that is four to five model calls in a row and 14-23s for "what
colours do you have for the Dashing", against ~4s when the passages already
carry the answer. The facts themselves are static per seed and live in
vehicle_models / vehicle_variants; this renders them as chunks so the same
~50ms local lookup that answers a warranty question answers a colour one.

WHAT IS AND IS NOT HERE. Per model: an overview (body type, seats, powertrain,
highlights, showrooms), the colour palette, and the trims with their list
prices. NOT here, on purpose: stock (live, per showroom — the anchor already
routes "stock checked" to the cascade), on-road totals and instalments (the
finance desk's numbers, kept out of every sales surface by design — see
catalogue._variant_brief), and anything computed. A passage states what the
seed states, worded the way the tools already word it.

CITATIONS are `<model_code>#<section>` so a reader can tell a catalogue fact
from a brochure paragraph, and `meta.vehicle_id` carries the id the specialists
would have produced.

RENDERING IS DETERMINISTIC (models by id, variants by price) so the digest is
stable and the cache in index.load does not thrash. Any failure — no database,
no tables, an empty catalogue — returns [] and logs: the index simply has no
catalogue collection that boot, and every question falls through exactly as
it did before this existed.
"""

from __future__ import annotations

import hashlib
import sys
import logging

from sqlalchemy import select

from app.retrieval.chunk import Chunk

log = logging.getLogger(__name__)

COLLECTION = "catalogue"

# Bumped whenever the wording below changes, so a redeploy re-embeds rather
# than serving vectors of the old wording from the cache.
_VERSION = "v1"


def _aed(n: int | None) -> str:
    return "not yet published" if n is None else f"AED {n:,}"


def _seats(lo: int | None, hi: int | None) -> str:
    if lo is None and hi is None:
        return ""
    if lo == hi or hi is None:
        return f"{lo} seats"
    if lo is None:
        return f"{hi} seats"
    return f"{lo} to {hi} seats"


def _economy(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return ""
    if lo == hi:
        return f"about {lo:g} km per litre"
    return f"about {lo:g} to {hi:g} km per litre"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _sentence(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _chunk(model, heading: str, body: str) -> Chunk:
    source = (model.model_code or model.name or "").lower()
    return Chunk(
        # Heading-prefixed like chunk_file does, and the model NAME rather than
        # the code: the customer writes "Dashing", never "JET-DASHING".
        text=f"{model.name} — {heading}\n{body}",
        body=body,
        source=source,
        collection=COLLECTION,
        heading=heading,
        kind="fact",
        review_status="complete",
        meta={"vehicle_id": model.id},
    )


def render(db) -> list[Chunk]:
    """Every catalogue passage, in a stable order. Raises on a broken DB — the
    public collect() is the one that degrades."""
    from app.models import Showroom, VehicleModel, VehicleVariant

    models = list(db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all())
    if not models:
        return []
    variants: dict[int, list] = {}
    for v in db.scalars(select(VehicleVariant).order_by(VehicleVariant.id)).all():
        variants.setdefault(v.model_id, []).append(v)
    showrooms = {s.id: s for s in db.scalars(select(Showroom)).all()}

    out: list[Chunk] = []
    for m in models:
        name = m.name
        rows = sorted(
            variants.get(m.id, []),
            key=lambda x: (x.price_aed is None, x.price_aed or 0, x.id),
        )

        # --- Overview ------------------------------------------------------
        fuel = m.fuel_type.value if m.fuel_type else None
        fuel_words = {"petrol": "petrol", "phev": "plug-in hybrid (PHEV)"}.get(fuel, fuel)
        where = _join([showrooms[i].name for i in (m.showroom_ids or []) if i in showrooms])
        overview = _sentence(
            f"{name}: {m.body_type}." if m.body_type else f"{name}.",
            f"{_seats(m.seats_min, m.seats_max).capitalize()}." if _seats(m.seats_min, m.seats_max) else "",
            f"Powertrain: {fuel_words}." if fuel_words else "",
            f"Highlights: {'; '.join(m.headline_features or [])}." if m.headline_features else "",
            f"Available at {where}." if where else "",
        )
        out.append(_chunk(m, "Overview", overview))

        # --- Colours -------------------------------------------------------
        ext = list(m.exterior_colours or [])
        inte = list(m.interior_colours or [])
        colours = _sentence(
            f"{name} exterior colours: {_join(ext)}." if ext else f"{name}: exterior colours not yet published.",
            f"Interior colours: {_join(inte)}." if inte else "",
            "These are the colours the car is offered in; which of them is in "
            "stock at a particular showroom is a separate stock check.",
        )
        out.append(_chunk(m, "Colours", colours))

        # --- Prices and variants ------------------------------------------
        if m.price_aed_min is not None and m.price_aed_max is not None:
            if m.price_aed_min == m.price_aed_max:
                headline = f"{name} list price: {_aed(m.price_aed_min)}."
            else:
                headline = f"{name} list prices from {_aed(m.price_aed_min)} to {_aed(m.price_aed_max)}."
        else:
            headline = f"{name}: list price not yet published."
        trims: list[str] = []
        for v in rows:
            label = v.name or "Standard"
            bits = [f"{label} {_aed(v.price_aed)}"]
            s = _seats(v.seats_min, v.seats_max)
            e = _economy(v.km_per_l_min, v.km_per_l_max)
            detail = _join([s, e])
            if detail:
                bits.append(f"({detail})")
            if v.coming_soon:
                bits.append("coming soon")
            trims.append(" ".join(bits))
        prices = _sentence(
            headline,
            f"Variants: {'; '.join(trims)}." if trims else "",
            "List prices exclude insurance and registration; on-road totals and "
            "instalment plans are quoted separately.",
        )
        out.append(_chunk(m, "Prices and variants", prices))
    return out


def collect(db=None) -> list[Chunk]:
    """The catalogue passages, or [] when they cannot be rendered. Never raises."""
    try:
        if db is not None:
            return render(db)
        from app.db import SessionLocal

        with SessionLocal() as session:
            return render(session)
    except Exception:  # noqa: BLE001 — a missing catalogue must not break boot
        log.warning("catalogue passages unavailable — indexing documents only (%s)", repr(sys.exc_info()[1])[:200])
        return []


def content_hash(db=None) -> str:
    """A digest of the RENDERED passages — the cache gate, like chunk.content_hash.
    Re-seeding, editing a colour or bumping _VERSION all change it."""
    h = hashlib.sha256()
    h.update(_VERSION.encode())
    for c in collect(db):
        h.update(c.citation.encode())
        h.update(c.text.encode())
    return h.hexdigest()[:16]
