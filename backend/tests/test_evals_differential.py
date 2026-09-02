"""CI gate for the prompt-split/tool-binding differential (see evals/differential.py).

BODY ORPHANS MUST BE ZERO. A block in an agent's prompt naming a tool that agent
cannot call is a defect with no runtime symptom except an agent that keeps trying
to do something impossible.

ANCHOR ORPHANS ARE NOW ZERO TOO. S5 split the prompt body but left the anchors
reaching every agent, which produced four orphans. S7a scoped them: state anchors
(date, facts, bookings, language, voice) stay shared, instruction anchors go to
the journey owning the tools they name — _SECOND_DRIVE_ANCHOR and the compare
paragraph to TestDrive. The allowlist below is deliberately empty and should stay
that way; an anchor that starts issuing orders to an agent that cannot obey them
fails here.
"""

from __future__ import annotations

from evals import differential

# Cleared by S7a. Empty is the correct state — see the module docstring.
KNOWN_ANCHOR_ORPHANS: set[tuple[str, str, str]] = set()


def test_no_agent_is_told_to_call_a_tool_it_cannot_call():
    reports = differential.check_all()
    offenders = {
        (agent, module, const, tool)
        for agent, r in reports.items()
        for module, const, tool in r.orphaned_rules
    }
    assert not offenders, differential.render(reports)


def test_anchor_orphans_match_the_tracked_inventory():
    reports = differential.check_all()
    found = {
        (agent, anchor, tool)
        for agent, r in reports.items()
        for anchor, tool in r.orphaned_anchors
    }
    assert found == KNOWN_ANCHOR_ORPHANS, differential.render(reports)


def test_every_registry_tool_is_reachable_by_some_agent():
    from app.agent import tool_binding

    assert tool_binding.unassigned() == set()
    assert tool_binding.unknown() == set()


def test_tool_schemas_are_a_strict_subset_per_agent():
    from app.agent import tool_binding
    from app.tools import REGISTRY

    for agent in tool_binding.AGENTS:
        schemas = tool_binding.schemas_for(agent)
        names = {s["name"] for s in schemas}
        assert names == tool_binding.tools_for(agent)
        assert names < set(REGISTRY), f"{agent} must not hold the whole registry"


# --------------------------------------------------------------------------- #
# The sub-agent cards (2026-09-01)
# --------------------------------------------------------------------------- #
# check_all() covered only the legacy journeys over REGISTRY, so the Phase-5
# surface was invisible to the one check built for it: get_accessories sat on
# the sales card with no prose naming it — the alloy-wheel bug, live, verbatim
# the defect this file's module docstring opens with — and search_knowledge
# joined the testdrive card the same way. These tests close that hole.
def test_no_card_tells_its_agent_to_call_a_tool_it_cannot_call():
    """Orphans outside the tracked inventory are defects. The inventory itself
    lives in differential.KNOWN_CARD_ORPHANS — shared with the boot gate —
    and is already subtracted inside check_card, so anything surfacing here
    is new."""
    reports = differential.check_cards()
    offenders = {
        (agent, section, tool)
        for agent, r in reports.items()
        for agent_, section, tool in r.orphaned_rules
    }
    assert not offenders, differential.render(reports)


def test_every_card_tool_is_named_by_the_card_s_own_prose():
    """THE CLASS THAT SHIPPED. A tool bound to a card that no prose mentions is
    a capability the model is never told it has: the accessory table was
    seeded, bound, and never once called while customers were told we did not
    hold it. Unlike the legacy journeys — where unguided is reported, not
    failed — a card's tool list is short and curated, so unguided there is a
    hard failure."""
    reports = differential.check_cards()
    unguided = {
        (agent, tool)
        for agent, r in reports.items()
        for tool in r.unguided_tools
    }
    assert not unguided, differential.render(reports)


def test_the_gate_sees_card_orphans_too():
    """gate.check_tool_reachability read a field that never existed
    (`rep.orphaned`) and so returned [] forever, and it never consulted the
    cards at all. It now consults both surfaces; with a healthy tree it is
    empty, and the judge of health is the same code the tests above use."""
    from app.agents import gate

    assert gate.check_tool_reachability() == []
