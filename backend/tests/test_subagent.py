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

from tests.conftest import wire_text
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
    sent = wire_text(client.calls[0]["messages"][0]["content"])
    assert "vehicle_id" in sent and "8" in sent


def test_the_brief_carries_constraints_and_never_the_person(patched):
    """Three more models see conversation context from here. This is the
    data-minimisation line."""
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(
        STEP, {}, db=None, session_id="s1",
        profile={"seats_needed": 7, "area": "Deira"},
    )
    sent = wire_text(client.calls[0]["messages"][0]["content"])
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
    system = wire_text(client.calls[0]["system"])
    assert loader.sections("sales")["FINDING_THE_RIGHT_CAR"] in system
    assert "YOU ARE NOT TALKING TO THE CUSTOMER" in system


def test_the_sub_agent_prompt_carries_no_customer_facing_rules(patched):
    """Persona, Arabic register, one-question and no-markdown live in base/ and
    aisha/ alone. A sub-agent writing prose would be a second voice with none of
    the guardrails.

    ASSERTED AS ABSENCE since 2026-09-01. This test used to check only that
    the findings instruction was present, which passed while the full base
    prefix — the very persona and output rules its docstring denies — rode on
    every specialist call (~7.4k tokens). Now the wire prompt itself is
    checked against the base blocks."""
    from app.agents import loader

    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    system = wire_text(client.calls[0]["system"])
    assert "Do not greet" in system
    assert "do not write to the customer" in system
    base = loader.sections("base")
    for name in ("WHO_YOU_ARE", "LANGUAGE", "HOW_YOU_TALK", "OUTPUT_FORMAT",
                 "CLOSING", "BRINGING_IN_A_PERSON"):
        assert base[name] not in system, f"specialist wire prompt carries base {name}"
    for name in loader.SUBAGENT_BASE_ORDER:
        assert base[name] in system, f"specialist wire prompt lost base {name}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_json_wrapped_in_a_sentence_is_still_read():
    out = subagent._parse_findings('Here you go: {"findings": {"vehicle_id": 3}}')
    assert out == {"vehicle_id": 3}


def test_a_reply_with_no_json_invents_no_findings():
    """No fact is guessed at: an unparsed sentence yields no findings and no
    entities, so nothing untraceable can reach the customer."""
    for reply in ("I could not find that.", "{not json}", '"a bare string"'):
        out = subagent._parse_findings(reply)
        assert [k for k in out if not k.startswith("_")] == []


def test_a_reply_with_no_json_is_carried_as_a_note_not_discarded():
    """30% of live steps answered in prose and every byte was thrown away, which
    left Aisha re-briefing the same specialist until the cascade ceiling."""
    assert subagent._parse_findings("I could not find that.") == {
        "_note": "I could not find that."
    }
    assert subagent._parse_findings("{not json}") == {"_note": "{not json}"}
    # Nothing to carry stays nothing.
    assert subagent._parse_findings("") == {}
    assert subagent._parse_findings("   ") == {}


def test_a_carried_note_is_capped():
    out = subagent._parse_findings("x" * 9000)
    assert len(out["_note"]) == subagent.PROSE_NOTE_MAX_CHARS


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


def test_an_offer_of_times_establishes_no_slot():
    """Found live, 2026-08-31. A reschedule ran get_booking then
    get_available_slots; the walk took the first slot_id it met, and
    run.execute's last-step-wins fold put slot 477 — 5 PM, the first row of the
    OFFER — into session_facts over the booking's real slot. The customer had
    chosen 6:30 PM.

    The first row is the right answer for a ranked SEARCH and the wrong one for
    a list of free times, where nothing is established until the customer picks.
    """
    results = [
        {"booking": {"booking_ref": "TD-1", "slot_id": 91}},
        {"slots": [{"slot_id": 477}, {"slot_id": 530}]},
    ]
    tools = ["get_booking", "get_available_slots"]
    assert subagent._harvest_entities("testdrive", results, tools) == {
        "booking_ref": "TD-1", "slot_id": 91
    }
    # And availability ALONE establishes no slot at all.
    assert subagent._harvest_entities(
        "testdrive", [{"slots": [{"slot_id": 477}]}], ["get_available_slots"]
    ) == {}


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


