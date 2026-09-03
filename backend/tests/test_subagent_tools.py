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

from tests.conftest import wire_text

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


# THE TWO LOOKUPS AISHA KEEPS, and the line is "does this need judgement"
# (measured 2026-08-31). Across every live run that day, 57% of specialist round
# trips wrapped a SINGLE tool call at a median of 3,704ms, with a ~1.3s routing
# judge in front — to run a query that takes 0.3ms. Choosing a car does need
# judgement: search_vehicles was called ten times in a day, three of them inside
# one Sales brief, turning "a 7-seater for my family" into a ranked shortlist.
# Asking which times are free at a branch does not.
#
# nearest_showroom JOINED THEM 2026-09-03, on the same line and for a second
# reason. Turning "I'm near Dubai Mall" into the closest branch is a geocode and
# a distance sort — nothing to decide. And leaving it to TestDrive alone meant it
# often did not run at all: measured live that day, within one session, "im near
# dubai mall" routed to TestDrive and returned distance_km and drive_minutes,
# while "i am at dubai mall right now" routed to Sales — which does not hold it,
# location not being its domain — and the customer was told "I don't have exact
# driving distances" beside a guessed branch.
#
# NAMED HERE RATHER THAN LOOSENED AWAY, so the guard below still fails the day
# somebody hands her search_vehicles or a finance read.
AISHA_KEEPS = {"get_available_slots", "get_booking", "nearest_showroom"}


def test_aisha_can_place_a_customer_without_a_specialist():
    """The bug this closes: whether a customer got a distance and a Maps link
    depended on which specialist the brief happened to route to.

    Pinned on the BINDING rather than on the card text, because the binding is
    what the model actually receives — and the tool's own description, which
    tells her to call it the moment they say where they are and not to paste the
    link because the card carries it, is only reachable once it is bound.
    """
    assert "nearest_showroom" in {s["name"] for s in aisha_schemas()}


def test_aisha_holds_emit_briefs_and_no_specialist_read():
    """She delegates the JUDGEMENT rather than looking things up herself.

    If she kept a catalogue read she would answer from it instead of briefing,
    and the fan-out would be dead code that still cost a generation. The two
    exceptions above are lookups with nothing to decide — see AISHA_KEEPS.
    """
    bound = {s["name"] for s in aisha_schemas()}
    assert "emit_briefs" in bound
    for agent in SUBAGENTS:
        specialist_reads = set(_declared(agent)) - {"search_knowledge"} - AISHA_KEEPS
        assert bound & specialist_reads == set(), (
            f"Aisha holds {bound & specialist_reads}, which is {agent}'s work"
        )


def test_the_catalogue_stays_with_sales():
    """The exception is two named lookups, not a door. Choosing a car is the
    judgement the fan-out exists for."""
    bound = {s["name"] for s in aisha_schemas()}
    for name in ("search_vehicles", "compare_vehicles", "get_vehicle_details"):
        assert name not in bound, f"Aisha must not hold {name}"


def test_aisha_still_holds_no_commercial_write():
    """Holding a slot and making, moving or cancelling a booking remain the
    orchestrator's, whatever reads she is given.

    COMMERCIAL_WRITE_TOOLS, not WRITE_TOOLS: the split is by KIND, not by layer.
    A write that is an act of dialogue — a consent, a licence acknowledgement, a
    remembered preference, goodbye — belongs to whoever is talking, and only
    Aisha talks. See orchestrator/write.py."""
    from app.tools import COMMERCIAL_WRITE_TOOLS

    bound = {s["name"] for s in aisha_schemas()}
    assert not (bound & COMMERCIAL_WRITE_TOOLS), sorted(bound & COMMERCIAL_WRITE_TOOLS)


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
        system = wire_text(sent["system"])
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
    assert tomorrow.isoformat() in wire_text(sent["system"])


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


