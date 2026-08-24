"""GET /api/brochure/{vehicle_id} — the one-page model summary as a PDF (C3).

PUBLIC AND UNAUTHENTICATED, deliberately. The URL is sent to a customer over
WhatsApp and has to open from a phone with no session, no cookie and no app. It
exposes nothing that is not already on the public catalogue pages — price band,
seats, powertrain, headline features — and carries no personal data at all: the
brochure is about a CAR, and the only thing tying it to a person is the
notification row, which is not reachable from here.

The id is the catalogue's own vehicle_id, so an unknown one is a plain 404
rather than an error envelope; this is a document endpoint, not a tool.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import brochure as brochure_service

router = APIRouter()


@router.get("/api/brochure/{vehicle_id}")
def get_brochure(vehicle_id: int, db: Session = Depends(get_db)) -> Response:
    pdf = brochure_service.pdf_for(db, vehicle_id)
    if pdf is None:
        raise HTTPException(status_code=404, detail="No brochure for that model.")
    summary = brochure_service.summary_for(db, vehicle_id) or {}
    # `inline` so a phone opens it in the browser's viewer rather than dropping a
    # file into Downloads — the customer asked to READ it, not to file it.
    name = (summary.get("model_code") or f"vehicle-{vehicle_id}").lower()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}-summary.pdf"',
            "Cache-Control": "public, max-age=300",
        },
    )