# --------------------------------------------------------------------------- #
# The booking state arrives as data (found live, 2026-08-31)
# --------------------------------------------------------------------------- #
def test_a_held_slot_reaches_the_brief_as_an_established_fact(patched, db, open_slot):
    """A sub-agent briefed to finish a booking used to re-verify with
    get_available_slots, find the customer's own held slot missing (the hold
    is what locked it), and report their choice as taken. The derived booking
    state goes into the brief as data, so the cascade and the write gate can
    never disagree about where the booking is."""
    from app.services import slots as slots_service

    slots_service.lock(db, session_id="s-held", slot_id=open_slot.id)
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(
        Step(brief="finish the booking", agent="testdrive", index=0),
        {}, db=db, session_id="s-held",
    )
    sent = wire_text(client.calls[0]["messages"][0]["content"])
    assert "held_slot_id" in sent and str(open_slot.id) in sent
    assert "booking_stage" in sent


def test_no_hold_means_no_booking_state_in_the_brief(patched, db):
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=db, session_id="s-none")
    sent = wire_text(client.calls[0]["messages"][0]["content"])
    assert "held_slot_id" not in sent


def test_no_hold_briefs_the_lock_not_a_recheck(patched, db):
    """The 2026-08-31 incident from the other side (found live, 2026-09-01). A
    customer accepted the one offered slot IN WORDS ("Yes, book it!"), so no
    lock was ever taken — and the step briefed to finish the booking re-verified
    with get_available_slots, where the unheld slot has no held_by_you
    protection: the 3-result cap left the accepted, still-open slot out, and
    the step read absence as taken. The brief forbids exactly that misreading."""
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(
        Step(brief="finish the booking", agent="testdrive", index=0),
        {}, db=db, session_id="s-unheld",
    )
    sent = wire_text(client.calls[0]["messages"][0]["content"])
    assert "No slot is held" in sent
    assert "hold_slot" in sent


def test_the_no_hold_brief_is_only_for_agents_that_can_hold(patched, db):
    """Sales proposes nothing, so it must not be handed a booking verb — the
    same reason PROPOSALS_INSTRUCTION is appended only for proposing agents."""
    client = patched([[_text('{"findings": {}}')]])
    subagent.run_step(STEP, {}, db=db, session_id="s-none")
    sent = wire_text(client.calls[0]["messages"][0]["content"])
    assert "No slot is held" not in sent


def test_a_lookup_agent_settles_without_a_summary_round(patched, db):
    """MEASURED over 40 live steps, 2026-08-31: a round costs ~2,187ms and 29 of
    40 took two — one to call the tool, one to write findings about the result.
    Nothing reads that second round: _harvest_entities takes the ids from the
    tool RESULTS and `_evidence` hands Aisha those results whole. One step spent
    821 chars of JSON on two keys; another wrote 327 chars and delivered none.

    Sales can propose nothing, so for it that round is summary and nothing else.
    """
    client = patched(
        [[_use("search_vehicles", {"query": "7 seater"})]],
        dispatch=lambda *a, **k: {"matches": [{"vehicle_id": 7, "variant_id": 13}]},
    )
    out = subagent.run_step(
        Step(brief="a 7-seater for a family", agent="sales", index=0),
        {}, db=db, session_id="s1",
    )
    # ONE model call, not two.
    assert len(client.calls) == 1, f"took {len(client.calls)} rounds"
    assert out["vehicle_id"] == 7 and out["variant_id"] == 13
    assert out["_evidence"][0]["tool"] == "search_vehicles"


def test_an_unfinished_lookup_still_gets_its_next_round(patched, db):
    """THE GUARD ON TOOL CHAINING. Sales legitimately searches and then looks up
    details on what it found; a step that has not produced its declared entities
    has not finished working, so it must not be settled early."""
    client = patched(
        [[_use("search_vehicles", {"query": "x"})],
         [_text('{"findings": {"vehicle_id": 7, "variant_id": 13}}')]],
        dispatch=lambda *a, **k: {"matches": []},   # nothing harvestable yet
    )
    subagent.run_step(Step(brief="find one", agent="sales", index=0),
                      {}, db=db, session_id="s1")
    assert len(client.calls) == 2, "an unfinished step must get another round"


