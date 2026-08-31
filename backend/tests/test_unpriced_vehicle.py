"""An unpriced car must never be cheapest, dearest, or inside a budget (T0.7).

WHY THIS FILE EXISTS, IN THE WORDS OF THE CODE IT PROTECTS. models.py used to
carry a note forbidding exactly the change T0.7 makes:

    "If a future model genuinely has no published price, this is a real decision
     to reopen and not a column to quietly re-null: restoring the column alone
     would let an unpriced car sort as free in lowest_price and vanish from every
     budget query."

The column has now been re-nulled, on purpose, because the G700 Top Fire exists
and is coming soon. These tests are the other half of that decision — the part
that makes it safe rather than merely intended. Delete them and the warning comes
true silently: an unpriced car sorts as free, wins "what's your cheapest car",
and crashes every budget query on `None > int`.

Offline: plain objects, no database, no vendor.
"""

from __future__ import annotations

import pytest

from app.services import catalogue


class _Row:
    """The attributes _ranked_for and _apply_filters actually touch."""

    def __init__(self, id, name, price_min, price_max, *, seats=5, fuel="petrol"):
        self.id = id
        self.name = name
        self.price_aed_min = price_min
        self.price_aed_max = price_max
        self.seats_min = self.seats_max = seats
        self.body_type = "SUV"
        self.brand = "Jetour"
        self.fuel_type = type("F", (), {"value": fuel})()


CHEAP = _Row(1, "X50", 69_000, 79_000)
DEAR = _Row(2, "G700", 169_000, 240_450, seats=6, fuel="phev")
# The real case: G700 Top Fire, coming soon, no price at all.
UNPRICED = _Row(3, "G700 Top Fire", None, None, seats=6, fuel="phev")

ALL = [CHEAP, DEAR, UNPRICED]
NO_FILTERS = dict(body_type=None, min_seats=None, fuel=None, brand=None)


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def test_an_unpriced_car_is_not_the_cheapest_car():
    """The exact failure the old note named: sorting as free."""
    ranked = catalogue._ranked_for(ALL, "lowest_price")
    assert UNPRICED not in ranked
    assert ranked[0] is CHEAP, "an unpriced car outranked a real cheapest"


def test_an_unpriced_car_is_not_the_most_expensive_car():
    """Symmetrical: we cannot call it top-of-range with no ceiling either."""
    ranked = catalogue._ranked_for(ALL, "flagship")
    assert UNPRICED not in ranked
    assert ranked[0] is DEAR


def test_an_unpriced_phev_cannot_take_the_efficiency_basis():
    """`efficiency` picks the CHEAPEST phev, so it needs the same guard.

    The Top Fire is a PHEV, so it passes the fuel filter and would have been
    picked on a null price — the one basis where the guard is easy to forget
    because the obvious filter is fuel, not price.
    """
    ranked = catalogue._ranked_for(ALL, "efficiency")
    assert UNPRICED not in ranked
    assert ranked == [DEAR]


def test_an_unpriced_car_still_ranks_where_price_is_irrelevant():
    """The guards are per-basis, not a blanket exclusion.

    A coming-soon car has seats, so it belongs in a seats answer. Excluding it
    everywhere would be the opposite mistake — a car Elite is about to sell,
    invisible in the one place it can honestly be mentioned.
    """
    ranked = catalogue._ranked_for(ALL, "most_seats")
    assert UNPRICED in ranked


# --------------------------------------------------------------------------- #
# budget filtering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("budget", [50_000, 100_000, 300_000, 10_000_000])
def test_an_unpriced_car_never_satisfies_a_budget(budget):
    """"Vanish from every budget query" — at any budget, however generous.

    Not a judgement that it is expensive. We cannot promise a car fits a budget
    when we do not know what it costs.
    """
    kept = catalogue._apply_filters(ALL, max_price_aed=budget, **NO_FILTERS)
    assert UNPRICED not in kept


def test_a_budget_query_does_not_crash_on_an_unpriced_car():
    """This one is a CRASH, not a wrong answer.

    `None > 100000` raises TypeError, so before the guard a single coming-soon
    row would take down every budget query in the system the moment it was
    seeded — not degrade it, end it.
    """
    catalogue._apply_filters(ALL, max_price_aed=100_000, **NO_FILTERS)


def test_an_unpriced_car_is_visible_when_no_budget_is_named():
    """It is coming soon, not hidden. With no budget it must still appear."""
    kept = catalogue._apply_filters(ALL, max_price_aed=None, **NO_FILTERS)
    assert UNPRICED in kept


# --------------------------------------------------------------------------- #
# the column itself
# --------------------------------------------------------------------------- #
def test_the_price_columns_are_actually_nullable():
    """Guards protect nothing if the column silently goes NOT NULL again."""
    import app.models  # noqa: F401
    from app.db import Base

    cols = Base.metadata.tables["vehicle_models"].c
    assert cols["price_aed_min"].nullable
    assert cols["price_aed_max"].nullable
