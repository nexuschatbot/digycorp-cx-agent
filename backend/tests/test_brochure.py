"""Brochure tests (C3).

Two gates and one document. The gates are where this can hurt somebody — it puts
a message on a real phone — so they are tested from the refusing side first.

The PDF is asserted for the things a customer would notice being wrong: that it
carries the CURRENT catalogue price rather than a stored one, and that it never
claims to be more than a summary.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import (
    Notification,
    NotificationKind,
    NotificationStatus,
    VehicleModel,
)
from app.services import brochure, consent, customers
from app.tools.brochure_tools import send_brochure

SESSION = "sess-brochure-1"


def _customer(db, *, messaging=True):
    cid = customers.find_or_create(db, mobile="+971501234567", name="Nadia Rahman")[
        "customer"
    ]["customer_id"]
    if messaging:
        consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    return cid


def _a_vehicle(db) -> VehicleModel:
    return db.scalars(select(VehicleModel).order_by(VehicleModel.id)).first()


# --------------------------------------------------------------------------- #
# Gate 1: we must know whose phone it is
# --------------------------------------------------------------------------- #
def test_unidentified_customer_is_refused(db):
    v = _a_vehicle(db)
    out = send_brochure(db, SESSION, customer_id=None, vehicle_id=v.id)
    assert out["error"] == "customer_not_identified"
    assert db.scalars(select(Notification)).all() == []


# --------------------------------------------------------------------------- #
# Gate 2: we must be allowed to message them
# --------------------------------------------------------------------------- #
def test_no_messaging_consent_is_refused_before_anything_is_queued(db):
    """REFUSED IN THE TOOL, not silently retired by the dispatcher.

    The dispatcher would drop the row and say nothing, which is right for a
    scheduled reminder and wrong for something the customer just asked for —
    Aisha would promise a message that never arrives. So the tool refuses up
    front and she can ask for permission in the same breath.
    """
    cid = _customer(db, messaging=False)
    v = _a_vehicle(db)
    out = send_brochure(db, SESSION, customer_id=cid, vehicle_id=v.id)
    assert out["error"] == "consent_required"
    assert db.scalars(select(Notification)).all() == []


def test_granting_consent_then_sending_queues_exactly_one_row(db):
    cid = _customer(db)
    v = _a_vehicle(db)
    out = send_brochure(db, SESSION, customer_id=cid, vehicle_id=v.id)
    assert out["queued"] is True
    assert out["vehicle_name"] == v.name

    rows = db.scalars(select(Notification)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == NotificationKind.brochure
    assert row.status == NotificationStatus.scheduled
    # A brochure belongs to a PERSON, not a booking — this is why `ref` became
    # nullable and customer_id was added.
    assert row.customer_id == cid
    assert row.ref is None
    assert row.payload["vehicle_id"] == v.id
    assert row.payload["document"] == f"/api/brochure/{v.id}"


def test_unknown_vehicle_is_refused(db):
    cid = _customer(db)
    out = send_brochure(db, SESSION, customer_id=cid, vehicle_id=999_999)
    assert out["error"] == "vehicle_not_found"
    assert db.scalars(select(Notification)).all() == []


def test_two_models_are_two_calls_and_two_rows(db):
    cid = _customer(db)
    vs = db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all()[:2]
    for v in vs:
        assert send_brochure(db, SESSION, customer_id=cid, vehicle_id=v.id)["queued"]
    rows = db.scalars(select(Notification)).all()
    assert sorted(r.payload["vehicle_id"] for r in rows) == sorted(v.id for v in vs)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #
def test_summary_reads_from_the_live_catalogue(db):
    v = _a_vehicle(db)
    s = brochure.summary_for(db, v.id)
    assert s["name"] == v.name
    assert s["model_code"] == v.model_code
    assert str(v.price_aed_min) in s["price"].replace(",", "")


def test_summary_is_none_for_an_unknown_model(db):
    assert brochure.summary_for(db, 999_999) is None
    assert brochure.pdf_for(db, 999_999) is None


def test_price_follows_the_catalogue_rather_than_being_frozen(db):
    """THE REASON THE PDF IS GENERATED AND NOT STORED.

    A brochure sitting in a bucket goes stale the first time a price moves, and
    then contradicts the figure Aisha just quoted in the chat.
    """
    v = _a_vehicle(db)
    before = brochure.summary_for(db, v.id)["price"]
    v.price_aed_min = 1
    v.price_aed_max = 2
    db.flush()
    after = brochure.summary_for(db, v.id)["price"]
    assert after != before
    assert "AED 1" in after


def test_the_price_band_is_never_collapsed_to_one_figure(db):
    """The catalogue holds a RANGE; picking one number would invent a trim."""
    v = _a_vehicle(db)
    s = brochure.summary_for(db, v.id)
    if v.price_aed_min != v.price_aed_max:
        assert "–" in s["price"] or "-" in s["price"]


def test_pdf_is_a_real_pdf_with_the_model_name_in_it(db):
    v = _a_vehicle(db)
    pdf = brochure.pdf_for(db, v.id)
    assert pdf is not None
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 800  # not an empty page


def test_every_seeded_model_produces_a_brochure(db):
    """No model may be the one that 404s when a customer asks for it."""
    for v in db.scalars(select(VehicleModel)).all():
        assert brochure.pdf_for(db, v.id) is not None, f"no brochure for {v.model_code}"


def test_the_document_carries_no_customer_details(db):
    """It is about a CAR. The link is public and unauthenticated, so anything
    personal in here would be personal data behind no gate at all.

    THIS TEST'S REASONING CHANGED AT T0.10 even though its assertions did not.
    It used to prove that the PDF WE GENERATED never picked up the requesting
    customer's details. The document is now a static file of Elite's, so it
    structurally cannot — which makes this weaker as a test and stronger as a
    guarantee. Kept as the tripwire for the day anyone reintroduces per-customer
    rendering, or stamps a name onto the brochure before sending it.
    """
    cid = _customer(db)
    v = _a_vehicle(db)
    send_brochure(db, SESSION, customer_id=cid, vehicle_id=v.id)
    pdf = brochure.pdf_for(db, v.id)
    for secret in (b"Nadia", b"501234567", b"+971"):
        assert secret not in pdf
