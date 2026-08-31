"""The baseline's fixture DB must serve every code path it measures.

THE DEFECT THIS EXISTS FOR (2026-08-29). evals/baseline._seed_db seeded models
and showrooms only — it was ported from a script written before Phase 0 made the
catalogue variant-level. The single agent never noticed, because its tools are
model-level. The sub-agents were starved: get_variants returned [],
get_on_road_price and get_finance_estimate returned variant_not_found,
check_stock found nothing.

So the multi-agent baseline measured a database missing three tables and read as
a broken agent. Aisha behaved impeccably against it — retried, apologised,
escalated to a human — which is exactly why nobody would suspect the fixture.

A MEASUREMENT FIXTURE NEEDS THE SAME GUARD AS PRODUCTION CODE, because a
silently incomplete one does not fail: it produces a plausible number.
"""

from __future__ import annotations

import pytest

from app.models import FinanceBank, Showroom, Stock, VehicleModel, VehicleVariant
from app.tools import SUBAGENT_ONLY


@pytest.fixture(scope="module")
def seeded():
    from evals.baseline import _seed_db

    return _seed_db()


@pytest.mark.parametrize(
    "model", [Showroom, VehicleModel, VehicleVariant, Stock, FinanceBank]
)
def test_every_catalogue_table_is_populated(seeded, model):
    assert seeded.query(model).count() > 0, (
        f"{model.__name__} is empty — every tool that reads it will return "
        "nothing, and the run will measure the fixture rather than the agent."
    )


def test_every_subagent_only_tool_returns_data(seeded):
    """The five reads added for the sub-agents, each against the fixture.

    Asserted through the tools rather than the tables: a row count proves the
    seed ran, not that the tool can reach it.
    """
    variant = seeded.query(VehicleVariant).filter(
        VehicleVariant.price_aed.isnot(None)
    ).first()
    model = seeded.query(VehicleModel).first()
    assert variant is not None and model is not None

    calls = {
        "get_variants": {"vehicle_id": model.id},
        "check_stock": {"variant_id": variant.id},
        "get_on_road_price": {"variant_id": variant.id},
        "get_finance_estimate": {"variant_id": variant.id},
    }
    for name, kwargs in calls.items():
        result = SUBAGENT_ONLY[name]["fn"](seeded, "fixture-check", **kwargs)
        assert not result.get("error"), f"{name} -> {result.get('error')}"


def test_the_fixture_agrees_with_the_test_suites_own_seed(seeded):
    """Both read the same JSON; a divergence means one of them was edited."""
    import json
    from pathlib import Path

    seed_dir = Path(__file__).resolve().parent.parent / "seed"
    models = json.loads((seed_dir / "vehicle_models.json").read_text())
    expected_variants = sum(len(m["variants"]) for m in models)
    assert seeded.query(VehicleVariant).count() == expected_variants


def test_slots_exist_so_a_booking_can_be_reached(seeded):
    """booking_full cannot reach create_booking without inventory to hold."""
    from app.models import Slot

    assert seeded.query(Slot).count() > 0
