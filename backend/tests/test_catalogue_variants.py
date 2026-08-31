"""Variant-aware discovery (T0.9).

THE DEFECT THIS CLOSES. A model card matches on a RANGE. The G700 spans AED
169,000-240,450 and seats 5-6, so it matches a 180,000 budget and it matches "I
need six seats" — but only its Comfort fits the first, only its Luxury and
Flagship fit the second, and NEITHER FITS BOTH. The agent was told the model
matched and had no way to learn which trim did; the customer found out at the
showroom.

WHAT DELIBERATELY DID NOT CHANGE. search() still returns MODEL cards with a
price range, `_pick_spread` is untouched and `basis` means what it always meant.
An unfiltered browse returns byte-for-byte the shape it did before — asserted
below, because the temptation on this task was to turn discovery into a trim
list, and nine models is what a customer asked for.

Offline: the seeded `db`, no vendor, no model.
"""

from __future__ import annotations

from sqlalchemy import event, select

from app.models import VehicleModel
from app.services import catalogue

G700, T1, X50 = 1, 4, 9


def _match(out: dict, name: str) -> dict:
    return next(m for m in out["matches"] if m["name"] == name)


# --------------------------------------------------------------------------- #
# The motivating case
# --------------------------------------------------------------------------- #
def test_a_budget_that_the_model_meets_but_most_trims_do_not(db):
    """THE WHOLE POINT OF T0.9.

    The G700's range starts at 169,000, so a 180,000 budget matches the model.
    Exactly one of its four trims is actually purchasable at that money, and the
    card now says so instead of leaving "the G700 fits" to stand.
    """
    out = catalogue.search(db, max_price_aed=180_000, basis="flagship")
    g700 = _match(out, "Jetour G700")

    # The card keeps its honest model-level range.
    assert (g700["price_aed_min"], g700["price_aed_max"]) == (169_000, 240_450)
    assert g700["variant_count"] == 4

    fits = g700["matching_variants"]
    assert [v["name"] for v in fits] == ["Comfort"]
    assert fits[0]["price_aed"] == 169_000
    # Fewer matching than total IS the signal that some trims do not fit.
    assert len(fits) < g700["variant_count"]


def test_a_seat_constraint_names_the_trims_that_actually_seat_that_many(db):
    """The G700 Comfort is a 5-seater (2+3); its Luxury and Flagship are 6
    (2+2+2). "The G700 seats six" is true of two trims out of four."""
    out = catalogue.search(db, min_seats=6)
    g700 = _match(out, "Jetour G700")
    assert (g700["seats_min"], g700["seats_max"]) == (5, 6)
    assert [v["name"] for v in g700["matching_variants"]] == ["Luxury", "Flagship"]


def test_the_two_constraints_together_can_exclude_every_trim(db):
    """Six seats AND under 180,000: the G700's six-seaters start at 210,000, so
    NO trim satisfies both even though the model satisfies each separately.

    An empty list is the honest answer and is not the same as the key being
    absent — absent means "nothing to discriminate", empty means "we checked".
    """
    rows = db.scalars(select(VehicleModel)).all()
    g700 = next(v for v in rows if v.id == G700)
    variants = catalogue._variants_by_model(db, [G700])[G700]

    assert g700.seats_max >= 6 and g700.price_aed_min <= 180_000
    fits = catalogue._variants_satisfying(
        variants, min_seats=6, max_price_aed=180_000
    )
    assert fits == []


def test_a_coming_soon_trim_never_satisfies_a_budget(db):
    """Same rule as the model-level guard: we cannot promise a car fits a budget
    when we do not know what it costs. The G700 Top Fire has no price."""
    variants = catalogue._variants_by_model(db, [G700])[G700]
    for budget in (100_000, 300_000, 10_000_000):
        fits = catalogue._variants_satisfying(
            variants, min_seats=None, max_price_aed=budget
        )
        assert "Top Fire" not in [v.name for v in fits], budget


# --------------------------------------------------------------------------- #
# What did NOT change
# --------------------------------------------------------------------------- #
def test_an_unfiltered_browse_carries_no_matching_variants_at_all(db):
    """Absent, not empty. Nothing discriminates within a model when the customer
    named no budget and no seat count, so the key would be noise — and the
    ordinary browse pays not one token for T0.9.
    """
    for out in (
        catalogue.search(db),
        catalogue.search(db, fuel="petrol"),
        catalogue.search(db, body_type="suv"),
    ):
        for m in out["matches"]:
            assert "matching_variants" not in m, m["name"]


def test_search_still_returns_model_cards_not_trim_lists(db):
    """The standing decision: "what SUVs do you have" wants nine cars, not
    fifteen trims. No card carries a variant list; it carries a COUNT."""
    out = catalogue.search(db)
    for m in out["matches"]:
        assert "variants" not in m
        assert isinstance(m["variant_count"], int)
        assert m["variant_count"] >= 1


def test_the_spread_and_its_bases_are_unchanged(db):
    """_pick_spread untouched: same three cars, same three bases, same order."""
    out = catalogue.search(db)
    assert [m["code"] for m in out["matches"]] == ["JET-X70P", "JET-T1-IDM", "JET-X50"]
    assert [m["basis"] for m in out["matches"]] == [
        "most_seats", "efficiency", "lowest_price"
    ]


