"""The real Jetour brochures, and the ways serving them can go wrong (T0.10).

Until T0.10 this system GENERATED a one-page PDF from the catalogue row. It now
serves Elite's own artwork — nine model files plus one variant file, ~54MB.
Three things changed shape with it, and each is asserted here:

  1. THE FILES MUST SHIP. A container without content/ boots fine and 404s every
     brochure request while local dev works perfectly. That exact bug shipped
     once already with the alembic directory.
  2. THE DASHING NEEDS TWO. Elite ships one brochure per trim for it, and the
     two are different engines and different gearboxes. Sending a 1.5 customer
     the 1.6's brochure is wrong, not approximate.
  3. THE PRICE IN THE PDF CANNOT BE UPDATED. It is Elite's artwork. The chat
     message still reads the live catalogue row, so the two now have different
     lifetimes — a real cost, recorded rather than discovered later.

Offline: reads files and the seeded db, no vendor.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.models import VehicleModel, VehicleVariant
from app.services import brochure

BACKEND = Path(__file__).resolve().parent.parent
PDF_DIR = BACKEND / "content" / "brochures" / "pdf"


# --------------------------------------------------------------------------- #
# The files exist and are real
# --------------------------------------------------------------------------- #
def test_every_model_has_a_brochure_file_named_by_its_code(db):
    """Keyed on model_code, so the lookup is exact rather than a filename guess.
    No model may be the one that 404s when a customer asks for it."""
    missing = []
    for v in db.scalars(select(VehicleModel)).all():
        if not (PDF_DIR / f"{v.model_code.lower()}.pdf").is_file():
            missing.append(v.model_code)
    assert missing == [], missing


def test_every_brochure_is_a_real_pdf(db):
    for path in sorted(PDF_DIR.glob("*.pdf")):
        head = path.read_bytes()[:5]
        assert head == b"%PDF-", f"{path.name} is not a PDF"


def test_no_brochure_is_near_the_whatsapp_document_limit():
    """WhatsApp accepts documents to 100MB. Nothing is close — but the generated
    page was ~5KB, so this is a thousandfold increase and the next brochure
    Elite sends is the one that breaks it. Checked, not assumed."""
    oversized = [
        (p.name, p.stat().st_size)
        for p in PDF_DIR.glob("*.pdf")
        if p.stat().st_size > brochure.WHATSAPP_DOCUMENT_LIMIT_BYTES
    ]
    assert oversized == [], oversized
    # And a much tighter tripwire than the hard limit, so growth is noticed long
    # before it becomes a failure.
    biggest = max(p.stat().st_size for p in PDF_DIR.glob("*.pdf"))
    assert biggest < 25 * 1024 * 1024, f"largest brochure is {biggest/1e6:.1f}MB"


# --------------------------------------------------------------------------- #
# The Dashing
# --------------------------------------------------------------------------- #
def test_the_dashing_serves_a_different_brochure_for_its_two_trims(db):
    """THE CASE THAT FORCED variant_id. Elite ships separate brochures for the
    1.5 Standard (1.5TCI, 6DCT) and the 1.6 Luxury (1.6TGDI, 7DCT). One model
    row covers both, so without this a 1.5 customer receives the 1.6's document.
    """
    dash = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-DASH"))
    trims = {
        v.name: v
        for v in db.scalars(
            select(VehicleVariant).where(VehicleVariant.model_id == dash.id)
        ).all()
    }
    standard = brochure.pdf_for(db, dash.id, trims["1.5 Standard"].id)
    luxury = brochure.pdf_for(db, dash.id, trims["1.6 Luxury"].id)
    model_default = brochure.pdf_for(db, dash.id)

    assert standard is not None and luxury is not None
    assert standard != luxury, "both trims received the same file"
    # The 1.6 has no file of its own; it falls back to the model default, which
    # IS the 1.6 brochure. Recorded so the fallback is understood as deliberate.
    assert luxury == model_default


def test_a_variant_with_no_file_of_its_own_falls_back_to_the_model(db):
    """Only the Dashing needs per-trim files. Every other trim must still get
    its model's brochure rather than nothing."""
    g700 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-G700"))
    for v in db.scalars(
        select(VehicleVariant).where(VehicleVariant.model_id == g700.id)
    ).all():
        assert brochure.pdf_for(db, g700.id, v.id) == brochure.pdf_for(db, g700.id)


