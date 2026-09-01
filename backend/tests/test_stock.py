"""Stock service: availability, never counts (T0.8).

The load-bearing test in this file is `test_no_return_value_anywhere_carries_a
_unit_count`. It walks every value of every return shape and asserts that no
real unit count appears, for every query the service supports — so the rule is
enforced by the tests rather than by an agent remembering a prompt line.

The rest is about the four states. "No" means four different things here, and
each needs a different sentence from Aisha.

Offline: the seeded `db`, no vendor, no model.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import VehicleModel, VehicleVariant
from app.services import stock

AL_QUOZ, DEIRA, ABU_DHABI, AL_AIN, AJMAN = 1, 3, 4, 5, 6


def _model(db, code: str) -> VehicleModel:
    return db.scalar(select(VehicleModel).where(VehicleModel.model_code == code))


def _variant(db, code: str, name: str | None) -> VehicleVariant:
    m = _model(db, code)
    return db.scalar(
        select(VehicleVariant).where(
            VehicleVariant.model_id == m.id, VehicleVariant.name == name
        )
    )


def _ints_by_key(value, key: str | None = None) -> list[tuple[str | None, int]]:
    """Every integer in a nested structure, paired with the key it sits under."""
    found: list[tuple[str | None, int]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            found += _ints_by_key(v, k)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            found += _ints_by_key(v, key)
    elif isinstance(value, bool):
        pass  # bool is an int subclass; a yes/no is exactly what we want
    elif isinstance(value, int):
        found.append((key, value))
    return found


def _every_shape(db) -> list[dict]:
    """One of every return shape this service can produce."""
    g700 = _model(db, "JET-G700")
    flagship = _variant(db, "JET-G700", "Flagship")
    luxury = _variant(db, "JET-G700", "Luxury")
    top_fire = _variant(db, "JET-G700", "Top Fire")
    t2 = _variant(db, "JET-T2", None)
    return [
        stock.availability(db, variant_id=flagship.id),
        stock.availability(db, variant_id=flagship.id, colour="Matte Orange"),
        stock.availability(db, variant_id=flagship.id, showroom_id=AL_QUOZ),
        stock.availability(db, variant_id=flagship.id, colour="Lime Green"),
        stock.availability(db, variant_id=top_fire.id),
        stock.availability(db, model_id=g700.id),
        stock.availability(db, model_id=g700.id, colour="white"),
        stock.availability(db, variant_id=t2.id, colour="Night Black"),
        stock.colours_available(db, variant_id=flagship.id),
        stock.colours_available(db, variant_id=luxury.id, showroom_id=DEIRA),
        stock.colours_available(db, variant_id=top_fire.id),
        stock.colours_available(db, model_id=g700.id),
    ]


# --------------------------------------------------------------------------- #
# THE RULE
# --------------------------------------------------------------------------- #
def test_the_only_numbers_this_service_returns_are_identifiers(db):
    """Nothing decrements `units` when a car sells, so every count here is only
    as fresh as the last import. A stale "twelve at Deira" is a promise we cannot
    keep and a customer drives across town on; a stale "yes, we have it" degrades
    into a conversation at the showroom, which is where it belonged.

    A WHITELIST, NOT A BLACKLIST, and the difference was found the hard way. The
    first version of this test collected the real per-row counts and asserted
    none appeared. Tampering the service to return `total_units` — a SUM across
    rows — sailed straight past it, because a sum is not any individual row's
    count. Blacklisting values only catches the leak you already thought of.

    So the invariant is inverted: every integer this service returns must sit
    under an `*_id` key. Counts, sums, totals, averages and "we have a few"
    scores all fail it, including the ones nobody has invented yet.
    """
    for out in _every_shape(db):
        offenders = [
            (k, n) for k, n in _ints_by_key(out) if not (k or "").endswith("_id")
        ]
        assert not offenders, f"non-identifier numbers {offenders} in {out}"


def test_no_returned_key_is_named_like_a_count(db):
    """Belt and braces on the shape as well as the values: a `units` key holding
    a boolean would satisfy the test above and still invite the wrong answer.

    Matched as a SUBSTRING — `total_units` is the leak that got through an
    exact-match version of this check.
    """
    banned = ("unit", "count", "quantity", "qty", "stock_level", "how_many", "total")

    def keys(value):
        out = set()
        if isinstance(value, dict):
            for k, v in value.items():
                out.add(k)
                out |= keys(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                out |= keys(v)
        return out

    for out in _every_shape(db):
        for k in keys(out):
            assert not any(b in k.lower() for b in banned), k


# --------------------------------------------------------------------------- #
# The four states
# --------------------------------------------------------------------------- #
def test_available_names_the_branches_that_have_it_and_the_ones_that_do_not(db):
    """Both halves. `not_at` is what makes "not at Deira, but Abu Dhabi has it"
    possible in one turn — and it is the shape the profile-driven flow needs,
    where the preferred branch is asked about first and widened when empty.

    The G700 Flagship in Matte Orange is the plan's own worked example: Al Quoz,
    Abu Dhabi and Ajman have it; Deira and Al Ain do not.
    """
    v = _variant(db, "JET-G700", "Flagship")
    out = stock.availability(db, variant_id=v.id, colour="Matte Orange")
    assert out["status"] == "available"
    assert [s["showroom_id"] for s in out["showrooms"]] == [AL_QUOZ, ABU_DHABI, AJMAN]
    assert [s["showroom_id"] for s in out["not_at"]] == [DEIRA, AL_AIN]
    assert out["colour"] == "Matte Orange"


def test_out_of_stock_at_one_branch_is_not_out_of_stock_everywhere(db):
    """Scoped to Deira the same car comes back unavailable; unscoped it does
    not. Confusing the two is how a booking is lost."""
    v = _variant(db, "JET-G700", "Flagship")
    assert stock.availability(
        db, variant_id=v.id, colour="Matte Orange", showroom_id=DEIRA
    )["status"] == "out_of_stock"
    assert stock.availability(
        db, variant_id=v.id, colour="Matte Orange", showroom_id=ABU_DHABI
    )["status"] == "available"


def test_a_colour_we_do_not_make_is_distinct_from_one_we_have_run_out_of(db):
    """THE DISTINCTION THAT MATTERS MOST. "Out of stock" invites the customer to
    wait; "we don't make it in that colour" does not. Getting this wrong means
    telling someone to check back next week for a car that will never exist.

    The list of colours we DO offer travels with the refusal, so the turn ends
    with a choice instead of a dead end.
    """
    v = _variant(db, "JET-G700", "Flagship")
    out = stock.availability(db, variant_id=v.id, colour="Lime Green")
    assert out["status"] == "colour_not_offered"
    assert "Matte Orange" in out["available_colours"]
    assert out["showrooms"] == []


def test_coming_soon_is_not_out_of_stock(db):
    """The G700 Top Fire has no stock rows at all, so a naive check reports it
    out of stock — which implies it can be ordered. It cannot."""
    v = _variant(db, "JET-G700", "Top Fire")
    out = stock.availability(db, variant_id=v.id)
    assert out["status"] == "coming_soon"
    assert out["variant"]["coming_soon"] is True
    assert out["showrooms"] == [] and out["not_at"] == []

    assert stock.colours_available(db, variant_id=v.id)["status"] == "coming_soon"


# --------------------------------------------------------------------------- #
# Colour resolution
# --------------------------------------------------------------------------- #
def test_a_customer_saying_black_reaches_night_black(db):
    """Customers say "black"; the G700's colour is "Night Black"."""
    v = _variant(db, "JET-G700", "Comfort")
    out = stock.availability(db, variant_id=v.id, colour="black")
    assert out["colour"] == "Night Black"


