"""Brochure service (C3) — a one-page PDF per model, built from the catalogue.

WHERE THE CONTENT COMES FROM, AND WHY THAT MATTERS. Every line is read from
VehicleModel at request time: the name, body type, seat range, powertrain, price
band and headline features. Nothing is written here that we do not already tell
customers in the chat, so a brochure can never contradict what Aisha just said —
which a hand-made PDF sitting in a bucket absolutely would, the first time a
price moved.

NOT A MARKETING BROCHURE, AND IT SHOULD NOT PRETEND TO BE. Elite Group's real
brochures have photography, finance offers and trim tables we do not hold. This
is an honest one-page summary of what our catalogue knows, so it is titled as a
summary. When real PDFs arrive, `pdf_for` is the one function to replace and
everything downstream — the tool, the notification, the console — is unchanged.

GENERATED ON DEMAND, NOT STORED. A brochure is a pure function of the vehicle
row, so caching it would only create a second copy to keep in step with the
first. They are small and rare; the cost of rebuilding one is a few milliseconds
against the cost of serving a stale price, which is a customer arriving expecting
the wrong number.

ARABIC IS DELIBERATELY NOT ATTEMPTED HERE. Correct Arabic PDF output needs a
shaped, RTL-aware font pipeline, and a half-working one produces reversed,
disconnected letterforms — visibly broken in a way that reads as carelessness
about the language rather than a missing feature. The English summary is offered
in both conversations until that is done properly.
"""

from __future__ import annotations

import io
import logging

from sqlalchemy.orm import Session

from app.models import VehicleModel

log = logging.getLogger("app.services.brochure")

# Elite's navy, matching the console and the documents.
_NAVY = (0x1F / 255, 0x38 / 255, 0x64 / 255)
_INK = (0.13, 0.13, 0.13)
_SOFT = (0.42, 0.42, 0.42)


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


def pdf_for(db: Session, vehicle_id: int) -> bytes | None:
    """The one-page PDF as bytes, or None for a vehicle we do not hold.

    REPLACE THIS FUNCTION when Elite Group supply real brochures — return the
    stored file instead of building one. Nothing else in C3 needs to change.
    """
    s = summary_for(db, vehicle_id)
    if s is None:
        return None

    # Imported here, not at module scope: a missing reportlab must degrade to
    # "no brochure" on one request, never take the whole app down at import.
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except Exception:  # noqa: BLE001
        log.exception("reportlab unavailable — cannot build a brochure")
        return None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setTitle(f"{s['name']} — summary")
    c.setAuthor("Elite Group")

    # Header band
    c.setFillColorRGB(*_NAVY)
    c.rect(0, H - 42 * mm, W, 42 * mm, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(20 * mm, H - 24 * mm, s["name"])
    c.setFont("Helvetica", 11)
    c.drawString(20 * mm, H - 32 * mm, s["body_type"] or s["brand"])
    c.setFont("Helvetica", 9)
    c.drawRightString(W - 20 * mm, H - 24 * mm, "ELITE GROUP")

    y = H - 58 * mm

    # The three facts a customer actually asks for, side by side.
    for label, value in (("Price", s["price"]), ("Seats", s["seats"]), ("Powertrain", s["fuel"])):
        c.setFillColorRGB(*_SOFT)
        c.setFont("Helvetica", 8.5)
        c.drawString(20 * mm, y, label.upper())
        c.setFillColorRGB(*_INK)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(20 * mm, y - 7 * mm, value)
        y -= 18 * mm

    if s["features"]:
        c.setFillColorRGB(*_SOFT)
        c.setFont("Helvetica", 8.5)
        c.drawString(20 * mm, y, "HIGHLIGHTS")
        y -= 8 * mm
        c.setFillColorRGB(*_INK)
        c.setFont("Helvetica", 11)
        for f in s["features"]:
            # Wrap by hand rather than pulling in Platypus for six bullets.
            line = f if len(f) <= 78 else f[:75] + "…"
            c.drawString(22 * mm, y, "•")
            c.drawString(28 * mm, y, line)
            y -= 7 * mm

    # The honest footer. It says what this is, so nobody mistakes a generated
    # summary for Elite's own marketing material.
    c.setFillColorRGB(*_SOFT)
    c.setFont("Helvetica", 8)
    c.drawString(
        20 * mm, 18 * mm,
        "Summary generated from the Elite Group catalogue. Specification and pricing "
        "are indicative and may vary by trim.",
    )
    c.drawString(20 * mm, 13 * mm, "Book a test drive: elite-group.digycorp.com")

    c.showPage()
    c.save()
    return buf.getvalue()