def test_a_variant_from_another_model_is_ignored_not_served(db):
    """A mismatched pair is worse than the model default — it would send the
    Dashing's brochure to someone asking about a G700."""
    g700 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-G700"))
    dash = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-DASH"))
    foreign = db.scalar(
        select(VehicleVariant).where(
            VehicleVariant.model_id == dash.id, VehicleVariant.name == "1.5 Standard"
        )
    )
    assert brochure.pdf_for(db, g700.id, foreign.id) == brochure.pdf_for(db, g700.id)


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_an_unknown_vehicle_yields_no_brochure_rather_than_an_error(db):
    assert brochure.pdf_for(db, 999_999) is None
    assert brochure.path_for(db, 999_999) is None


def test_a_missing_file_is_a_missing_brochure_not_a_fallback_page(db, monkeypatch):
    """NO FALLBACK TO THE GENERATED SUMMARY, deliberately. It would mean a
    customer sometimes gets Elite's photography and sometimes a plain grey page,
    with nothing in the conversation to say which — and the grey page is the one
    that looks like a mistake."""
    monkeypatch.setattr(brochure, "PDF_DIR", BACKEND / "content" / "brochures" / "nope")
    v = db.scalar(select(VehicleModel))
    assert brochure.pdf_for(db, v.id) is None


def test_the_filename_cannot_escape_the_brochure_directory(db):
    """The endpoint is public and unauthenticated. The inputs are ours — a
    model_code from our own database — but the path is built defensively anyway:
    the guard costs one regex and removes the whole class."""
    v = db.scalar(select(VehicleModel))
    v.model_code = "../../etc/passwd"
    db.flush()
    path = brochure.path_for(db, v.id)
    assert path is None or PDF_DIR in path.parents


# --------------------------------------------------------------------------- #
# What shipping real artwork costs
# --------------------------------------------------------------------------- #
def test_the_chat_summary_still_comes_from_the_live_catalogue_row(db):
    """THE ONE REAL COST OF REAL BROCHURES, recorded rather than discovered.

    The PDF is Elite's artwork and cannot be re-rendered when a price moves. The
    message that accompanies it still reads the live row, so the two now have
    DIFFERENT lifetimes: change a price and the chat updates, the document does
    not. That is the correct trade — a stale sentence is worse than a stale
    brochure — but it must stay true that the sentence is the fresh half.
    """
    v = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X50"))
    assert "69,000" in brochure.summary_for(db, v.id)["price"]
    v.price_aed_min = v.price_aed_max = 71_000
    db.flush()
    assert "71,000" in brochure.summary_for(db, v.id)["price"]


def test_the_docker_image_ships_the_content_directory():
    """A container without content/ boots fine and then 404s every brochure
    request, while local dev works perfectly. That exact failure shipped once
    already with the alembic directory — same test, same reason."""
    dockerfile = (BACKEND / "Dockerfile").read_text()
    assert "COPY content ./content" in dockerfile


def test_the_generated_one_pager_is_gone_not_merely_unused():
    """Retired, per the plan. A dormant generator invites a fallback later, and
    a fallback is the failure mode this task exists to remove."""
    import ast

    tree = ast.parse((BACKEND / "app" / "services" / "brochure.py").read_text())
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    # Checked against the IMPORTS, not the text: the module docstring still says
    # the word, explaining what was removed and why, and that sentence should
    # outlive the code it describes.
    assert "reportlab" not in imported, "the generator is still wired in"
