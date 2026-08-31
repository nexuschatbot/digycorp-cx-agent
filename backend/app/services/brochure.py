"""Brochure service (C3, T0.10) — Elite's own Jetour PDFs.

WHAT CHANGED AT T0.10, AND WHY THE OLD FUNCTION IS GONE RATHER THAN KEPT AS A
FALLBACK. Until now this module BUILT a one-page PDF with reportlab from the
catalogue row, because Elite had supplied no artwork. Its own docstring named
`pdf_for` as the single function to replace when real brochures arrived. They
have arrived — ten of them, 110 pages — so it is replaced.

NO FALLBACK TO THE GENERATED PAGE. It would mean a customer sometimes receives
Elite's photography and finance offers and sometimes a plain grey summary, with
nothing in the conversation to say which — and the summary is the one that looks
like a mistake. A missing brochure returns None and the endpoint 404s, which the
agent already handles by saying so.

`summary_for` STAYS, and is still read from the catalogue at request time. It is
what the chat message says about the car ("the X70 FL, from AED 79,000"), and it
must keep coming from the live row: the PDF is Elite's artwork and cannot be
re-rendered when a price moves, so the two now have DIFFERENT lifetimes. That is
the one real cost of shipping real brochures — see the price note on
`summary_for`.

FILES ARE KEYED ON model_code, NOT ON THEIR ORIGINAL NAMES. `jet-g700.pdf`, not
`g700-compressed.pdf`: the code is the catalogue's natural key, so the lookup is
exact rather than a filename guess. Provenance is not lost — each brochure MD in
content/brochures/ records its `source_pdf` in frontmatter.

THE DASHING NEEDS TWO, and it is why this takes an optional variant. Elite ships
separate brochures for the 1.5 Standard and the 1.6 Luxury — different engines
(1.5TCI vs 1.6TGDI), different gearboxes (6DCT vs 7DCT) — while the catalogue
holds one Dashing model. Sending a 1.5 customer the 1.6's brochure would be
wrong, not merely approximate, so a variant-specific file wins when one exists
and the model-level file is the fallback.

ARABIC. The generated summary was English-only by deliberate decision — correct
Arabic PDF output needs a shaped RTL font pipeline and a half-working one looks
careless. That constraint is gone from OUR side, but it is now Elite's brochures
that decide: whatever language they are in is what the customer receives. Every
one of the ten is English.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import VehicleModel, VehicleVariant

log = logging.getLogger("app.services.brochure")

# backend/app/services/brochure.py -> backend/content/brochures/pdf
#
# Overridable so the 54MB of vendor artwork does not have to live in the image if
# an operator would rather mount it — the default keeps the repo self-contained
# and the test suite offline, which is how everything else here works.
PDF_DIR = Path(
    os.getenv("BROCHURE_PDF_DIR")
    or (Path(__file__).resolve().parent.parent.parent / "content" / "brochures" / "pdf")
)

# WhatsApp accepts documents up to 100MB. The largest brochure is ~10MB, so
# nothing is near it — but the generated page was ~5KB, so this is a THOUSANDFOLD
# increase on a public unauthenticated endpoint. Checked by a test rather than
# assumed, because the next brochure Elite sends is the one that breaks it.
WHATSAPP_DOCUMENT_LIMIT_BYTES = 100 * 1024 * 1024


def _fmt_price(lo: int | None, hi: int | None) -> str:
    """The price BAND, never a single figure — the catalogue holds a range and
    collapsing it to one number would be inventing a trim we do not know."""
    if lo and hi and lo != hi:
        return f"AED {lo:,} – {hi:,}"
    if lo:
        return f"From AED {lo:,}"
    return "Price on request"


def _fmt_seats(lo: int | None, hi: int | None) -> str:
    if lo and hi and lo != hi:
        return f"{lo}–{hi} seats"
    if lo:
        return f"{lo} seats"
    return "—"


_FUEL = {"petrol": "Petrol", "phev": "Plug-in hybrid (PHEV)"}


def summary_for(db: Session, vehicle_id: int) -> dict | None:
    """The facts a brochure states, or None if we hold no such vehicle.

    Split out from the rendering so the tool can name the car in the chat from
    exactly what the document will say — one read, one truth, no chance of the
    message and the attachment disagreeing.
    """
    v = db.get(VehicleModel, vehicle_id)
    if v is None:
        return None
    features = v.headline_features or []
    return {
        "vehicle_id": v.id,
        "model_code": v.model_code,
        "name": v.name,
        "brand": v.brand,
        "body_type": v.body_type,
        "seats": _fmt_seats(v.seats_min, v.seats_max),
        "fuel": _FUEL.get(getattr(v.fuel_type, "value", v.fuel_type), "—"),
        "price": _fmt_price(v.price_aed_min, v.price_aed_max),
        "features": [str(f) for f in features][:6],
    }


def _slug(text: str) -> str:
    """"1.5 Standard" -> "1-5-standard". Filenames, not display."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def path_for(db: Session, vehicle_id: int, variant_id: int | None = None) -> Path | None:
    """Which file this customer should receive, or None if we hold none.

    RESOLUTION IS VARIANT-FIRST, MODEL-SECOND. The Dashing is the case that
    forces it: Elite ships a separate brochure for the 1.5 Standard and the 1.6
    Luxury — different engines and different gearboxes — and the catalogue holds
    one Dashing. `jet-dash--1-5-standard.pdf` wins for that trim; every other
    trim falls back to `jet-dash.pdf`, which is the 1.6.

    THE FILENAME IS BUILT FROM THE DATABASE, NOT FROM ANYTHING A CUSTOMER TYPED,
    and is slugged to [a-z0-9-] regardless. The endpoint is public and
    unauthenticated, so the path is constructed defensively even though its
    inputs are ours: the guard costs one regex and removes the entire class.
    """
    v = db.get(VehicleModel, vehicle_id)
    if v is None or not v.model_code:
        return None
    code = _slug(v.model_code)
    if not code:
        return None

    if variant_id is not None:
        var = db.get(VehicleVariant, variant_id)
        # Ignore a variant belonging to a DIFFERENT model rather than serving
        # its brochure — a mismatched pair is worse than the model default.
        if var is not None and var.model_id == v.id and var.name:
            candidate = PDF_DIR / f"{code}--{_slug(var.name)}.pdf"
            if candidate.is_file():
                return candidate

    model_file = PDF_DIR / f"{code}.pdf"
    return model_file if model_file.is_file() else None


def pdf_for(
    db: Session, vehicle_id: int, variant_id: int | None = None
) -> bytes | None:
    """Elite's own brochure for this car, as bytes, or None if we hold none.

    NO LONGER GENERATED. This used to render a one-page summary with reportlab
    because Elite had supplied no artwork; it now returns their file. There is
    deliberately no fallback to the old page — see the module docstring.

    Read per request rather than cached in memory: nine files totalling ~54MB
    would be 54MB of resident process for documents that are asked for rarely,
    and the OS page cache already does this job better than we would.
    """
    path = path_for(db, vehicle_id, variant_id)
    if path is None:
        log.info("no brochure file for vehicle_id=%s variant_id=%s", vehicle_id, variant_id)
        return None
    try:
        return path.read_bytes()
    except OSError:
        # A brochure that cannot be read is a missing brochure, not a 500. The
        # agent already has wording for "I don't have one for that".
        log.exception("could not read brochure %s", path)
        return None