def test_an_agent_that_can_propose_always_gets_its_second_look(patched, db):
    """TestDrive reads the free times and THEN proposes a hold on one. Cutting
    its second round would cut the ask."""
    client = patched(
        [[_use("get_available_slots", {"date_from": "2026-09-01"})],
         [_text('{"findings": {}, "proposals": '
                '[{"action": "hold_slot", "args": {"slot_id": 5}}]}')]],
        dispatch=lambda *a, **k: {"slots": [{"slot_id": 5}], "booking_ref": "TD-1"},
    )
    out = subagent.run_step(Step(brief="times on Tuesday", agent="testdrive", index=0),
                            {}, db=db, session_id="s1")
    assert len(client.calls) == 2, "a proposing agent must keep its second round"
    assert out["_proposals"][0]["action"] == "hold_slot"


def test_finance_settles_on_the_figure_its_tools_actually_return(patched, db):
    """Its card declared `produces: [emi_estimate, on_road_price]` — two names
    that exist nowhere in finance_tools.py — so nothing was ever harvested,
    every finance step logged `entities=[]`, and all five measured on
    2026-08-31 paid a second round to summarise what they already had."""
    client = patched(
        [[_use("get_finance_estimate", {"variant_id": 13})]],
        dispatch=lambda *a, **k: {
            "on_road_aed": 96500,
            "options": [{"bank": "A", "monthly_aed": 2459}],
        },
    )
    out = subagent.run_step(
        Step(brief="monthly instalments on the T2", agent="finance", index=0),
        {}, db=db, session_id="s1",
    )
    assert len(client.calls) == 1, f"took {len(client.calls)} rounds"
    assert out["on_road_aed"] == 96500


def test_no_single_monthly_figure_is_threaded_as_the_instalment(patched, db):
    """`options` is one entry per bank and the card says every one of them stays
    visible. Harvesting the first would thread one lender's number as though it
    were THE monthly payment."""
    patched(
        [[_use("get_finance_estimate", {"variant_id": 13})]],
        dispatch=lambda *a, **k: {
            "on_road_aed": 96500,
            "options": [{"bank": "A", "monthly_aed": 2459},
                        {"bank": "B", "monthly_aed": 2610}],
        },
    )
    out = subagent.run_step(
        Step(brief="monthly instalments on the T2", agent="finance", index=0),
        {}, db=db, session_id="s1",
    )
    assert "monthly_aed" not in out
    # The full panel still reaches Aisha, whole.
    assert len(out["_evidence"][0]["result"]["options"]) == 2


def test_calling_the_write_and_writing_the_proposal_is_one_ask(patched, db):
    """FOUND LIVE 2026-08-31. The specialist both CALLED create_booking and
    WROTE a proposal for the same booking, with different argument sets. Keyed
    on the whole dict those were two keys, so both 'executed' and the customer
    saw two identical booking cards under one reply."""
    patched(
        [[_use("create_booking", {"vehicle_id": 7, "slot_id": 1401,
                                  "companions": "", "child_seat": False})],
         [_text('{"findings": {}, "proposals": '
                '[{"action": "create_booking", '
                '"args": {"slot_id": 1401, "vehicle_id": 7}}]}')]],
    )
    out = subagent.run_step(
        Step(brief="book it", agent="testdrive", index=0),
        {}, db=db, session_id="s1",
    )
    books = [p for p in out["_proposals"] if p["action"] == "create_booking"]
    assert len(books) == 1, out["_proposals"]
    # The INTERCEPT wins: it is what the model actually tried to call, in full.
    assert books[0]["args"]["companions"] == ""
    # The hold the card requires is still prepended, and is its own ask.
    assert [p["action"] for p in out["_proposals"]] == ["hold_slot", "create_booking"]