def test_finances_callback_ask_travels_as_a_note_not_an_action():
    """OPEN-ISSUES #7. The card forbids proposals (`proposes: []`) yet its prose
    said "propose a human callback" — an instruction the write path would refuse
    if the model ever obeyed it. The ask now travels as DATA: the note flags the
    moment, and Aisha — who holds request_human_callback and rule 13's prose —
    arranges it."""
    stop = loader.sections("finance")["WHERE_YOU_STOP"].replace("\\\n", "")
    assert "propose a human callback" not in stop
    assert "SAY IN YOUR NOTE" in stop
    assert "needs a human callback" in stop


def test_finance_is_told_it_cannot_arrange_the_callback():
    """The other half of the same defect: prose about a callback is not a
    callback. The card says plainly that the sub-agent cannot arrange one, so
    its note never reads back to Aisha as a confirmation."""
    stop = loader.sections("finance")["WHERE_YOU_STOP"].replace("\\\n", "")
    assert "YOU CANNOT ARRANGE THAT CALLBACK" in stop
    assert "Aisha" in stop


# --------------------------------------------------------------------------- #
# The DELEGATING section — multi-agent only
# --------------------------------------------------------------------------- #
def test_delegating_never_reaches_the_live_single_agent_prompt():
    """It is absent from SINGLE_AGENT_ORDER on purpose. If it ever leaks in, the
    live prompt changes, the S5 fixture breaks, and the ~14.8k cached prefix
    takes a cold write for every customer."""
    from app.agents import loader

    assert "HOW THE WORK GETS DONE NOW" not in loader.single_agent_body()


def test_the_fan_out_aisha_is_told_she_no_longer_books():
    """Her ported prose says 'call lock_slot' and 'call create_booking'. Without
    this section she did exactly that — and until the binding was enforced at
    dispatch, it ran."""
    from app.agents import loader

    body = loader.body_for("aisha")
    assert "HOW THE WORK GETS DONE NOW" in body
    for tool in ("lock_slot", "create_booking", "reschedule_booking", "cancel_booking"):
        assert tool in body
    assert "You no longer call" in body


def test_she_is_told_only_executed_actions_are_real():
    """The difference between a confirmation and a lie."""
    from app.agents import loader

    body = loader.body_for("aisha")
    assert "'executed'" in body or "executed" in body
    assert "asked for" in body.lower()


def test_the_ported_booking_sequence_is_untouched():
    """Rule zero: port, don't re-author. DELEGATING says who performs the steps;
    it must not have edited the steps themselves."""
    from app.agents import loader

    sections = loader.sections("aisha")
    seq = sections["BOOKING_SEQUENCE"]
    assert "A booking becomes real only when you call create_booking" in seq
    assert "rule 9" in seq


# --------------------------------------------------------------------------- #
# An unwrapped reply is still findings (2026-08-29)
#
# Live: a Sales step wrote 1158 chars of valid JSON with the results at the TOP
# level and no `findings` wrapper. Every byte was discarded — six seconds of
# generation for two ids that _harvest_entities read out of the tool results
# anyway. Second sighting of the same shape; the first is in that docstring.
# --------------------------------------------------------------------------- #
def test_findings_at_the_top_level_are_read_not_discarded():
    from app.orchestrator import subagent as sub

    out = sub._parse_findings('{"vehicle_id": 8, "price": "AED 79,000"}')
    assert out == {"vehicle_id": 8, "price": "AED 79,000"}


def test_the_wrapper_still_wins_when_it_is_there():
    from app.orchestrator import subagent as sub

    out = sub._parse_findings(
        '{"findings": {"vehicle_id": 8}, "note": "found one", "extra": "ignored"}'
    )
    assert out == {"vehicle_id": 8, "_note": "found one"}