# --------------------------------------------------------------------------- #
# Model-level facts derived from trims
# --------------------------------------------------------------------------- #
def test_the_card_carries_a_fuel_economy_band_across_its_trims(db):
    """"My commute is 50km a day" is a running-costs question the catalogue
    could not answer at all before — economy did not exist as data."""
    out = catalogue.search(db)
    t1idm = _match(out, "Jetour T1 i-DM")
    assert (t1idm["km_per_l_min"], t1idm["km_per_l_max"]) == (15.8, 18.5)

    det = catalogue.get_details(db, code="JET-G700")["vehicle"]
    # Spans Comfort 10.0-11.0, Luxury 10.0-11.6, Flagship 11.6 — and IGNORES the
    # Top Fire, which publishes nothing.
    assert (det["km_per_l_min"], det["km_per_l_max"]) == (10.0, 11.6)


def test_an_unpublished_economy_figure_is_null_never_zero(db):
    """A car with no published consumption must not render as 0 km/L, which
    would read as the thirstiest in the range."""
    variants = catalogue._variants_by_model(db, [G700])[G700]
    top_fire = next(v for v in variants if v.name == "Top Fire")
    assert top_fire.km_per_l_min is None
    full = catalogue._variant_full(top_fire)
    assert full["km_per_l_min"] is None and full["km_per_l_max"] is None
    assert full["on_road_aed"] is None


# --------------------------------------------------------------------------- #
# get_variants
# --------------------------------------------------------------------------- #
def test_trims_come_back_ordered_by_price_not_by_name(db):
    """THE T1 IS WHY. Its Luxury is the 4WD range-topper and its Flagship is
    specified close to Comfort, so Luxury is priced ABOVE Flagship. Sorting by
    name — or trusting the seed order — would present a ladder that does not
    exist.
    """
    names = [v["name"] for v in catalogue.get_variants(db, T1)["variants"]]
    assert names == ["Comfort", "Flagship", "Luxury"]


def test_an_unpriced_trim_sorts_last_not_first(db):
    """The G700 Top Fire is coming soon, not cheapest. A null price sorting to
    the front would make it the headline trim of the flagship model."""
    variants = catalogue.get_variants(db, G700)["variants"]
    assert [v["name"] for v in variants] == ["Comfort", "Luxury", "Flagship", "Top Fire"]
    assert variants[-1]["coming_soon"] is True
    assert variants[-1]["price_aed"] is None


def test_get_variants_carries_the_full_commercial_picture(db):
    """It is how Sales produces a variant_id, and how Finance is reached: an
    on-road price and an instalment both need one, and a model id reaches
    neither."""
    comfort = catalogue.get_variants(db, G700)["variants"][0]
    assert comfort["variant_id"]
    assert comfort["price_aed"] == 169_000
    assert comfort["insurance_aed"] == 2_500
    assert comfort["registration_aed"] == 1_000
    assert comfort["on_road_aed"] == 172_500
    assert (comfort["seats_min"], comfort["seats_max"]) == (5, 5)


def test_a_single_unnamed_trim_is_flagged_so_none_is_never_rendered(db):
    """A null name means "the only version", not "unknown". Said explicitly so
    the agent prints the model name alone rather than the word None."""
    out = catalogue.get_variants(db, 3)  # T2 i-DM
    assert out["single_unnamed_variant"] is True
    assert out["variants"][0]["name"] is None

    assert catalogue.get_variants(db, G700)["single_unnamed_variant"] is False
    assert catalogue.get_variants(db, X50)["single_unnamed_variant"] is False


def test_get_variants_carries_the_interior_palette_once(db):
    """Model-level and identical across trims, so it sits beside them rather
    than being repeated on each."""
    out = catalogue.get_variants(db, T1)
    assert out["interior_colours"] == [
        "Black & Orange", "Black & Green", "Black & Light Grey"
    ]
    for v in out["variants"]:
        assert "interior_colours" not in v


def test_get_variants_on_an_unknown_model_is_named_not_guessed(db):
    assert catalogue.get_variants(db, 9999)["error"] == "vehicle_not_found"


# --------------------------------------------------------------------------- #
# get_details
# --------------------------------------------------------------------------- #
def test_get_details_carries_the_trims_so_a_round_trip_is_saved(db):
    """The full path is ONE named model, so the trims come with it. Deliberately
    not on the base dict, which serialises up to six cards per search."""
    det = catalogue.get_details(db, code="JET-G700")["vehicle"]
    assert [v["name"] for v in det["variants"]] == [
        "Comfort", "Luxury", "Flagship", "Top Fire"
    ]
    assert det["interior_colours"] == ["Orange", "Ivory"]


# --------------------------------------------------------------------------- #
# Alternatives
# --------------------------------------------------------------------------- #
def test_the_relaxed_criterion_is_not_reported_back_as_unsatisfied(db):
    """An alternative is offered BECAUSE it fails one criterion. Saying "no trim
    meets your budget" beside a card whose whole point is that it exceeds the
    budget is noise — the `relaxed` field already said it.
    """
    out = catalogue.search(db, min_seats=7, max_price_aed=60_000)
    assert out["no_exact_match"] is True
    alt = out["nearest_alternatives"][0]
    assert alt["relaxed"] == ["max_price_aed"]
    # Reported against the criterion that still stands — seats.
    assert alt["matching_variants"], "the seat constraint should still be checked"
    assert all(v["seats_max"] >= 7 for v in alt["matching_variants"])


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
def test_a_search_does_not_fetch_variants_once_per_card(db):
    """Six cards would be six round trips for a fifteen-row table. The N+1 is
    the easy mistake here, so it is asserted rather than assumed.
    """
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "vehicle_variants" in statement:
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        catalogue.search(db, max_price_aed=180_000)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(seen) == 1, f"{len(seen)} variant queries for one search"
