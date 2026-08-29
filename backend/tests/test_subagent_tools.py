"""Every tool a card declares must actually resolve (Phase 5 wiring).

THE DEFECT THIS EXISTS FOR. `subagent._schemas_for` used to skip a declared tool
that was not in the registry, silently and by design — the comment said "a card
may name a tool that is written but not yet registered". The result was that the
FINANCE agent bound zero of its three tools and ran with an empty `tools` list.
It could call nothing, so it returned no findings, so the orchestrator recorded
it as a step that produced nothing — which is indistinguishable from a step
blocked by dependency ordering. Nothing raised, nothing logged above debug, and
the live smoke run read as a pass.

A card is a CONTRACT, not a wish list. These assertions are the build-time half
of that; the log.error in _schemas_for is the runtime half.
"""

from __future__ import annotations

import pytest

from app.agents import loader
from app.orchestrator import subagent
from app.tools import (
    AISHA_ONLY,
    COMMERCIAL_WRITE_TOOLS,
    REGISTRY,
    SUBAGENT_ONLY,
    SUBAGENT_REGISTRY,
    TOOL_SCHEMAS,
    WRITE_TOOLS,
    aisha_schemas,
)

SUBAGENTS = ("sales", "testdrive", "finance")
ALL_CARDS = ("aisha", *SUBAGENTS)


def _declared(agent: str) -> list[str]:
    return list(loader.frontmatter(agent).get("tools") or [])


def _resolvable(agent: str) -> set[str]:
    """Which registry answers for this agent.

    Aisha and the specialists have different surfaces on purpose — she holds
    emit_briefs and none of their reads, they hold the reads and no write — so
    "does this tool exist" is a different question per card, not one global set.
    """
    if agent == "aisha":
        return set(REGISTRY) | set(AISHA_ONLY)
    return set(SUBAGENT_REGISTRY)


@pytest.mark.parametrize("agent", ALL_CARDS)
def test_every_declared_tool_exists(agent):
    """No card may name a tool that does not exist. The finance defect."""
    missing = [t for t in _declared(agent) if t not in _resolvable(agent)]
    assert missing == [], (
        f"{agent}'s card declares {missing}, which no registry provides. "
        "Register the tool or remove it from the card — a declared tool that "
        "cannot resolve is dropped silently at bind time."
    )


@pytest.mark.parametrize("agent", SUBAGENTS)
def test_every_declared_tool_actually_binds(agent):
    """Declared count == bound count. The assertion that would have caught it."""
    declared = _declared(agent)
    bound = [s["name"] for s in subagent._schemas_for(agent)]
    assert bound == declared, (
        f"{agent} declares {len(declared)} tools but binds {len(bound)}: "
        f"{sorted(set(declared) - set(bound))} did not resolve."
    )


@pytest.mark.parametrize("agent", SUBAGENTS)
def test_no_subagent_binds_a_write(agent):
    """The property that makes running sub-agents on a cheap model safe."""
    bound = {s["name"] for s in subagent._schemas_for(agent)}
    assert bound & WRITE_TOOLS == set()


@pytest.mark.parametrize("agent", SUBAGENTS)
def test_every_subagent_has_at_least_one_tool(agent):
    """A sub-agent with no tools cannot do anything, and says so as 'blocked'."""
    assert subagent._schemas_for(agent), f"{agent} binds no tools at all"


def test_aisha_holds_no_commercial_write():
    """Doc §2: holding inventory and booking belong to the orchestrator alone."""
    assert set(_declared("aisha")) & COMMERCIAL_WRITE_TOOLS == set()
    assert {s["name"] for s in aisha_schemas()} & COMMERCIAL_WRITE_TOOLS == set()


def test_aisha_binds_every_tool_her_card_declares():
    """Same contract as the sub-agents, against her own binding."""
    assert [s["name"] for s in aisha_schemas()] == _declared("aisha")


def test_aisha_holds_emit_briefs_and_no_specialist_read():
    """She delegates rather than looking things up herself.

    If she kept a catalogue read she would answer from it instead of briefing,
    and the fan-out would be dead code that still cost a generation.
    """
    bound = {s["name"] for s in aisha_schemas()}
    assert "emit_briefs" in bound
    for agent in SUBAGENTS:
        specialist_reads = set(_declared(agent)) - {"search_knowledge"}
        assert bound & specialist_reads == set(), (
            f"Aisha holds {bound & specialist_reads}, which is {agent}'s work"
        )


def test_emit_briefs_has_a_schema_and_no_function():
    """It is answered by the orchestrator node, so there is nothing to call."""
    assert "schema" in AISHA_ONLY["emit_briefs"]
    assert "fn" not in AISHA_ONLY["emit_briefs"]
    assert "emit_briefs" not in REGISTRY
    assert "emit_briefs" not in SUBAGENT_REGISTRY


def test_single_agent_tool_surface_is_unchanged():
    """TOOL_SCHEMAS must not grow when a sub-agent tool is added.

    graph.py:1143 hands TOOL_SCHEMAS to the unrouted single agent, and
    _system_blocks depends on that list being byte-stable for the ~14.8k cached
    prefix to hit. A cache miss is not reported by the API — it is just billed —
    so this is the only place the regression can be caught.
    """
    assert len(TOOL_SCHEMAS) == len(REGISTRY) == 22
    assert {s["name"] for s in TOOL_SCHEMAS} == set(REGISTRY)
    assert set(SUBAGENT_ONLY) & set(REGISTRY) == set()


def test_subagent_only_tools_are_all_reads():
    """Nothing added for sub-agents may be a write, under any name."""
    assert set(SUBAGENT_ONLY) & WRITE_TOOLS == set()


def test_subagent_registry_extends_rather_than_replaces():
    """Every single-agent tool stays reachable, with the same implementation."""
    for name, entry in REGISTRY.items():
        assert SUBAGENT_REGISTRY[name] is entry