def test_an_exact_colour_beats_a_substring(db):
    """The T1 really does have a plain "Black". It must not resolve to something
    else merely because a longer name also contains the word."""
    v = _variant(db, "JET-T1", "Comfort")
    assert stock.availability(db, variant_id=v.id, colour="Black")["colour"] == "Black"


def test_an_ambiguous_colour_is_returned_for_the_customer_to_settle(db):
    """"grey" on a Dashing matches Dark Grey and Light Grey. Picking one would
    answer confidently about a car the customer did not ask for."""
    v = _variant(db, "JET-DASH", "1.6 Luxury")
    out = stock.availability(db, variant_id=v.id, colour="grey")
    assert out["error"] == "ambiguous_colour"
    assert set(out["candidates"]) == {"Dark Grey", "Light Grey"}


def test_colour_matching_is_case_insensitive(db):
    v = _variant(db, "JET-G700", "Comfort")
    for spoken in ("MATTE ORANGE", "matte orange", "Matte Orange"):
        assert stock.availability(db, variant_id=v.id, colour=spoken)["colour"] == (
            "Matte Orange"
        )


# --------------------------------------------------------------------------- #
# Model-level questions
# --------------------------------------------------------------------------- #
def test_a_model_level_question_aggregates_its_trims_and_names_them(db):
    """"Do you have the G700 in black?" is a fair question with three trims
    behind it, and the answer should say which of them."""
    m = _model(db, "JET-G700")
    out = stock.availability(db, model_id=m.id, colour="Night Black")
    assert out["status"] == "available"
    names = {v["variant_name"] for v in out["variants_available"]}
    assert names == {"Comfort", "Luxury", "Flagship"}
    # Single-variant shape is None on a model query — there is no one variant.
    assert out["variant"] is None


