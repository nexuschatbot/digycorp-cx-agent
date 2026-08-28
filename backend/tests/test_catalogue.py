"""Catalogue service tests (FR-BK-04/09/10, FR-CX-07)."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.models import VehicleModel
from app.services import catalogue


def _codes(rows: list[dict]) -> set[str]:
    return {r["code"] for r in rows}


# --------------------------------------------------------------------------- #
# seats are RANGES (FR-BK-09/10)
# --------------------------------------------------------------------------- #
def test_min_seats_7_matches_x70_plus_via_seats_max(db):
    """The X70 Plus is 5-7. Filtering on a single seat value would hide the
    main family SUV from every "I need a 7-seater" conversation.
    """
    out = catalogue.search(db, min_seats=7)
    assert "JET-X70P" in _codes(out["matches"]), "X70 Plus (5-7) must satisfy min_seats=7"
    assert "JET-X70FL" in _codes(out["matches"])
    assert out["no_exact_match"] is False


def test_min_seats_7_excludes_five_seaters(db):
    out = catalogue.search(db, min_seats=7)
    assert "JET-X50" not in _codes(out["matches"])
    assert "JET-T2" not in _codes(out["matches"])


def test_min_seats_6_matches_g700_range(db):
    """G700 is 5 or 6 — seats_max=6 satisfies a 6-seat need."""
    assert "JET-G700" in _codes(catalogue.search(db, min_seats=6)["matches"])
    assert "JET-G700" not in _codes(catalogue.search(db, min_seats=7)["matches"])


# --------------------------------------------------------------------------- #
# EVERY model carries a published price (product decision 2026-07-27)
#
# price_aed_min/max used to be NULLABLE, and null meant "not published" — a real
# state with real machinery behind it: a price_unknown bucket in search(), a
# price_published flag on the detail dict, and null guards in three of the four
# ranking bases. Every Jetour model now has a published AED range, so that
# machinery came out rather than being left as branches no data could reach.
#
# These two tests are what stops it drifting back in silently. A model seeded
# without a price would now sort as free in lowest_price and disappear from every
# budget query, so the invariant is asserted against the DATA, not just the type.
# --------------------------------------------------------------------------- #
def test_every_seeded_model_has_a_published_price(db):
    missing = [
        v.model_code
        for v in db.scalars(select(VehicleModel)).all()
        if v.price_aed_min is None or v.price_aed_max is None
    ]
    assert missing == [], (
        f"{missing} carry no price. The nullable-price path was REMOVED on "
        "2026-07-27 — a priceless model now ranks as free and vanishes from "
        "budget filters rather than being handled. Restore the price, or reopen "
        "the decision properly (see VehicleModel.price_aed_min)."
    )


def test_search_no_longer_returns_a_price_unknown_bucket(db):
    """The bucket is gone, including on the path that used to populate it."""
    assert "price_unknown" not in catalogue.search(db)
    assert "price_unknown" not in catalogue.search(db, max_price_aed=130000)


def test_price_never_serialises_as_the_string_none(db):
    """Kept from the nullable era: a customer must never be shown "None"/"null",
    whatever a future refactor does to these fields."""
    blob = json.dumps(catalogue.search(db), ensure_ascii=False)
    assert '"None"' not in blob and '"null"' not in blob
    for v in catalogue.search(db)["matches"]:
        for field in ("price_aed_min", "price_aed_max"):
            assert isinstance(v[field], int)


def test_matches_are_capped_at_three(db):
    """Rule 5 / FR-BK-09: at most three recommendations, enforced HERE rather
    than trusted to the prompt — the same reason get_available_slots caps its
    offers server-side. Before this, a matching query returned every hit, so a
    broad search handed the agent the entire nine-model catalogue."""
    out = catalogue.search(db)
    assert len(out["matches"]) == 3


def test_search_never_reports_a_total(db):
    """The agent must never state how many models exist or matched.

    Enforced by WITHHOLDING the number (rule 14's lock-TTL corollary), not by
    asking the prompt to resist it: she volunteered "we have 9 Jetour models in
    total" to a customer, and then described the three-car cap as a system limit
    when pushed for the rest. This asserts the inverse of what it used to —
    deliberately. Do not restore total_matches without reading services/
    catalogue.search.
    """
    out = catalogue.search(db)
    assert len(out["matches"]) == 3
    assert "total_matches" not in out

    narrow = catalogue.search(db, fuel="phev")
    assert len(narrow["matches"]) == 3 and "total_matches" not in narrow


def test_null_price_model_does_not_crash_price_sorted_alternatives(db):
    out = catalogue.search(db, min_seats=7, max_price_aed=60000)
    assert out["no_exact_match"] is True
    assert out["nearest_alternatives"]


# --------------------------------------------------------------------------- #
# no-match must be USEFUL (FR-BK-10)
# --------------------------------------------------------------------------- #
def test_seven_seater_under_60k_offers_the_cheapest_real_seven_seater(db):
    """The honest answer is "our 7-seater starts at AED 79,000".

    The figure was 73,000 until T0.5, when Elite's workbook re-priced the X70 FL.
    The CAR is unchanged: it is still the cheapest thing that can seat seven,
    because min_seats filters on seats_max and the FL's is 7 (a 5+2 third row).
    """
    out = catalogue.search(db, min_seats=7, max_price_aed=60000)
    assert out["no_exact_match"] is True
    alts = out["nearest_alternatives"]
    assert alts, "a no-match must offer the nearest real alternative"
    assert alts[0]["code"] == "JET-X70FL"
    assert alts[0]["price_aed_min"] == 79000
    assert alts[0]["relaxed"] == ["max_price_aed"]


def test_alternatives_never_relax_min_seats(db):
    """A 5-seater is not an alternative for someone who needs 7."""
    out = catalogue.search(db, min_seats=7, max_price_aed=60000)
    for alt in out["nearest_alternatives"]:
        assert alt["seats_max"] >= 7


def test_conflicting_criteria_still_return_something_honest(db):
    """"7-seater coupe" — nothing will ever match; relax body_type."""
    out = catalogue.search(db, min_seats=7, body_type="coupe")
    assert out["no_exact_match"] is True
    assert out["nearest_alternatives"]
    assert out["nearest_alternatives"][0]["relaxed"] == ["body_type"]
    for alt in out["nearest_alternatives"]:
        assert alt["seats_max"] >= 7


def test_alternatives_capped_at_three(db):
    out = catalogue.search(db, max_price_aed=1000)
    assert len(out["nearest_alternatives"]) <= 3


def test_unknown_brand_is_an_honest_no_not_a_substitute(db):
    """Offering a Jetour to someone asking for a Toyota would be dishonest."""
    out = catalogue.search(db, brand="Toyota")
    assert out["matches"] == []
    assert out["no_exact_match"] is True
    assert out["nearest_alternatives"] == []


# --------------------------------------------------------------------------- #
# fuel enum — the catalogue has ONLY petrol and phev
# --------------------------------------------------------------------------- #
def test_phev_matches_only_the_three_hybrids(db):
    out = catalogue.search(db, fuel="phev")
    assert _codes(out["matches"]) == {"JET-G700", "JET-T2-IDM", "JET-T1-IDM"}


def test_electric_request_gets_an_honest_no(db):
    """We have no EV — the answer must say so, not silently return nothing."""
    out = catalogue.search(db, fuel="electric")
    assert out["error"] == "unsupported_fuel"
    assert out["available_fuel_types"] == ["petrol", "phev"]
    assert out["matches"] == []


def test_body_type_substring_is_case_insensitive(db):
    assert _codes(catalogue.search(db, body_type="suv")["matches"])
    assert _codes(catalogue.search(db, body_type="SUV")["matches"]) == _codes(
        catalogue.search(db, body_type="suv")["matches"]
    )
    assert _codes(catalogue.search(db, body_type="crossover")["matches"]) == {"JET-X50"}


# --------------------------------------------------------------------------- #
# ambiguity must never be resolved silently (FR-BK-04)
# --------------------------------------------------------------------------- #
def test_t2_is_ambiguous_and_returns_candidates(db):
    """"the T2" is both the T2 (petrol) and the T2 i-DM (PHEV) — different
    cars. Picking one silently could book the wrong car.
    """
    out = catalogue.get_details(db, name="T2")
    assert out.get("ambiguous") is True
    assert _codes(out["candidates"]) == {"JET-T2", "JET-T2-IDM"}
    assert "vehicle" not in out


def test_t1_and_x70_are_ambiguous_too(db):
    assert _codes(catalogue.get_details(db, name="T1")["candidates"]) == {
        "JET-T1",
        "JET-T1-IDM",
    }
    assert _codes(catalogue.get_details(db, name="X70")["candidates"]) == {
        "JET-X70P",
        "JET-X70FL",
    }


def test_exact_full_name_is_not_ambiguous_despite_being_a_prefix(db):
    """"Jetour X70 FL" is an exact name even though "Jetour X70 FL Plus" contains it.

    RE-POINTED AT T0.5. The pair used to be "Jetour T2" inside "Jetour T2 i-DM";
    the workbook renamed that model to "Jetour T2 Luxury Plus", so the bare
    "Jetour T2" is no longer any model's exact name and could not test this. The
    X70s are now the exact same shape, and closer to home — customers say "the
    X70" constantly.
    """
    out = catalogue.get_details(db, name="Jetour X70 FL")
    assert out["vehicle"]["code"] == "JET-X70FL"


def test_the_bare_t2_is_now_ambiguous_between_the_two_t2s(db):
    """A PRODUCT CHANGE, not a bug, recorded so it is not "fixed" later.

    Elite sells two T2s — the T2 Luxury Plus and the T2 i-DM — and neither is
    called plain "Jetour T2" any more. Someone saying "the T2" has genuinely not
    said which, so the honest response is the disambiguation path, not a guess at
    the petrol one. Before T0.5 the petrol car WAS named "Jetour T2" and won this
    outright.
    """
    out = catalogue.get_details(db, name="Jetour T2")
    assert "vehicle" not in out
    assert {c["code"] for c in out["candidates"]} == {"JET-T2", "JET-T2-IDM"}


def test_code_match_wins_outright(db):
    assert catalogue.get_details(db, code="JET-T2-IDM")["vehicle"]["code"] == "JET-T2-IDM"


def test_unknown_model_is_not_fuzzy_matched(db):
    out = catalogue.get_details(db, name="Land Cruiser")
    assert out["error"] == "vehicle_not_found"
    assert "vehicle" not in out and "candidates" not in out


def test_get_details_with_no_query_returns_error_not_crash(db):
    assert catalogue.get_details(db)["error"] == "missing_query"


# --------------------------------------------------------------------------- #
# showrooms
# --------------------------------------------------------------------------- #
def test_al_ain_resolves_to_abu_dhabi(db):
    """"Al Ain" is a city, not an emirate — the column holds abu_dhabi."""
    out = catalogue.list_showrooms(db, emirate="Al Ain")
    names = {s["name"] for s in out["showrooms"]}
    assert "Jetour Al Ain" in names


def test_emirate_input_is_normalised(db):
    """The customer types "Abu Dhabi"; the column holds "abu_dhabi"."""
    assert catalogue.list_showrooms(db, emirate="Abu Dhabi")["showrooms"]
    assert catalogue.list_showrooms(db, emirate="abu_dhabi")["showrooms"]
    assert catalogue.list_showrooms(db, emirate="ABU DHABI")["showrooms"]


def test_arabic_emirate_input_resolves(db):
    assert catalogue.list_showrooms(db, emirate="دبي")["showrooms"]


def test_unknown_place_is_empty_not_an_error(db):
    out = catalogue.list_showrooms(db, emirate="Riyadh")
    assert out["showrooms"] == []
    assert "error" not in out


def test_vehicle_filter_excludes_the_g700_dedicated_showroom(db):
    """Listing all six for "where can I see the X70?" would send the customer
    to a showroom the X70 is not in.
    """
    x70 = catalogue.get_details(db, code="JET-X70P")["vehicle"]
    out = catalogue.list_showrooms(db, vehicle_id=x70["vehicle_id"])
    ids = {s["showroom_id"] for s in out["showrooms"]}
    assert 2 not in ids
    assert ids == {1, 3, 4, 5, 6}


def test_the_g700_is_shown_at_every_remaining_showroom(db):
    """Was test_g700_is_the_only_vehicle_in_showroom_2 until 2026-07-23, when the
    G700-dedicated Al Quoz branch was removed from the seed. The old assertion
    (`2 in ids`) described a fixture rather than a rule, so it died with the row.
    What is worth pinning instead is that the removal did not strand the
    flagship: it listed showroom 2 alongside five others and must still be
    visible at all five."""
    g700 = catalogue.get_details(db, code="JET-G700")["vehicle"]
    ids = {s["showroom_id"] for s in catalogue.list_showrooms(db, vehicle_id=g700["vehicle_id"])["showrooms"]}
    assert ids == {1, 3, 4, 5, 6}


def test_vehicle_and_emirate_filters_combine(db):
    x70 = catalogue.get_details(db, code="JET-X70P")["vehicle"]
    out = catalogue.list_showrooms(db, vehicle_id=x70["vehicle_id"], emirate="Al Ain")
    names = {s["name"] for s in out["showrooms"]}
    # "Al Ain" widens to the emirate abu_dhabi, so BOTH Abu Dhabi showrooms come
    # back — Rabdan is a real (if distant) alternative, not a wrong answer. The
    # agent is no longer left to work out which is which: each row carries
    # match_type, and the Al Ain branch leads. See the match_type tests below.
    assert names == {"Jetour Al Ain", "Jetour Abu Dhabi (Rabdan)"}
    assert out["showrooms"][0]["name"] == "Jetour Al Ain"


# --------------------------------------------------------------------------- #
# match_type — a data field, not a name to be parsed
# --------------------------------------------------------------------------- #
def test_al_ain_is_a_city_match_and_ranks_first(db):
    """The whole point: "anything in Al Ain?" must not leave the agent working
    out which of two abu_dhabi showrooms is the Al Ain one.
    """
    rows = catalogue.list_showrooms(db, emirate="Al Ain")["showrooms"]
    assert rows[0]["name"] == "Jetour Al Ain"
    assert rows[0]["match_type"] == "city"


def test_rabdan_is_an_emirate_match_not_a_city_match(db):
    """A real alternative, honestly labelled as not what they asked for."""
    rows = catalogue.list_showrooms(db, emirate="Al Ain")["showrooms"]
    rabdan = next(s for s in rows if s["name"] == "Jetour Abu Dhabi (Rabdan)")
    assert rabdan["match_type"] == "emirate"


def test_deira_ranks_the_deira_showroom_first(db):
    """The CLAUDE.md worked example says "Deira, Saturday evening" — a district,
    not an emirate. It has to rank too, or the widening is useless there.
    """
    rows = catalogue.list_showrooms(db, emirate="Deira")["showrooms"]
    assert rows[0]["name"] == "Jetour Deira"
    assert rows[0]["match_type"] == "city"
    assert [s["match_type"] for s in rows[1:]] == ["emirate"] * (len(rows) - 1)


def test_an_emirate_word_produces_no_city_matches(db):
    """"Dubai" names no locality, so no showroom may claim to be the one they
    named — every Dubai row is an equal emirate match.
    """
    rows = catalogue.list_showrooms(db, emirate="Dubai")["showrooms"]
    assert rows
    assert {s["match_type"] for s in rows} == {"emirate"}


def test_match_type_is_absent_when_no_place_was_named(db):
    """With nothing to match against, any value would be a claim we cannot
    support.
    """
    for s in catalogue.list_showrooms(db)["showrooms"]:
        assert "match_type" not in s


def test_arabic_city_input_resolves_and_ranks(db):
    rows = catalogue.list_showrooms(db, emirate="العين")["showrooms"]
    assert rows[0]["match_type"] == "city"
    assert rows[0]["name"] == "Jetour Al Ain"


def test_city_is_exposed_as_data(db):
    """The agent must never have to read a location out of the display name."""
    rows = catalogue.list_showrooms(db, emirate="Al Ain")["showrooms"]
    assert rows[0]["city"] == "al_ain"
    assert rows[0]["emirate"] == "abu_dhabi"


def test_match_type_does_not_come_from_the_name(db):
    """Showroom 1 is "Jetour Al Quoz" — its name contains the locality, but so
    would a substring hack. Rename it and the classification must not move: it
    is read from the city COLUMN.

    Used to rename showroom 2 for this; that row was removed from the seed on
    2026-07-23, so the subject moved to showroom 1. The test is unchanged in
    substance — it only ever needed A showroom whose display name happens to
    contain the place it sits in.
    """
    from app.models import Showroom

    s = db.get(Showroom, 1)
    s.name = "Jetour Flagship"  # no locality anywhere in the display text
    db.flush()

    rows = catalogue.list_showrooms(db, emirate="Al Quoz")["showrooms"]
    renamed = next(r for r in rows if r["showroom_id"] == 1)
    assert renamed["match_type"] == "city"


def test_unknown_vehicle_filter_returns_error_not_crash(db):
    out = catalogue.list_showrooms(db, vehicle_id=99)
    assert out["error"] == "vehicle_not_found"
    assert out["showrooms"] == []


def test_showrooms_expose_structured_hours_not_display_text(db):
    s = catalogue.list_showrooms(db)["showrooms"][0]
    assert s["hours_by_day"]["sun"] == [12, 21]
    assert "hours" not in s, "display text must not reach the agent (LOC-01)"
