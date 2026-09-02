"""select.entity_owner: a brief that names an entity goes to the card that
produces it, before the judge gets a say (2026-09-02).

Seen live: "What is the vehicle_id for the Jetour Dashing? I need it to look
up slots" — the Haiku judge answered "none", twice, and the booking ended in
an apology. The cards already declare who produces what.
"""

from __future__ import annotations

from app.orchestrator import select


def test_an_entity_lookup_goes_to_its_producer():
    r = select.entity_owner("What is the vehicle_id for the Jetour Dashing? I need it to look up slots")
    assert r is not None and r.agent == "sales" and "vehicle_id" in r.reason
    assert select.entity_owner("hold a slot_id for tomorrow and give me the booking_ref").agent == "testdrive"
    assert select.entity_owner("what is the on_road_aed for the T2 Luxury Plus").agent == "finance"


def test_a_brief_without_an_entity_name_is_left_to_the_judge():
    assert select.entity_owner("what documents are required for registration") is None
    assert select.entity_owner("") is None


def test_an_entity_nobody_produces_changes_nothing():
    assert select.entity_owner("what is the showroom_id for Jetour Deira") is None


def test_an_entity_two_cards_produce_is_ambiguous(monkeypatch):
    from app.orchestrator import order

    monkeypatch.setattr(order, "produces", lambda agent: {"vehicle_id"} if agent in ("sales", "finance") else set())
    assert select.entity_owner("give me the vehicle_id") is None


def test_select_uses_the_owner_before_the_judge():
    def judge(brief, agents):  # noqa: ARG001
        raise AssertionError("the judge was asked about an owned entity")

    r = select.select("What is the vehicle_id for the Jetour Dashing?", judge=judge)
    assert r.agent == "sales" and r.layer == "fast_lane"