def test_a_model_query_does_not_include_the_coming_soon_trim(db):
    """The Top Fire is a G700 variant with no stock. It must not appear as an
    available trim of a car that is otherwise on the ground."""
    m = _model(db, "JET-G700")
    out = stock.availability(db, model_id=m.id)
    assert "Top Fire" not in {v["variant_name"] for v in out["variants_available"]}


# --------------------------------------------------------------------------- #
# Colours available
# --------------------------------------------------------------------------- #
def test_offered_and_available_are_different_lists(db):
    """"We do it in six colours, four are here" beats either half alone — and it
    is the difference between a customer waiting for a colour and discovering it
    does not exist."""
    v = _variant(db, "JET-G700", "Luxury")
    out = stock.colours_available(db, variant_id=v.id, showroom_id=AL_QUOZ)
    assert len(out["offered"]) == 6
    # Matte Orange is 0 at Al Quoz for the Luxury.
    assert "Matte Orange" in out["offered"]
    assert "Matte Orange" not in out["available"]
    assert set(out["available"]) < set(out["offered"])
    assert out["showroom"]["showroom_id"] == AL_QUOZ


def test_colours_available_unscoped_covers_every_branch(db):
    v = _variant(db, "JET-G700", "Luxury")
    out = stock.colours_available(db, variant_id=v.id)
    assert set(out["available"]) == set(out["offered"])
    assert "showroom" not in out


# --------------------------------------------------------------------------- #
# Bad input
# --------------------------------------------------------------------------- #
def test_unknown_ids_are_named_not_guessed(db):
    assert stock.availability(db, variant_id=9999)["error"] == "variant_not_found"
    assert stock.availability(db, model_id=9999)["error"] == "vehicle_not_found"
    assert stock.availability(db)["error"] == "missing_query"


def test_offered_is_the_palette_not_the_warehouse(db):
    """A colour Elite makes with no stock rows anywhere used to vanish from
    `offered` entirely — "offered" had quietly meant "has ever had a stock
    row". The model palette is the existence claim; stock stays the
    availability claim."""
    m = _model(db, "JET-G700")
    m.exterior_colours = list(m.exterior_colours or []) + ["Test Lilac"]
    db.flush()
    v = _variant(db, "JET-G700", "Luxury")
    out = stock.colours_available(db, variant_id=v.id)
    assert "Test Lilac" in out["offered"]
    assert "Test Lilac" not in out["available"]


def test_the_get_colours_tool_answers_the_open_colour_question(db):
    """The service existed, tested and orphaned, since T0.8 — no tool wrapped
    it, so "what colours does the X50 come in?" dead-ended live (2026-09-01)."""
    from app.tools.colour_tools import get_colours

    m = _model(db, "JET-X50")
    out = get_colours(db, "s", model_id=m.id)
    assert out["offered"] and set(out["available"]) <= set(out["offered"])
    assert get_colours(db, "s")["error"] == "missing_vehicle"
    assert get_colours(db, "s", model_id="x")["error"] == "invalid_model_id"
