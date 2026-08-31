"""GET /api/brochure/{vehicle_id} — Elite's own model brochure as a PDF (C3, T0.10).

PUBLIC AND UNAUTHENTICATED, deliberately. The URL is sent to a customer over
WhatsApp and has to open from a phone with no session, no cookie and no app. It
exposes nothing that is not already on the public catalogue pages — price band,
seats, powertrain, headline features — and carries no personal data at all: the
brochure is about a CAR, and the only thing tying it to a person is the
notification row, which is not reachable from here.

The id is the catalogue's own vehicle_id, so an unknown one is a plain 404
rather than an error envelope; this is a document endpoint, not a tool.

`variant_id` IS OPTIONAL AND EXISTS FOR THE DASHING. Elite ships one brochure per
trim for it — 1.5 Standard and 1.6 Luxury, different engines and gearboxes — and
one model row covers both. Everything else falls back to the model file, so the
parameter is safe to omit and safe to pass.

WHAT THESE FILES ARE NOW. Until T0.10 this endpoint served a ~5KB page this
system GENERATED from the catalogue row. It now serves Elite's artwork: 2-10MB
each. Same URL, same headers, three orders of magnitude more bytes — the 300s
cache header matters considerably more than it used to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import brochure as brochure_service

router = APIRouter()


@router.get("/api/brochure/{vehicle_id}")
def get_brochure(
    vehicle_id: int,
    variant_id: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    pdf = brochure_service.pdf_for(db, vehicle_id, variant_id)
    if pdf is None:
        raise HTTPException(status_code=404, detail="No brochure for that model.")
    summary = brochure_service.summary_for(db, vehicle_id) or {}
    # `inline` so a phone opens it in the browser's viewer rather than dropping a
    # file into Downloads — the customer asked to READ it, not to file it.
    # "-brochure", not "-summary": it is no longer a summary we wrote.
    name = (summary.get("model_code") or f"vehicle-{vehicle_id}").lower()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}-brochure.pdf"',
            "Cache-Control": "public, max-age=300",
        },
    )