def test_a_move_to_a_different_slot_is_a_different_ask(patched, db):
    """The subject includes the slot, so two moves are not silently merged."""
    patched(
        [[_use("reschedule_booking", {"ref": "TD-1", "new_slot_id": 5})],
         [_text('{"findings": {}, "proposals": '
                '[{"action": "reschedule_booking", '
                '"args": {"ref": "TD-1", "new_slot_id": 9}}]}')]],
    )
    out = subagent.run_step(
        Step(brief="move it", agent="testdrive", index=0),
        {}, db=db, session_id="s1",
    )
    moves = [p for p in out["_proposals"] if p["action"] == "reschedule_booking"]
    assert len(moves) == 2, moves


def test_hold_slot_and_lock_slot_are_one_action(patched, db):
    """write.py maps hold_slot onto lock_slot; two names for one ask must not
    survive as two proposals."""
    assert subagent._proposal_key(
        {"action": "hold_slot", "args": {"slot_id": 5}}
    ) == subagent._proposal_key(
        {"action": "lock_slot", "args": {"slot_id": 5}}
    )


def test_a_proposal_missing_its_subject_is_not_merged(patched, db):
    """Merging two requests is worse than performing a duplicate one — the
    service guards already make the duplicate safe."""
    a = subagent._proposal_key({"action": "create_booking", "args": {"vehicle_id": 7}})
    b = subagent._proposal_key({"action": "create_booking", "args": {"vehicle_id": 9}})
    assert a != b


def test_a_tool_error_is_not_settled_early(patched, db):
    """A step whose tool failed has not produced anything to settle on."""
    client = patched(
        [[_use("search_vehicles", {"query": "x"})],
         [_text('{"findings": {}}')]],
        dispatch=lambda *a, **k: {"error": "vehicle_not_found", "vehicle_id": 7},
    )
    subagent.run_step(Step(brief="find one", agent="sales", index=0),
                      {}, db=db, session_id="s1")
    assert len(client.calls) == 2


def test_a_write_the_card_proposes_is_read_as_the_proposal(monkeypatch):
    """Found live, 2026-08-31, and this is the SECOND fix for it.

    The card prose is ported single-agent journey text and SAYS "call
    cancel_booking"; Haiku emits the tool_use it was never offered. The first
    fix enforced the binding at dispatch, which correctly stopped the write —
    but a refusal is not an outcome. Measured live on a reschedule: sub-agent
    tool_use refused, Aisha re-briefed, refused again, Aisha tried the tool
    herself, refused, and the customer was told to phone the helpline while the
    booking never moved.

    A tool_use for an action the card PROPOSES is the proposal, in the tool's
    own argument names. Reading it as one is deterministic, costs no extra
    round, and asking harder had already failed twice."""
    client = _FakeClient([
        [_use("cancel_booking", {"ref": "TD-2026-0KXC"})],
        [_text('{"findings": {}}')],
    ])
    monkeypatch.setattr(subagent, "_client", lambda: client)

    out = subagent.run_step(
        Step(brief="cancel booking TD-2026-0KXC", agent="testdrive", index=0),
        {}, db=None, session_id="s1",
    )

    # It was NOT refused, and it was NOT executed — it was recorded as a request.
    recorded = json.loads(client.calls[1]["messages"][2]["content"][0]["content"])
    assert "error" not in recorded
    assert recorded["requested"] == "cancel_booking"
    assert "do not say it has been done" in recorded["detail"].lower()

    assert out["_proposals"] == [
        {"action": "cancel_booking", "args": {"ref": "TD-2026-0KXC"},
         "agent": "testdrive"}
    ]


