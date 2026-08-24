"""GET /api/vehicles/{id} — the dialog's data source (the DIRECT path, AD-I).

No LLM anywhere in these tests: the whole point of the endpoint is that browsing
a car is fully determined by its id, so it must never reach a model.

The load-bearing assertion is the LAST one — that the agent's tool payload did
NOT grow. Arabic bullets exist so the dialog can render them; carrying them in
_vehicle_dict as well would add ~29% to every search_vehicles result in tokens
the model never speaks. That is easy to undo by accident, silent when it
happens, and only shows up as a bigger bill.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.services import catalogue


@pytest.fixture()
def client(db) -> TestClient:
    """A TestClient whose routes use the test's in-memory Session.

    Clears the override afterwards — a leaked dependency_overrides entry points
    later tests at a closed Session and fails them somewhere unrelated.
    """
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_returns_full_detail_in_both_languages(db, client):
    r = client.get("/api/vehicles/2")
    assert r.status_code == 200
    body = r.json()

    assert body["vehicle_id"] == 2
    assert body["name"]
    # Both languages, same length — the dialog pairs them by index.
    assert len(body["headline_features"]) == len(body["headline_features_ar"]) > 0
    assert any("؀" <= ch <= "ۿ" for ch in " ".join(body["headline_features_ar"])), (
        "the _ar bullets must actually be Arabic"
    )
    # Enums and numbers, not display text — the UI labels them per language.
    assert body["fuel"] in {"petrol", "phev"}
    assert isinstance(body["seats_min"], int)
    # Showrooms carry city for the dialog's "available at" line.
    assert body["showrooms"] and {"showroom_id", "name", "emirate", "city"} <= set(
        body["showrooms"][0]
    )


def test_unknown_id_is_404(db, client):
    # Contrast GET /api/conversation/{id}, where an unknown id returns an empty
    # transcript rather than an error (it means "rotate to a fresh session").
    # An unknown VEHICLE id is a real client bug and should say so.
    assert client.get("/api/vehicles/999999").status_code == 404


def test_every_vehicle_ships_a_price_over_the_wire(db, client):
    """Replaces test_unpublished_price_stays_null (2026-07-27).

    That test asserted a NULL price survived the wire as null so the dialog could
    render no price line — correct while `null` meant "not published". Every model
    now carries a published range and the nullable path was removed, so the thing
    worth guarding inverted: no vehicle may reach the UI without a price, and none
    may arrive as a fabricated 0 or "" either.
    """
    rows = [client.get(f"/api/vehicles/{i}").json() for i in range(1, 10)]
    for r in rows:
        assert isinstance(r["price_aed_min"], int) and r["price_aed_min"] > 0, r
        assert isinstance(r["price_aed_max"], int) and r["price_aed_max"] > 0, r
        assert r["price_aed_max"] >= r["price_aed_min"], r


def test_agent_tool_payload_did_not_grow(db):
    """The Arabic bullets must NOT leak into the agent's context."""
    vehicle = catalogue.search(db)["matches"][0]
    assert "headline_features" in vehicle
    assert "headline_features_ar" not in vehicle, (
        "Arabic bullets in the tool result would grow every search_vehicles "
        "payload ~29% for text the model never speaks — keep them on the REST path"
    )
