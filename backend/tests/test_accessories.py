"""The accessory catalogue, and the wiring it went without (2026-08-31).

The table has been seeded since T0.7 and NOTHING READ IT — no tool, no service,
no endpoint. A customer who asked the price of the alloy wheel upgrade was told
we did not hold it, while the row sat in the database with 4500 in it. These
tests are about the wiring, not the data.

Against the REAL seed fixtures, like the rest of the suite: if seed/accessories
drifts from Elite's sheet, this notices.
"""

from __future__ import annotations

from app.services import accessories
from app.tools.accessory_tools import get_accessories


def test_the_alloy_wheel_upgrade_can_actually_be_reached(db):
    """The question that found the gap, live."""
    out = get_accessories(db, "s1")
    by_name = {a["name"]: a for a in out["accessories"]}
    alloy = by_name["Alloy Wheel Upgrade"]
    assert alloy["price_aed"] == 4500
    assert "alloy rims" in alloy["description"]


def test_every_accessory_elite_supplied_is_present_and_priced(db):
    out = accessories.catalogue(db)
    assert out["count"] == 5
    assert all(a["price_aed"] and a["price_aed"] > 0 for a in out["accessories"])
    assert all(a["description"] for a in out["accessories"]), (
        "a price beside a bare name reads as arbitrary — see models.py"
    )


def test_a_description_comes_back_for_every_part(db):
    """Three of the five names mean nothing alone. The agent speaks only from
    tool results, so a missing description means she cannot describe the thing
    at all — and inventing one is the grounding failure the guardrails exist
    to prevent."""
    out = accessories.catalogue(db)
    for a in out["accessories"]:
        assert len(a["description"]) > 20, a


def test_filtering_by_model_keeps_the_universal_accessories(db):
    """model_id is nullable and NULL means "fits anything". A filter that
    dropped those would hide most of Elite's range the moment a customer
    mentioned a car."""
    everything = accessories.catalogue(db)["count"]
    narrowed = accessories.catalogue(db, model_id=1)
    assert narrowed["count"] == everything
    assert narrowed["filtered_to_model_id"] == 1


def test_the_price_is_the_part_not_the_fitting(db):
    """Elite's sheet says nothing about fitting, so the tool must not let the
    agent imply it is included."""
    assert accessories.catalogue(db)["prices_exclude_fitting"] is True


def test_a_junk_model_id_is_data_not_an_exception(db):
    assert get_accessories(db, "s1", model_id="not-a-number")["error"] == (
        "invalid_model_id"
    )


def test_the_sku_is_carried_for_joining_and_flagged_as_unspeakable(db):
    """promotions.md names accessories by code ("free window tinting
    (ACC-002)"); matching that on the name string is the fragile join the code
    exists to avoid. The schema is what tells the model never to say it."""
    from app.tools.accessory_tools import GET_ACCESSORIES_SCHEMA

    codes = {a["part_code"] for a in accessories.catalogue(db)["accessories"]}
    assert "ACC-002" in codes
    assert "NEVER say it" in GET_ACCESSORIES_SCHEMA["description"]


def test_it_is_a_sub_agent_read_and_never_a_write():
    """A write reaching a sub-agent would undo the single property that makes
    running them on Haiku safe."""
    from app.tools import REGISTRY, SUBAGENT_ONLY, SUBAGENT_TOOL_FNS, WRITE_TOOLS

    assert "get_accessories" in SUBAGENT_ONLY
    assert "get_accessories" in SUBAGENT_TOOL_FNS
    assert "get_accessories" not in WRITE_TOOLS
    # Absent from REGISTRY on purpose: TOOL_SCHEMAS must stay byte-stable while
    # the single-agent path is the live one.
    assert "get_accessories" not in REGISTRY


def test_the_sales_card_declares_the_tool_its_domain_already_claimed():
    """The card has claimed accessories since it was written, with no tool
    behind the claim."""
    from app.agents import loader

    card = loader.frontmatter("sales")
    assert "get_accessories" in card["tools"]
    assert "accessories" in card["domain"].lower()


def test_an_empty_catalogue_is_an_answer_not_an_error(db):
    from app.models import Accessory

    db.query(Accessory).delete()
    db.commit()
    out = accessories.catalogue(db)
    assert out == {
        "accessories": [], "count": 0,
        "filtered_to_model_id": None, "prices_exclude_fitting": True,
    }