def test_an_unwrapped_reply_cannot_smuggle_a_proposal():
    """The reserved keys stay reserved. Otherwise omitting the wrapper would be
    a way to put `proposals` into findings unvalidated — and proposals are the
    one thing that moves inventory."""
    from app.orchestrator import subagent as sub

    out = sub._parse_findings(
        '{"vehicle_id": 8, "proposals": [{"action": "create_booking"}]}'
    )
    assert out["vehicle_id"] == 8
    # It is lifted to the reserved key by the normal path, never left inline.
    assert "proposals" not in out
    assert out.get("_proposals") == [{"action": "create_booking"}]


def test_a_reply_that_is_not_json_yields_no_findings_but_is_not_thrown_away():
    """Tolerant, but never inventing — nothing becomes a finding or an entity.

    It is not DISCARDED either, and that half is newer: 14 of 47 live steps on
    2026-08-31 answered in prose, every byte was binned, and the step came back
    `ok` with nothing in it. Aisha re-briefed until the cascade ceiling."""
    from app.orchestrator import subagent as sub

    out = sub._parse_findings("I looked but found nothing useful")
    assert [k for k in out if not k.startswith("_")] == []
    assert out["_note"] == "I looked but found nothing useful"


# --------------------------------------------------------------------------- #
# The visit details, and the three places they can fall out (2026-08-31)
# --------------------------------------------------------------------------- #
def test_the_specialist_is_told_the_brief_is_its_view_of_the_conversation():
    """FOUND LIVE. A customer said "my wife and two kids will come along, we'll
    need a child seat", Aisha promised one, and the booking was written with
    companions empty and child_seat false — the showroom would not have known.

    The specialist was RIGHT to omit them by the letter of the schema: the
    fields say "if they said", and it cannot see what anyone said. The card
    told it to have a hospitality exchange it has no way to have."""
    from app.agents import loader

    card = loader.body_for("testdrive")
    assert "THE BRIEF IS THE ONLY PART OF THAT CONVERSATION YOU CAN SEE" in card
    assert "companions" in card and "child_seat" in card
    # It lives in its OWN section, not edited into the ported prose — rule zero.
    assert "THE_VISIT_DETAILS_ARE_IN_THE_BRIEF" in loader.sections("testdrive")
    assert "MAKING_THE_VISIT_THEIRS" in loader.sections("testdrive")


def test_create_booking_tells_a_briefed_caller_what_told_you_means():
    from app.tools.booking_tools import CREATE_BOOKING_SCHEMA

    text = CREATE_BOOKING_SCHEMA["description"]
    assert "THE BRIEF IS" in text
    # And the guard against the opposite failure survives: no guessing.
    assert "Silence in the brief is still silence" in text
    assert "do NOT fill companions or child_seat with a guess" in text


def test_emit_briefs_tells_aisha_to_write_the_visit_details_down():
    """She is the only one who hears them, and the brief is the only channel."""
    from app.tools.brief_tools import EMIT_BRIEFS_SCHEMA

    text = EMIT_BRIEFS_SCHEMA["description"]
    assert "VISIT DETAILS" in text
    assert "child seat" in text


def test_the_booking_tool_still_accepts_what_the_card_now_promises():
    """The prose is worthless if the field is not really there."""
    from app.tools.booking_tools import CREATE_BOOKING_SCHEMA

    props = CREATE_BOOKING_SCHEMA["input_schema"]["properties"]
    assert props["companions"]["type"] == "string"
    assert props["child_seat"]["type"] == "boolean"


def test_a_proposal_carries_the_comfort_fields_through_to_the_write(db):
    """The pipeline never dropped them — write.execute passes args whole. This
    pins that, so a future filter cannot silently reintroduce the bug."""
    from app.orchestrator import subagent as sub

    kept = sub._validate_proposals("testdrive", [{
        "action": "create_booking",
        "args": {"vehicle_id": 7, "slot_id": 1401,
                 "companions": "wife and two kids", "child_seat": True},
    }])
    assert kept[0]["args"]["companions"] == "wife and two kids"
    assert kept[0]["args"]["child_seat"] is True
