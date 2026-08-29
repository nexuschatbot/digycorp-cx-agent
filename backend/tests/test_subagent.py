"""The Haiku sub-agent runner (Phase 9 binding).

EVERYTHING HERE IS OFFLINE. The Anthropic client is stubbed, so the whole loop —
prompt assembly, tool dispatch, event forwarding, findings parsing, the iteration
ceiling — runs without a network call. The live smoke test is separate and costs
money; these are the tests that must pass before it is worth spending any.
"""

from __future__ import annotations

import json
import types

import pytest

from app.orchestrator import subagent
from app.orchestrator.order import Step


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _text(s):
    return _Block(type="text", text=s)


def _use(name, inp, uid="u1"):
    return _Block(type="tool_use", name=name, input=inp, id=uid)


class _FakeClient:
    """Returns a scripted sequence of responses. Records what it was asked."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return _Block(content=self.script.pop(0))


@pytest.fixture()
def patched(monkeypatch):
    def _apply(script, dispatch=None):
        client = _FakeClient(script)
        monkeypatch.setattr(subagent, "_client", lambda: client)
        monkeypatch.setattr(
            "app.agent.tool_node._dispatch",
            dispatch or (lambda *a, **k: {"ok": True}),
        )
        return client

    return _apply


STEP = Step(brief="I need a 7 seater", agent="sales", index=0)


# --------------------------------------------------------------------------- #
# Read-only, twice over
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("agent", ["sales", "testdrive", "finance"])
def test_no_write_tool_is_ever_bound(agent):
    from app.tools import WRITE_TOOLS

    names = {s["name"] for s in subagent._schemas_for(agent)}
    assert not (names & WRITE_TOOLS)


def test_a_write_on_a_card_would_still_be_refused(monkeypatch):
    """THE LAST GATE. differential.py fails the build first, so this should be
    unreachable — but the whole reason a sub-agent may run on a cheaper model is
    that a bad generation cannot write, and that must not rest on one check."""
    from app.agents import loader

    monkeypatch.setattr(
        loader, "frontmatter",
        lambda a, v=None: {"tools": ["search_vehicles", "create_booking"]},
    )
    names = {s["name"] for s in subagent._schemas_for("sales")}
    assert "create_booking" not in names
    assert "search_vehicles" in names


def test_a_card_naming_an_unregistered_tool_is_skipped_not_fatal(monkeypatch):
    """Cards name search_knowledge, which is written but not bound until Phase 2
    registers it. That is a known state, not an error."""
    from app.agents import loader

    monkeypatch.setattr(
        loader, "frontmatter",
        lambda a, v=None: {"tools": ["search_vehicles", "not_a_real_tool"]},
    )
    assert {s["name"] for s in subagent._schemas_for("sales")} == {"search_vehicles"}


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
def test_it_calls_tools_then_returns_findings(patched):
    client = patched([
        [_use("search_vehicles", {"min_seats": 7})],
        [_text('{"findings": {"vehicle_id": 7}, "note": "two seven-seaters"}')],
    ], dispatch=lambda *a, **k: {"matches": [{"vehicle_id": 7}]})

    out = subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert out["vehicle_id"] == 7
    assert out["_note"] == "two seven-seaters"
    assert len(client.calls) == 2


def test_tool_events_are_forwarded_to_the_parent(patched):
    """MISS THIS AND THE CARDS SILENTLY VANISH. runner._card_anchor builds the
    vehicle and slot cards from these, and nothing errors when they never
    arrive — it reads as a styling bug and is not one."""
    patched([
        [_use("search_vehicles", {"min_seats": 7})],
        [_text('{"findings": {}}')],
    ])
    seen = []
    subagent.run_step(STEP, {}, db=None, session_id="s1",
                      emit=lambda k, p: seen.append((k, p)))
    assert seen and seen[0][0] == "tool_event"
    assert seen[0][1]["tool"] == "search_vehicles"


def test_earlier_results_are_handed_to_the_next_step(patched):
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(
        Step(brief="book it", agent="testdrive", index=1),
        {"vehicle_id": 8}, db=None, session_id="s1",
    )
    sent = client.calls[0]["messages"][0]["content"]
    assert "vehicle_id" in sent and "8" in sent


def test_the_brief_carries_constraints_and_never_the_person(patched):
    """Three more models see conversation context from here. This is the
    data-minimisation line."""
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(
        STEP, {}, db=None, session_id="s1",
        profile={"seats_needed": 7, "area": "Deira"},
    )
    sent = client.calls[0]["messages"][0]["content"]
    assert "Deira" in sent
    for banned in ("mobile", "+971", "booking_ref", "name"):
        assert banned not in sent


def test_the_iteration_ceiling_fails_the_step_rather_than_looping(patched):
    """A sub-agent still looping after six rounds on ONE brief is lost. The
    honest outcome is a failed step the customer hears about."""
    patched([[_use("search_vehicles", {})] for _ in range(10)])
    with pytest.raises(RuntimeError, match="did not settle"):
        subagent.run_step(STEP, {}, db=None, session_id="s1", max_iterations=3)


def test_it_is_bound_to_haiku_not_sonnet(patched):
    from app import config

    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert client.calls[0]["model"] == config.CLAUDE_MODEL
    assert "haiku" in client.calls[0]["model"].lower()


def test_the_prompt_is_the_agents_own_body_plus_the_findings_instruction(patched):
    from app.agents import loader

    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    system = client.calls[0]["system"]
    assert loader.sections("sales")["FINDING_THE_RIGHT_CAR"] in system
    assert "YOU ARE NOT TALKING TO THE CUSTOMER" in system


def test_the_sub_agent_prompt_carries_no_customer_facing_rules(patched):
    """Persona, Arabic register, one-question and no-markdown live in base/ and
    aisha/ alone. A sub-agent writing prose would be a second voice with none of
    the guardrails."""
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    system = client.calls[0]["system"]
    assert "Do not greet" in system
    assert "do not write to the customer" in system


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_json_wrapped_in_a_sentence_is_still_read():
    out = subagent._parse_findings('Here you go: {"findings": {"vehicle_id": 3}}')
    assert out == {"vehicle_id": 3}


def test_a_reply_with_no_json_returns_nothing_rather_than_guessing():
    """An empty result is reported honestly as a step that found nothing. An
    invented one is a fact nobody can trace."""
    assert subagent._parse_findings("I could not find that.") == {}
    assert subagent._parse_findings("") == {}
    assert subagent._parse_findings("{not json}") == {}


def test_trusted_context_is_closed_over_not_passed_through(patched):
    """Nothing a model produced can reach db, session_id or customer_id."""
    seen = {}

    def dispatch(db, session_id, name, tool_input, customer_id, corr, **kw):
        seen.update(db=db, session_id=session_id, customer_id=customer_id)
        return {}

    patched([[_use("search_vehicles", {})], [_text('{"findings": {}}')]],
            dispatch=dispatch)
    runner = subagent.make_runner("DB", "sess-1", customer_id=42)
    runner(STEP, {})
    assert seen == {"db": "DB", "session_id": "sess-1", "customer_id": 42}


# --------------------------------------------------------------------------- #
# Entity harvesting — found by running it live (Phase 9)
# --------------------------------------------------------------------------- #
def test_declared_entities_are_read_from_tool_results_not_from_the_model():
    """THE LIVE FAILURE THIS FIXES. Asked to put the next step's ids in its JSON,
    Haiku returned `{"results": ..., "search_performed": true}` for a Sales brief
    whose card declares `produces: [vehicle_id, variant_id]`. Nothing threaded
    forward and the dependent TestDrive step ran with an empty context.

    Asking harder is the wrong fix — the ids are sitting in the tool result the
    model just received. Reading them is deterministic and done once, the same
    lesson agent/sanitise.py records about rule 17.
    """
    results = [{"matches": [{"vehicle_id": 7, "variant_id": 13}, {"vehicle_id": 9}]}]
    assert subagent._harvest_entities("sales", results) == {
        "vehicle_id": 7, "variant_id": 13
    }


def test_the_first_occurrence_wins():
    """A search returns a RANKED list; the first row is the one it recommended.
    Later steps must get the car it led with, not the last one it mentioned."""
    results = [{"matches": [{"vehicle_id": 7}, {"vehicle_id": 9}, {"vehicle_id": 3}]}]
    assert subagent._harvest_entities("sales", results)["vehicle_id"] == 7


def test_nothing_is_invented_when_the_tools_returned_nothing():
    assert subagent._harvest_entities("sales", [{"error": "vehicle_not_found"}]) == {}
    assert subagent._harvest_entities("sales", []) == {}


def test_only_the_entities_the_card_declares_are_harvested():
    """A sub-agent does not get to introduce entities its card never promised —
    the ordering is built on those declarations."""
    results = [{"matches": [{"vehicle_id": 7, "slot_id": 41, "booking_ref": "TD-1"}]}]
    harvested = subagent._harvest_entities("sales", results)
    assert "vehicle_id" in harvested
    assert "slot_id" not in harvested and "booking_ref" not in harvested


def test_the_model_can_still_supply_what_the_tools_did_not(patched):
    """Harvesting FILLS GAPS; it does not overwrite. A model that did return the
    id keeps its value."""
    patched([[_text('{"findings": {"vehicle_id": 99}}')]])
    out = subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert out["vehicle_id"] == 99


def test_harvested_ids_reach_the_findings(patched):
    patched(
        [[_use("search_vehicles", {})], [_text('{"findings": {"note": "found some"}}')]],
        dispatch=lambda *a, **k: {"matches": [{"vehicle_id": 7, "variant_id": 13}]},
    )
    out = subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert out["vehicle_id"] == 7 and out["variant_id"] == 13
