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

from types import SimpleNamespace

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


# --------------------------------------------------------------------------- #
# The date anchor (found live, 2026-08-29)
# --------------------------------------------------------------------------- #
def test_every_subagent_prompt_carries_todays_date(monkeypatch):
    """A sub-agent with no calendar answers date questions confidently wrong.

    THE LIVE FAILURE. Asked for "slots at Deira on Saturday", TestDrive resolved
    Saturday to 11 Jan 2025 — nineteen months stale — looked that date up, and
    reported "no slots available". The date it used appears nowhere in the
    findings, so the wrong answer is indistinguishable from a right one.

    The single agent has always been immune (prompt.py appends this anchor every
    turn); the sub-agents were split off that path and inherited none of it.

    ASSERTED THROUGH run_step, not by rebuilding the prompt here — a test that
    composes its own copy of the system prompt passes whether or not the code
    under test does the same thing.
    """
    from datetime import date

    from app.orchestrator import order, subagent as sub

    sent = {}

    class _Stub:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text='{"findings": {}}')],
                    usage=None,
                )

    monkeypatch.setattr(sub, "_client", lambda: _Stub)

    for agent in SUBAGENTS:
        sent.clear()
        sub.run_step(
            order.Step(brief="when are you open on Saturday", agent=agent),
            {}, db=None, session_id="t", timeout=5.0, max_iterations=1,
        )
        system = sent["system"]
        today = date.today()
        assert str(today.year) in system, f"{agent} has no idea what year it is"
        assert today.isoformat() in system, f"{agent} was not given today's date"
        assert "never do date arithmetic yourself" in system


def test_the_anchor_is_todays_date_not_a_frozen_one(monkeypatch):
    """The anchor is computed per call, so a long-running process cannot serve
    yesterday's calendar to every turn after midnight."""
    from datetime import date, timedelta

    from app.agent import prompt as _prompt
    from app.orchestrator import order, subagent as sub

    tomorrow = date.today() + timedelta(days=1)
    sent = {}

    class _Stub:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text='{"findings": {}}')],
                    usage=None,
                )

    monkeypatch.setattr(sub, "_client", lambda: _Stub)
    monkeypatch.setattr(
        sub, "_DATE_ANCHOR", _prompt._DATE_ANCHOR, raising=False
    )

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(sub, "date", _FakeDate)
    sub.run_step(
        order.Step(brief="x", agent="testdrive"),
        {}, db=None, session_id="t", timeout=5.0, max_iterations=1,
    )
    assert tomorrow.isoformat() in sent["system"]


# --------------------------------------------------------------------------- #
# Proposals — the card is the allowlist (T4.2)
# --------------------------------------------------------------------------- #
def test_only_testdrive_may_propose_commercial_actions():
    """The vocabulary is not offered to an agent that cannot use it: a model
    shown a booking verb it may not use will eventually reach for it, and every
    attempt is a refusal the customer waits through."""
    from app.orchestrator import subagent as sub

    assert set(sub._proposes("testdrive")) == {
        "hold_slot", "create_booking", "reschedule_booking", "cancel_booking",
    }
    assert sub._proposals_clause("sales") == ""
    assert sub._proposals_clause("finance") == ""
    assert "hold_slot" in sub._proposals_clause("testdrive")


def test_an_invented_action_is_dropped_before_write_sees_it():
    from app.orchestrator import subagent as sub

    kept = sub._validate_proposals("testdrive", [
        {"action": "create_booking", "args": {"slot_id": 5}},
        {"action": "refund_deposit", "args": {}},        # invented
        {"action": "send_brochure", "args": {}},         # sales', not testdrive's
    ])
    assert [p["action"] for p in kept] == ["create_booking"]
    assert kept[0]["agent"] == "testdrive"


def test_a_sales_proposal_of_a_booking_is_dropped():
    """Borrowing another agent's vocabulary is the realistic failure, not
    inventing a verb from nothing."""
    from app.orchestrator import subagent as sub

    assert sub._validate_proposals("sales", [{"action": "create_booking"}]) == []


def test_proposals_are_parsed_off_the_reply():
    from app.orchestrator import subagent as sub

    parsed = sub._parse_findings(
        '{"findings": {"slot_id": 12}, '
        '"proposals": [{"action": "hold_slot", "args": {"slot_id": 12}}], '
        '"note": "asked for the 1pm"}'
    )
    assert parsed["slot_id"] == 12
    assert parsed["_proposals"] == [{"action": "hold_slot", "args": {"slot_id": 12}}]


def test_no_proposals_key_when_none_were_made():
    from app.orchestrator import subagent as sub

    assert "_proposals" not in sub._parse_findings('{"findings": {"a": 1}}')


def test_every_proposed_action_is_something_write_can_perform():
    """A card asking for what write.py cannot execute is a build error."""
    from app.agents import loader
    from app.orchestrator.write import PROPOSABLE

    for agent in ALL_CARDS:
        declared = set(loader.frontmatter(agent).get("proposes") or [])
        unknown = declared - PROPOSABLE - set(REGISTRY)
        assert unknown == set(), f"{agent} proposes {unknown}, which nothing can perform"
