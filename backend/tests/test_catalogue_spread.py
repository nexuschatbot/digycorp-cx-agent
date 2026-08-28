"""Spread selection and the feature digest (FR-BK-09, FR-CX-07, rule 5).

Two defects are pinned here, both variants of "the tool made a choice it never
actually made":

1. When more than three models fitted, search() returned the first three IN SEED
   ORDER. search_vehicles({}) always answered G700/T2/T2 i-DM and fuel=petrol
   always answered T2/T1/Dashing — never the cheapest car, never a seven-seater
   — while the agent presented them as a considered recommendation.

2. headline_features was returned on every match but searchable by nothing, so
   "which car has the best sound system?" had no path at all.
"""

from __future__ import annotations

from app.services import catalogue


def _codes(rows: list[dict]) -> list[str]:
    return [r["code"] for r in rows]


def _bases(rows: list[dict]) -> list[str | None]:
    return [r.get("basis") for r in rows]


# --------------------------------------------------------------------------- #
# The spread: one best-in-category per basis, not row order
# --------------------------------------------------------------------------- #
def test_unfiltered_search_returns_one_car_per_axis(db):
    """The three cars must answer the three axes of the narrowing question the
    agent closes on — space, running costs, budget. Before the fix this was
    G700/T2/T2 i-DM: two mid-size off-roaders and no seven-seater.
    """
    out = catalogue.search(db)
    # The efficiency pick moved T2 i-DM -> T1 i-DM on 2026-07-27: that basis is
    # the CHEAPEST PHEV, and the T1 i-DM had no published price until then, so it
    # could not win it. Elite's workbook re-priced both at T0.5 — 144,000 against
    # the T2 i-DM's 159,000 — and the T1 i-DM still wins.
    #
    # The seats pick moved X70 FL -> X70 FL PLUS at T0.5, and that one is a
    # CORRECTION, not a re-price: the seed had the two cars' seat ranges swapped,
    # so this axis was answering with the wrong car.
    assert _codes(out["matches"]) == ["JET-X70P", "JET-T1-IDM", "JET-X50"]
    assert _bases(out["matches"]) == ["most_seats", "efficiency", "lowest_price"]


def test_spread_never_repeats_a_model(db):
    out = catalogue.search(db)
    ids = [m["vehicle_id"] for m in out["matches"]]
    assert len(ids) == len(set(ids))


def test_flagship_backfills_when_a_basis_cannot_resolve(db):
    """No petrol car is a PHEV, so `efficiency` cannot resolve here. Without a
    spare basis the third slot would fall back to seed order — the bug itself.
    """
    out = catalogue.search(db, fuel="petrol")
    assert "efficiency" not in _bases(out["matches"])
    assert _bases(out["matches"]) == ["most_seats", "lowest_price", "flagship"]
    assert "JET-X50" in _codes(out["matches"]), "cheapest petrol car must be offered"


def test_x70_plus_wins_most_seats_over_x70_fl(db):
    """Both reach 7. The PLUS is 7-7, the FL is 5-7 — the tiebreak must prefer
    the car that is ALWAYS a seven-seater over the one that optionally is.

    THIS TEST ASSERTED THE OPPOSITE UNTIL T0.5, and it passed, because the seed
    had the two cars' seat ranges exactly swapped. The tiebreak was always right;
    the data underneath it was not. The brochure specification tables settle it:
    the X70 FL's says `5/5+2`, the X70 FL Plus's says `7` with a removable second
    row. Both cars' MARKETING pages say "5/7 seater", which is where the
    confusion came from — quote the spec table.
    """
    out = catalogue.search(db)
    seats = next(m for m in out["matches"] if m.get("basis") == "most_seats")
    assert seats["code"] == "JET-X70P"


def test_unpublished_price_never_wins_a_price_basis(db):
    """No model with a null price may be called cheapest or dearest (FR-CX-07).

    RE-POINTED AT T0.5. This used to name the T1 i-DM, which had no published
    price when it was written and now costs AED 144,000 — so it was passing
    vacuously, which is worse than not existing. Every one of the nine MODELS is
    priced today; the unpriced car is a VARIANT (the G700 Top Fire, coming soon),
    and tests/test_unpriced_vehicle.py guards that path directly.

    Kept as a shape assertion so the invariant survives the next unpriced model.
    """
    for kwargs in ({}, {"fuel": "phev"}, {"body_type": "suv"}):
        out = catalogue.search(db, **kwargs)
        priced = [m for m in out["matches"] if m.get("basis") in {"lowest_price", "flagship"}]
        for m in priced:
            assert m.get("price_aed_min") is not None, m["code"]


def test_basis_values_are_enums_not_display_text(db):
    """LOC-01: a stored English label could not be spoken in an Arabic thread.
    The agent phrases these at speaking time.
    """
    out = catalogue.search(db)
    for m in out["matches"]:
        if "basis" in m:
            assert m["basis"] in catalogue.SPREAD_BASES
            assert m["basis"].islower() and " " not in m["basis"]


