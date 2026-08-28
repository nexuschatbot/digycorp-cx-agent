"""The admin Vehicles tab, now variant-level (T0.11).

The console row was model-shaped: a card reading "AED 169,000 – 240,450" tells
an operator nothing about which cars sit at either end. The catalogue is
variant-level now, so the row carries its trims — and the one an operator most
needs to see is the one that cannot be sold.

Offline: the seeded `db`, no HTTP, no vendor.
"""

from __future__ import annotations

from sqlalchemy import event, select

from app.models import VehicleModel
from app.services import console


def _row(db, code: str) -> dict:
    out = console.list_vehicles(db, limit=50)
    return next(r for r in out["rows"] if r["model_code"] == code)


def test_every_vehicle_row_carries_its_trims(db):
    out = console.list_vehicles(db, limit=50)
    assert out["rows"]
    for r in out["rows"]:
        assert r["variants"], r["model_code"]


def test_trims_are_ordered_by_price_never_by_name(db):
    """THE T1 IS WHY. Its Luxury is the four-wheel-drive range-topper and costs
    MORE than its Flagship, so a name sort would draw a ladder that does not
    exist — and an operator checking a price would read the wrong one."""
    names = [t["name"] for t in _row(db, "JET-T1")["variants"]]
    assert names == ["Comfort", "Flagship", "Luxury"]


def test_the_coming_soon_trim_is_flagged_and_sorts_last(db):
    """"Why won't Aisha quote this car?" is answered here. Coming soon is NOT
    sold out and NOT a data-entry mistake, and an operator must tell the three
    apart at a glance."""
    variants = _row(db, "JET-G700")["variants"]
    assert [t["name"] for t in variants] == ["Comfort", "Luxury", "Flagship", "Top Fire"]
    top_fire = variants[-1]
    assert top_fire["coming_soon"] is True
    assert top_fire["price_aed"] is None
    assert top_fire["on_road_aed"] is None
    # And nothing else is flagged, so the badge means something.
    assert sum(t["coming_soon"] for t in variants) == 1


def test_the_on_road_figure_is_all_three_parts_or_nothing(db):
    """A partial sum lands BELOW the list price and reads as a discount on an
    internal screen the operator trusts."""
    for t in _row(db, "JET-G700")["variants"]:
        if t["on_road_aed"] is None:
            continue
        assert t["on_road_aed"] == (
            t["price_aed"] + t["insurance_aed"] + t["registration_aed"]
        )
        assert t["on_road_aed"] > t["price_aed"]


def test_a_single_unnamed_trim_keeps_a_null_name_for_the_ui_to_resolve(db):
    """NULL means "the only version", not "unknown". The row does not invent a
    name; the card renders the model name in its place."""
    variants = _row(db, "JET-T2-IDM")["variants"]
    assert len(variants) == 1
    assert variants[0]["name"] is None


def test_the_row_carries_the_interior_palette(db):
    assert _row(db, "JET-T1")["interior_colours"] == [
        "Black & Orange", "Black & Green", "Black & Light Grey"
    ]


def test_the_model_price_range_still_spans_its_trims(db):
    """The card shows both, so they must agree — a range that disagreed with the
    trims beneath it is worse than either alone."""
    for r in console.list_vehicles(db, limit=50)["rows"]:
        priced = [t["price_aed"] for t in r["variants"] if t["price_aed"] is not None]
        assert r["price_aed_min"] == min(priced), r["model_code"]
        assert r["price_aed_max"] == max(priced), r["model_code"]


def test_listing_a_page_does_not_query_variants_once_per_row(db):
    """A page is twelve vehicles; a per-row lookup would be twelve round trips
    for a fifteen-row table."""
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "vehicle_variants" in statement:
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        console.list_vehicles(db, limit=50)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(seen) == 1, f"{len(seen)} variant queries for one page"


def test_an_empty_page_does_not_query_variants_at_all(db):
    """No rows, no ids, no `IN ()` — a guard worth having because an empty IN
    clause is a silent full-table scan on some backends."""
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "vehicle_variants" in statement:
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        console.list_vehicles(db, brand="Nonexistent")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert seen == []
