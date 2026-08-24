"""Ranked answers to superlative questions (FR-CX-07, rule 2).

THE DEFECT THIS PINS, observed live 2026-07-23. Asked "what is the most
expensive car?", the agent called search_vehicles({}) and answered "the T2 i-DM
is our top-of-range model". It is not — the G700 (AED 179,000–229,000) and the
T2 (146,000–179,000) both cost more.

Nothing was hallucinated: every SPEC it quoted was the T2 i-DM's real seeded
feature text. What it got wrong was the RANKING, and it got that wrong because
SPREAD_BASES lists four bases while MAX_RECOMMENDATIONS caps at three. `flagship`
is last, so on an unfiltered search it is ALWAYS the one dropped — the agent was
handed X70 FL / T2 i-DM / X50, the flagship structurally absent, and no signal
that anything had been withheld. It named the priciest of the three it could
see.

So the fix is a ROUTE, not a sterner sentence: the tool description already said
the spread is "NOT a ranking: the first is not 'the best'" and the model ranked
it anyway. `basis` lets a determinate question be asked as one.

The regression that matters most here is the LAST test: discovery must not move.
"""

from __future__ import annotations

from app.services import catalogue


def _names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows]


# --------------------------------------------------------------------------- #
# The three measures, each leading with the right car
# --------------------------------------------------------------------------- #
def test_flagship_leads_with_the_most_expensive_car(db):
    """The defect, in one assertion: the G700 must be first, not the T2 i-DM."""
    out = catalogue.search(db, basis="flagship")

    assert _names(out["matches"])[0] == "Jetour G700"
    assert out["ranked_by"] == "flagship"
    assert out["no_exact_match"] is False


def test_flagship_is_ordered_by_top_price_descending(db):
    out = catalogue.search(db, basis="flagship")
    prices = [m["price_aed_max"] for m in out["matches"]]

    assert prices == sorted(prices, reverse=True)


def test_lowest_price_leads_with_the_cheapest_car(db):
    out = catalogue.search(db, basis="lowest_price")

    assert _names(out["matches"])[0] == "Jetour X50"
    assert out["ranked_by"] == "lowest_price"


def test_most_seats_leads_with_the_seven_seater(db):
    """X70 FL (7-7) over X70 Plus (5-7): always a seven-seater beats optionally
    one — the same tiebreak the spread uses, because it is the same sort."""
    out = catalogue.search(db, basis="most_seats")

    assert _names(out["matches"])[0] == "Jetour X70 FL"


# --------------------------------------------------------------------------- #
# Shape: an answer, with the range still in view
# --------------------------------------------------------------------------- #
def test_ranking_returns_three_not_one(db):
    """Deliberately not just the winner. Answering "the G700, that's it" ends
    the conversation; a showroom host names the leader and keeps the range in
    view. It also keeps the card row honest — the reported symptom was cards for
    cars the agent never mentioned, and ranked, all three answer the question."""
    assert len(catalogue.search(db, basis="flagship")["matches"]) == 3


def test_ranked_matches_carry_no_per_match_basis(db):
    """`basis` per match means "why this car when others also fitted" — a
    category label for a spread. Ranked, every car is there for the same reason
    and the ORDER carries the meaning, so the measure is stated once in
    ranked_by. Labelling each match would tell the agent these are
    one-per-category picks: exactly the misreading that caused the defect."""
    out = catalogue.search(db, basis="flagship")

    assert all("basis" not in m for m in out["matches"])


# --------------------------------------------------------------------------- #
# Composition with the ordinary filters — no code of its own
# --------------------------------------------------------------------------- #
def test_ranking_composes_with_filters(db):
    """"Your most expensive seven-seater" is basis + min_seats, and the G700
    (5-6 seats) must fall out of it — the filter runs first, the ranking second."""
    out = catalogue.search(db, basis="flagship", min_seats=7)

    assert _names(out["matches"])[0] == "Jetour X70 FL"
    assert "Jetour G700" not in _names(out["matches"])


def test_ranking_excludes_unpublished_prices(db):
    """The T1 i-DM has no published price. It cannot be called the most or least
    expensive car (FR-CX-07) — we do not know what it costs."""
    for basis in ("flagship", "lowest_price"):
        assert "Jetour T1 i-DM" not in _names(catalogue.search(db, basis=basis)["matches"])


# --------------------------------------------------------------------------- #
# Honest failure, never a guess
# --------------------------------------------------------------------------- #
def test_unresolvable_basis_reports_no_match_and_offers_alternatives(db):
    """No petrol car is a hybrid, so "the most economical petrol car" has
    candidates but no answer. It must fall through to nearest_alternatives
    rather than return an empty match list dressed as a success — which is why
    no_exact_match keys on what was OFFERED, not on what matched."""
    out = catalogue.search(db, basis="efficiency", fuel="petrol")

    assert out["matches"] == []
    assert out["no_exact_match"] is True
    assert out["nearest_alternatives"]
    assert "ranked_by" not in out


def test_unknown_basis_never_guesses(db):
    out = catalogue.search(db, basis="not_a_real_basis")

    assert out["matches"] == []
    assert out["no_exact_match"] is True


# --------------------------------------------------------------------------- #
# THE REGRESSION THAT MATTERS: discovery is untouched
# --------------------------------------------------------------------------- #
def test_search_without_basis_is_unchanged(db):
    """The spread still spreads, still labels each pick with its own basis, and
    carries no ranked_by. If this fails, the fix broke needs-based discovery to
    serve superlatives — the wrong trade entirely."""
    out = catalogue.search(db)

    assert "ranked_by" not in out
    # T1 i-DM, not T2 i-DM: `efficiency` is the cheapest PHEV and the T1 i-DM
    # gained a published price on 2026-07-27 (AED 129,000 vs the T2 i-DM's
    # 144,000). The SPREAD is unchanged — only which car wins one axis.
    assert _names(out["matches"]) == ["Jetour X70 FL", "Jetour T1 i-DM", "Jetour X50"]
    assert [m.get("basis") for m in out["matches"]] == [
        "most_seats",
        "efficiency",
        "lowest_price",
    ]