def test_a_write_the_card_does_not_propose_is_still_refused_at_dispatch(monkeypatch):
    """The interception above is scoped to the card's `proposes` list and the
    allowlist underneath it is untouched. Sales proposes NOTHING, so a write it
    reaches for still meets the refusal — which is the guard the first fix added
    and which must not have been widened into a way in.

    Real _dispatch here, deliberately: the refusal happens before any db use,
    and stubbing it out is exactly how this hole stayed invisible."""
    client = _FakeClient([
        [_use("cancel_booking", {"ref": "TD-2026-0KXC"})],
        [_text('{"findings": {}}')],
    ])
    monkeypatch.setattr(subagent, "_client", lambda: client)

    out = subagent.run_step(
        Step(brief="cancel it", agent="sales", index=0),
        {}, db=None, session_id="s1",
    )

    refusal = json.loads(client.calls[1]["messages"][2]["content"][0]["content"])
    assert refusal["error"] == "tool_not_available"
    assert "_proposals" not in out


def test_asking_twice_in_one_round_is_one_request(monkeypatch):
    """A model that calls the tool AND writes the proposal has asked once.
    write.execute_all runs a list in order, so a duplicate would reschedule the
    same booking to the same slot twice."""
    client = _FakeClient([
        [_use("reschedule_booking", {"booking_ref": "TD-2026-0KXC", "new_slot_id": "530"})],
        [_text('{"findings": {}, "proposals": [{"action": "reschedule_booking", '
               '"args": {"ref": "TD-2026-0KXC", "new_slot_id": 530}}]}')],
    ])
    monkeypatch.setattr(subagent, "_client", lambda: client)

    out = subagent.run_step(
        Step(brief="move it", agent="testdrive", index=0),
        {}, db=None, session_id="s1",
    )

    # `booking_ref` is the name every tool RESULT uses; the write tool takes
    # `ref`, and "530" arrives as a string because the model copied it out of
    # prose. Both are normalised, which is also what makes the dedupe possible.
    assert out["_proposals"] == [
        {"action": "reschedule_booking",
         "args": {"ref": "TD-2026-0KXC", "new_slot_id": 530},
         "agent": "testdrive"}
    ]


# --------------------------------------------------------------------------- #
# Terminal reads settle after their tool round (2026-09-02)
#
# A knowledge question paid the measured ~2,187ms summary round because
# search_knowledge produces no card entity, so the harvest gate never fired —
# a Haiku call writing prose about passages Aisha receives verbatim in
# `_evidence` anyway. A step whose every call is a self-contained document
# read now settles straight after its tools; anything that could be a chain
# (search_vehicles, compare_vehicles, the finance ladder) keeps its rounds.
# --------------------------------------------------------------------------- #
def test_a_knowledge_only_step_settles_in_one_round(patched):
    client = patched(
        [[_use("search_knowledge", {"query": "warranty of the Dashing"})]],
        dispatch=lambda *a, **k: {"found": True, "passages": [{"text": "10 years"}]},
    )
    out = subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert len(client.calls) == 1, "the summary round is the defect this removes"
    assert out["_evidence"][0]["tool"] == "search_knowledge"


def test_a_compare_step_keeps_its_summary_round(patched):
    """compare_vehicles is NOT terminal: a compare brief legitimately follows
    up ('...and which has the better warranty')."""
    client = patched([
        [_use("compare_vehicles", {"names": ["X70", "T2"]})],
        [_text('{"findings": {}}')],
    ], dispatch=lambda *a, **k: {"rows": []})
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert len(client.calls) == 2


def test_a_mixed_step_keeps_its_rounds(patched):
    """search_vehicles in the mix means a chain may be underway — settling on
    ALL tools called, not just this round's, is what protects it."""
    client = patched([
        [_use("search_vehicles", {"min_seats": 7})],
        [_use("search_knowledge", {"query": "warranty"})],
        [_text('{"findings": {}}')],
    ], dispatch=lambda *a, **k: {"found": True, "passages": []})
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert len(client.calls) == 3


def test_an_erroring_terminal_read_still_gets_its_summary_round(patched):
    """An error envelope is a finding the model should read and react to, not
    something to settle over silently."""
    client = patched([
        [_use("search_knowledge", {"query": ""})],
        [_text('{"findings": {}}')],
    ], dispatch=lambda *a, **k: {"error": "missing_query"})
    subagent.run_step(STEP, {}, db=None, session_id="s1")
    assert len(client.calls) == 2