def test_spread_is_stable_across_identical_queries(db):
    """A recommendation that changed between two identical questions would read
    as indecision. Every tiebreak ends on id for this reason.
    """
    first = _codes(catalogue.search(db)["matches"])
    for _ in range(5):
        assert _codes(catalogue.search(db)["matches"]) == first


# --------------------------------------------------------------------------- #
# No spread when nothing was withheld
# --------------------------------------------------------------------------- #
def test_three_or_fewer_matches_carry_no_basis(db):
    """Only two cars seat seven, so no selection judgement was made. Labelling
    them would imply a comparison that never happened.
    """
    out = catalogue.search(db, min_seats=7)
    assert len(out["matches"]) <= catalogue.MAX_RECOMMENDATIONS
    assert _bases(out["matches"]) == [None] * len(out["matches"])
    assert "total_matches" not in out


def test_total_matches_is_never_returned(db):
    """No count reaches the agent, withheld or not — see test_catalogue.py."""
    assert "total_matches" not in catalogue.search(db)
    assert "total_matches" not in catalogue.search(db, min_seats=7)


def test_cap_still_holds(db):
    """FR-BK-09 is signed: at most three recommendations, whatever the query."""
    for kwargs in ({}, {"fuel": "petrol"}, {"body_type": "suv"}, {"max_price_aed": 250000}):
        assert len(catalogue.search(db, **kwargs)["matches"]) <= catalogue.MAX_RECOMMENDATIONS


# --------------------------------------------------------------------------- #
# catalogue_digest — the feature path
# --------------------------------------------------------------------------- #
def test_digest_absent_without_feature_query(db):
    """The ordinary path must pay nothing for it."""
    assert "catalogue_digest" not in catalogue.search(db)
    assert "catalogue_digest" not in catalogue.search(db, min_seats=7)


def test_digest_covers_every_match_not_just_the_offered_three(db):
    """The car that answers a feature question is often not one of the three
    offered — the whole point is that the agent can see past them.
    """
    out = catalogue.search(db, feature_query="best sound system")
    assert len(out["catalogue_digest"]) == 9
    assert len(out["matches"]) == 3


def test_digest_reaches_the_audio_cars(db):
    """'sound' appears NOWHERE in the catalogue — the features say 'audio'.
    That is why this is a digest for the model to read, not a keyword filter.
    """
    out = catalogue.search(db, feature_query="best sound system")
    text = {d["name"]: " ".join(d["headline_features"]) for d in out["catalogue_digest"]}
    assert "Lexicon" in text["Jetour G700"]
    assert "Sony" in text["Jetour T2 Luxury Plus"]
    assert not any("sound" in t.lower() for t in text.values())


def test_digest_carries_no_price(db):
    """THE STRUCTURAL GUARD. With no price in the digest the agent cannot answer
    a price question from it and must call get_vehicle_details — which
    re-grounds the claim (FR-CX-07) and fires the vehicle card (rule 18).
    """
    out = catalogue.search(db, feature_query="biggest screen")
    for entry in out["catalogue_digest"]:
        assert set(entry) == {"vehicle_id", "name", "headline_features"}


def test_digest_is_scoped_by_the_structured_filters(db):
    """'a seven-seater with good audio' digests the seven-seaters, not all nine."""
    out = catalogue.search(db, min_seats=7, feature_query="good audio")
    assert {d["name"] for d in out["catalogue_digest"]} == {"Jetour X70 FL Plus", "Jetour X70 FL"}


def test_digest_omitted_when_nothing_matched(db):
    """Nothing to reason over — the agent should be using nearest_alternatives."""
    out = catalogue.search(db, min_seats=7, max_price_aed=50000, feature_query="good audio")
    assert out["no_exact_match"] is True
    assert "catalogue_digest" not in out


def test_feature_query_alone_does_not_filter_anything(db):
    """It is a signal, not a filter: the match set must be identical with and
    without it, or it has quietly become a second, weaker search.
    """
    assert _codes(catalogue.search(db)["matches"]) == _codes(
        catalogue.search(db, feature_query="tows a boat")["matches"]
    )


# --------------------------------------------------------------------------- #
# Honesty is unchanged (FR-CX-07)
# --------------------------------------------------------------------------- #
def test_unavailable_things_are_still_refused_not_approximated(db):
    """The spread must never become a consolation prize. An EV request and an
    unstocked brand both still return nothing rather than three near-misses.
    """
    ev = catalogue.search(db, fuel="electric", feature_query="best range")
    assert ev["error"] == "unsupported_fuel" and ev["matches"] == []

    ferrari = catalogue.search(db, brand="Ferrari")
    assert ferrari["matches"] == [] and ferrari["no_exact_match"] is True
    assert ferrari["nearest_alternatives"] == []
