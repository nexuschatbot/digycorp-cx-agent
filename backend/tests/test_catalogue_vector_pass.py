"""The additive catalogue vector pass, and its measured limits (T1.6).

WHAT IT IS FOR. `body_type` is free text matched as a literal SUBSTRING, so a
customer phrase that is not a word in the catalogue matches nothing at all and is
silently discarded — "family car", "off roader", "something for camping" all
return zero rows today.

WHAT IT IS NOT FOR, RECORDED HERE SO NOBODY RE-ATTEMPTS IT. When the literal word
DOES appear in most rows, the embedding cannot beat lexical overlap and the pass
deliberately does not fire. `body_type="suv"` matches 8 of 9 rows; the X50
("Compact crossover") ranks 9th of 9 at 0.8011, below every substring match. That
is a real customer-visible miss and it is NOT fixed here — see the failing case
asserted at the bottom of this file, which is a specification, not a bug.

Off by default. Every test that needs it patches the flag rather than relying on
the environment, so the suite's meaning does not depend on a shell variable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import VehicleModel
from app.retrieval import catalogue_pass, embed
from app.services import catalogue

needs_model = pytest.mark.skipif(
    not embed.available(), reason="embedding model not vendored in this checkout"
)


@pytest.fixture()
def on(monkeypatch):
    """RETRIEVAL_ENABLED, for one test."""
    monkeypatch.setattr(catalogue, "RETRIEVAL_ENABLED", True)
    catalogue_pass.reset()
    yield
    catalogue_pass.reset()


def _names(out: dict) -> list[str]:
    return [m["name"].replace("Jetour ", "") for m in out["matches"]]


# --------------------------------------------------------------------------- #
# Off is off
# --------------------------------------------------------------------------- #
def test_disabled_by_default(db):
    """OFF MUST MEAN OFF — not "runs the vector pass and discards it". Phase 1
    ships changing nothing a customer sees until the flag is flipped."""
    assert catalogue.RETRIEVAL_ENABLED is False
    out = catalogue.search(db, body_type="off roader")
    assert out["matches"] == []
    assert "interpreted_loosely" not in out


def test_an_unfiltered_browse_is_untouched_even_when_enabled(db, on):
    """No free-text phrase was given, so there is nothing to loosen. The spread
    must be exactly what it was before Phase 1."""
    out = catalogue.search(db)
    assert _names(out) == ["X70 FL Plus", "T1 i-DM", "X50"]
    assert "interpreted_loosely" not in out


# --------------------------------------------------------------------------- #
# What it rescues
# --------------------------------------------------------------------------- #
@needs_model
@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("off roader", "T1"),
        ("family car", "X70 FL"),
        ("something for camping", "T1"),
    ],
)
def test_a_phrase_the_substring_filter_cannot_match_is_rescued(db, on, phrase, expected):
    """These return ZERO rows today — the customer's words are discarded and the
    generic spread comes back as though they had asked for nothing."""
    rows = db.scalars(select(VehicleModel)).all()
    no_filters = dict(min_seats=None, max_price_aed=None, fuel=None, brand=None)
    assert catalogue._apply_filters(rows, body_type=phrase, **no_filters) == [], (
        f"{phrase!r} is supposed to be a substring miss"
    )

    out = catalogue.search(db, body_type=phrase)
    assert expected in _names(out), f"{phrase!r} -> {_names(out)}"


@needs_model
def test_a_rescued_result_says_so(db, on):
    """HONEST ABOUT WHAT HAPPENED. The customer's words matched no row literally,
    so the agent must offer these as her reading of the request — not as cars
    that met a stated criterion."""
    out = catalogue.search(db, body_type="off roader")
    assert out["interpreted_loosely"] == "off roader"
    assert out["no_exact_match"] is False


@needs_model
def test_at_most_three_are_ever_added(db, on):
    """FR-BK-09 is signed: at most three recommendations, whatever the query.
    A rescue must not become a fourth card."""
    for phrase in ("off roader", "family car", "city car", "something for camping"):
        assert len(catalogue.search(db, body_type=phrase)["matches"]) <= 3


# --------------------------------------------------------------------------- #
# What it may never do
# --------------------------------------------------------------------------- #
@needs_model
def test_a_budget_is_never_widened_by_a_similarity_score(db, on):
    """THE GROUNDING GUARD. Without it, "under 75,000" picks up a 169,000 G700
    because it sounds roomy — a failure dressed as a feature."""
    out = catalogue.search(db, body_type="family car", max_price_aed=75_000)
    assert _names(out), "the rescue should still find something in budget"
    for m in out["matches"]:
        assert m["price_aed_min"] <= 75_000, m


@needs_model
def test_a_seat_constraint_is_never_widened_either(db, on):
    out = catalogue.search(db, body_type="something for camping", min_seats=7)
    for m in out["matches"]:
        assert m["seats_max"] >= 7, m


@needs_model
def test_it_never_fires_when_the_substring_filter_already_matched(db, on):
    """ADDITIVE MEANS IT ADDS WHERE THERE WAS NOTHING. With rows already matched
    there is nothing to remove and nothing to reorder, because it does not run.
    """
    baseline = catalogue.search(db, body_type="suv")
    assert baseline["matches"]
    assert "interpreted_loosely" not in baseline

    rows = db.scalars(select(VehicleModel)).all()
    assert catalogue_pass.additional_candidates(
        rows, rows[:2], body_type="suv", feature_query=None,
        still_satisfies=lambda v: True,
    ) == []


@needs_model
def test_the_x50_suv_miss_is_a_known_limitation_not_a_regression(db, on):
    """A SPECIFICATION, NOT A BUG — and the reason this test exists is so nobody
    "fixes" it by loosening the admission rule.

    "Show me an SUV under 80,000" still hides the X50 at AED 69,000, because its
    body_type is "Compact crossover" and the substring filter matches 8 of 9
    rows without it. The embedding ranks the X50 LAST of nine for the query
    "suv" (0.8011, against 0.8123-0.8382 for the matches), so no threshold
    separates it from noise — the whole score range on this corpus is 0.04 wide.

    Fixing this needs a different remedy than a vector pass. When one lands,
    delete this test in the same commit.
    """
    out = catalogue.search(db, body_type="suv", max_price_aed=80_000)
    assert "X50" not in _names(out)

    # And the evidence for why, so the claim above is checkable rather than
    # asserted: the X50 really is ranked below every substring match.
    rows = list(db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all())
    import numpy as np

    scores = np.asarray(catalogue_pass._vectors(rows)) @ np.asarray(
        embed.embed_query("suv")
    )
    by_name = {v.name: float(s) for v, s in zip(rows, scores)}
    matched = [n for n, v in by_name.items() if "suv" in (
        next(r for r in rows if r.name == n).body_type or "").lower()]
    assert by_name["Jetour X50"] < min(by_name[n] for n in matched)


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_a_dead_embedder_leaves_search_working(db, on, monkeypatch):
    """Retrieval must never break a search. With no model the query behaves
    exactly as it did before Phase 1."""
    monkeypatch.setattr(embed, "embed_query", lambda *a, **k: None)
    monkeypatch.setattr(embed, "embed_passages", lambda *a, **k: None)
    catalogue_pass.reset()
    out = catalogue.search(db, body_type="off roader")
    assert out["matches"] == []
    assert "interpreted_loosely" not in out


def test_an_exception_in_the_pass_is_swallowed(db, on, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("vector pass exploded")

    monkeypatch.setattr(catalogue_pass, "additional_candidates", _boom)
    out = catalogue.search(db, body_type="off roader")
    assert out["matches"] == []
    assert out["no_exact_match"] is True
