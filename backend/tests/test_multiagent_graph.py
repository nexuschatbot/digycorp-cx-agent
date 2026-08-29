"""The orchestrator node and the edge that reaches it (T5.1 wiring).

WHAT WAS MISSING BEFORE THIS. Every piece of the cascade was built and tested —
select, order, execute, budgets, event forwarding — and none of it was reachable:
the graph had two nodes, `agent` and `tools`, and nothing in app/ imported
app.orchestrator.turn. MULTI_AGENT_ENABLED gated a function no request called.
These tests are the ones that would have failed while that was true.

NOTHING HERE CALLS A MODEL. The cascade takes an injected runner and the node
takes an injected generate, which is what lets the whole fan-out be asserted
offline — the same property the rest of the suite relies on.
"""

from __future__ import annotations

import json

import pytest

from app.agent import graph
from app.tools import TOOL_SCHEMAS
from app.tools.brief_tools import CALLED_ALONGSIDE_ERROR


def _assistant(*blocks) -> dict:
    return {"role": "assistant", "content": list(blocks)}


def _tool_use(name: str, tid: str = "t1", **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _briefs(*texts, tid: str = "b1") -> dict:
    return _tool_use(
        "emit_briefs", tid, briefs=[{"brief": t} for t in texts]
    )


# --------------------------------------------------------------------------- #
# The edge
# --------------------------------------------------------------------------- #
def test_emit_briefs_routes_to_the_orchestrator():
    state = {"messages": [_assistant(_briefs("what seven seaters do you have"))]}
    assert graph._route_from_agent(state) == "orchestrator"


def test_an_ordinary_tool_still_routes_to_tools():
    state = {"messages": [_assistant(_tool_use("get_showrooms"))]}
    assert graph._route_from_agent(state) == "tools"


def test_a_text_only_reply_still_ends():
    state = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]}
    assert graph._route_from_agent(state) == graph.END


def test_briefs_win_when_mixed_with_another_tool():
    """The tools node would return unknown_tool for emit_briefs and strand the
    turn on an opening line that has already been delivered."""
    state = {"messages": [_assistant(_tool_use("get_showrooms", "a"), _briefs("x", tid="b"))]}
    assert graph._route_from_agent(state) == "orchestrator"


def test_the_orchestrator_node_is_in_the_compiled_graph():
    compiled = graph.build_graph(checkpointer=None)
    assert "orchestrator" in compiled.get_graph().nodes


# --------------------------------------------------------------------------- #
# The stream adapter — the one that silently blanks the UI
# --------------------------------------------------------------------------- #
def test_stream_sink_flattens_kind_into_type():
    """forward_events calls its sink with (kind, payload); the LangGraph writer
    takes ONE dict keyed by `type`. Reversed, every sub-agent card vanishes and
    nothing raises."""
    seen = []
    sink = graph._stream_sink(seen.append)
    sink("tool_event", {"tool": "search_vehicles", "output": {"matches": []}})
    assert seen == [
        {"type": "tool_event", "tool": "search_vehicles", "output": {"matches": []}}
    ]


def test_stream_sink_tolerates_no_writer():
    graph._stream_sink(None)("tool_event", {"tool": "x"})  # must not raise


def test_forwarded_events_survive_the_whole_path():
    """turn.forward_events -> _stream_sink -> writer, with attribution intact."""
    from app.orchestrator import turn

    seen = []
    emit = turn.forward_events(graph._stream_sink(seen.append), "sales")
    emit("tool_event", {"tool": "search_vehicles", "input": {}, "output": {}})
    assert seen[0]["type"] == "tool_event"
    assert seen[0]["agent"] == "sales"


# --------------------------------------------------------------------------- #
# The node
# --------------------------------------------------------------------------- #
def _config(**over):
    cfg = {"db": None, "session_id": "s1", "deadline_monotonic": None}
    cfg.update(over)
    return {"configurable": cfg}


def _result_for(out: dict, tool_use_id: str) -> dict:
    for block in out["messages"][0]["content"]:
        if block["tool_use_id"] == tool_use_id:
            return json.loads(block["content"])
    raise AssertionError(f"no tool_result for {tool_use_id}")


def test_every_tool_use_gets_a_tool_result(monkeypatch):
    """A tool_use without a matching tool_result is an API error next turn."""
    monkeypatch.setattr(
        graph, "_known_entities", lambda state: set()
    )
    state = {
        "messages": [_assistant(_tool_use("get_showrooms", "a"), _briefs("x", tid="b"))],
        "session_facts": {},
    }
    out = graph._orchestrator_node(state, _config())
    ids = {b["tool_use_id"] for b in out["messages"][0]["content"]}
    assert ids == {"a", "b"}


def test_a_tool_called_alongside_briefs_is_refused_not_run():
    state = {
        "messages": [_assistant(_tool_use("get_showrooms", "a"), _briefs("x", tid="b"))],
        "session_facts": {},
    }
    out = graph._orchestrator_node(state, _config())
    assert _result_for(out, "a") == CALLED_ALONGSIDE_ERROR


def test_no_briefs_is_answered_not_crashed():
    state = {"messages": [_assistant(_tool_use("emit_briefs", "b", briefs=[]))], "session_facts": {}}
    out = graph._orchestrator_node(state, _config())
    body = _result_for(out, "b")
    assert body["nothing_worked"] is True
    assert body["findings"] == []


def test_a_failing_cascade_still_returns_something_to_say(monkeypatch):
    """The opening line is already delivered; the turn must not end silent."""
    from app.orchestrator import turn

    def _boom(*a, **k):
        raise RuntimeError("cascade down")

    monkeypatch.setattr(turn, "execute", _boom)
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {}}
    out = graph._orchestrator_node(state, _config())
    body = _result_for(out, "b")
    assert body["nothing_worked"] is True
    assert body["detail"]


def test_the_node_does_not_consume_a_generation():
    """`iterations` bounds MODEL turns, and this node calls none. Counting it
    would cost Aisha the composition turn the budget exists to protect."""
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {}}
    out = graph._orchestrator_node(state, _config())
    assert "iterations" not in out


def test_findings_are_threaded_back_as_the_tool_result(monkeypatch):
    from app.orchestrator import run, turn

    def _fake(briefs, runner, **kw):
        return run.TurnResult(steps=[
            run.StepResult(brief="x", agent="sales", status="ok",
                           findings={"vehicle_id": 7, "price": "AED 89,000"}),
        ])

    monkeypatch.setattr(turn, "execute", _fake)
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {}}
    body = _result_for(graph._orchestrator_node(state, _config()), "b")
    assert body["findings"] == [{"agent": "sales", "data": {"vehicle_id": 7, "price": "AED 89,000"}}]
    assert body["nothing_worked"] is False


# --------------------------------------------------------------------------- #
# Known entities
# --------------------------------------------------------------------------- #
def test_session_entities_are_passed_as_known():
    """'book it for Saturday' must not be blocked on a Sales step for a car the
    customer chose ten turns ago."""
    assert graph._known_entities({"session_facts": {"vehicle_id": 7}}) == {"vehicle_id"}


def test_unrelated_facts_are_not_treated_as_entities():
    known = graph._known_entities({"session_facts": {"name": "Ahmed", "mobile": "050..."}})
    assert known == set()


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #
def test_flag_off_means_voice_and_chat_both_single_agent(monkeypatch):
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", False)
    assert graph._multi_agent({}) is False
    assert graph._multi_agent({"voice_call": True}) is False


def test_voice_is_excluded_even_when_the_flag_is_on(monkeypatch):
    """400ms first-audio budget; the Haiku round trip alone measured 1088ms."""
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    assert graph._multi_agent({}) is True
    assert graph._multi_agent({"voice_call": True}) is False


@pytest.mark.parametrize("flag", [False, True])
def test_the_single_agent_payload_never_changes(monkeypatch, flag):
    """With the fan-out off, Aisha is handed exactly what she was handed before
    — the whole registry, byte-identical, so the cached prefix survives."""
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", flag)
    from app.tools import aisha_schemas

    if flag:
        assert [s["name"] for s in aisha_schemas()] != [s["name"] for s in TOOL_SCHEMAS]
    assert len(TOOL_SCHEMAS) == 22


# --------------------------------------------------------------------------- #
# The bubble seal (T5.1)
# --------------------------------------------------------------------------- #
# WHY THIS IS TESTED HERE AND NOT BUILT HERE. emit_briefs' description promises
# Aisha that her opening line is DELIVERED and cannot be retracted, and the
# whole one-generation trick rests on that being true. It already is: graph.py
# seals the bubble on the start of ANY tool_use block, and emit_briefs is an
# ordinary tool_use as far as the model and the SDK are concerned. So there is
# no new mechanism — but the promise is now load-bearing on a path that did not
# exist when the seal was written, and an unasserted promise is a rule waiting
# to be refactored away.
from types import SimpleNamespace  # noqa: E402

from tests.test_lang_guard import _FakeStream, _text_events  # noqa: E402


def _briefs_tool_start():
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", id="b1", name="emit_briefs"),
    )


def _generate(monkeypatch, stream):
    from tests.test_lang_guard import _FakeClient

    client = _FakeClient([stream])
    emitted = []
    monkeypatch.setattr(graph, "_client", client)
    monkeypatch.setattr(graph, "_safe_writer", lambda: emitted.append)
    graph._default_generate(
        [{"role": "user", "content": "what seven seaters do you have"}],
        model="test-model", system="SYSTEM", tools=[],
        config={"configurable": {"session_id": "s1", "expected_lang": "en"}},
    )
    return emitted


def test_the_opening_line_is_sealed_before_the_briefs_run(monkeypatch):
    """Opening line delivered as its own bubble, then emit_briefs. Without the
    seal, her acknowledgement and her composed answer arrive as one run-on
    bubble — and the promise the tool description makes her is false."""
    stream = _FakeStream(
        _text_events("Let me check that ", "for you.") + [_briefs_tool_start()],
        stop_reason="tool_use",
    )
    emitted = _generate(monkeypatch, stream)
    kinds = [e.get("type") for e in emitted]
    assert "text_break" in kinds, "the opening line was never sealed"
    # And it seals AFTER the text, not before: the words are delivered first.
    assert kinds.index("text_break") == len(kinds) - 1
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "Let me check that for you."


def test_briefs_with_no_opening_line_seal_nothing(monkeypatch):
    """No text, no bubble to seal — an empty break would render as a stray gap."""
    stream = _FakeStream([_briefs_tool_start()], stop_reason="tool_use")
    emitted = _generate(monkeypatch, stream)
    assert "text_break" not in [e.get("type") for e in emitted]


def test_a_wrong_language_opening_aborts_before_the_cascade(monkeypatch):
    """Rule 3 still bites on the brief path: the seal is where the window
    settles, so a violation stops the turn BEFORE any sub-agent is paid for."""
    stream = _FakeStream(
        _text_events("حسناً، ") + [_briefs_tool_start()], stop_reason="tool_use"
    )
    from tests.test_lang_guard import _FakeClient

    client = _FakeClient([stream, _FakeStream(_text_events("Let me check."))])
    emitted = []
    monkeypatch.setattr(graph, "_client", client)
    monkeypatch.setattr(graph, "_safe_writer", lambda: emitted.append)
    graph._default_generate(
        [{"role": "user", "content": "hi"}],
        model="test-model", system="SYSTEM", tools=[],
        config={"configurable": {"session_id": "s1", "expected_lang": "en"}},
    )
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "Let me check."
    assert len(client.calls) == 2


# --------------------------------------------------------------------------- #
# The write path (T4.2)
# --------------------------------------------------------------------------- #
# SUB-AGENTS PROPOSE; THE ORCHESTRATOR EXECUTES. Before this was wired,
# write.py had no caller anywhere in app/ — Aisha correctly held no commercial
# write, no sub-agent held one, and the component that was meant to own them was
# connected to nothing. With the flag on, no booking could be created at all.
from app.orchestrator import run as _run  # noqa: E402


def _step_with(proposals, status="ok", agent="testdrive"):
    return _run.StepResult(brief="b", agent=agent, status=status,
                           findings={}, proposals=proposals)


class _FakeState:
    def __init__(self, allowed):
        self.allowed = allowed
        self.stage = "create"


def _patch_state(monkeypatch, allowed):
    from app.orchestrator import booking_state
    monkeypatch.setattr(booking_state, "read",
                        lambda db, sid, customer_id=None: _FakeState(allowed))


def test_no_proposals_means_no_writes():
    assert graph._execute_proposals(_run.TurnResult(steps=[_step_with([])]), {}, {}) == []


def test_a_proposal_reaches_write_execute_all(monkeypatch):
    from app.orchestrator import write

    seen = {}

    def _fake(db, sid, proposals, *, customer_id=None, allow=None):
        seen["proposals"] = proposals
        seen["allow"] = allow
        return [write.WriteResult(action="create_booking", status="executed",
                                  result={"booking_ref": "TD-2026-0001"})]

    monkeypatch.setattr(write, "execute_all", _fake)
    _patch_state(monkeypatch, {"create_booking"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)

    result = _run.TurnResult(steps=[
        _step_with([{"action": "create_booking", "args": {"slot_id": 5}, "agent": "testdrive"}])
    ])
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    out = graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})

    assert seen["allow"] == {"create_booking"}
    assert seen["proposals"][0].action == "create_booking"
    assert out == [{"action": "create_booking", "status": "executed",
                    "detail": "", "booking_ref": "TD-2026-0001"}]


def test_the_state_machine_gate_is_passed_through(monkeypatch):
    """write.execute refuses an action the machine does not allow RIGHT NOW."""
    _patch_state(monkeypatch, {"hold_slot", "lock_slot"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    result = _run.TurnResult(steps=[_step_with([{"action": "create_booking", "args": {}}])])
    out = graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})
    assert out[0]["status"] == "refused"
    assert "not valid at this point" in out[0]["detail"]


def test_an_executed_write_commits_and_a_refused_one_does_not(monkeypatch):
    from app.orchestrator import write

    calls = []
    db = SimpleNamespace(commit=lambda: calls.append("commit"),
                         rollback=lambda: calls.append("rollback"))
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)
    _patch_state(monkeypatch, {"create_booking"})

    monkeypatch.setattr(write, "execute_all", lambda *a, **k: [
        write.WriteResult(action="create_booking", status="executed", result={})])
    graph._execute_proposals(
        _run.TurnResult(steps=[_step_with([{"action": "create_booking"}])]),
        {}, {"db": db, "session_id": "s1"})
    assert calls == ["commit"]

    calls.clear()
    monkeypatch.setattr(write, "execute_all", lambda *a, **k: [
        write.WriteResult(action="create_booking", status="refused", detail="no licence")])
    graph._execute_proposals(
        _run.TurnResult(steps=[_step_with([{"action": "create_booking"}])]),
        {}, {"db": db, "session_id": "s1"})
    assert calls == ["rollback"]


def test_an_unreadable_booking_state_refuses_rather_than_guesses(monkeypatch):
    from app.orchestrator import booking_state

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(booking_state, "read", _boom)
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    out = graph._execute_proposals(
        _run.TurnResult(steps=[_step_with([{"action": "create_booking"}])]),
        {}, {"db": db, "session_id": "s1"})
    assert out[0]["status"] == "refused"


def test_proposals_from_a_failed_step_are_discarded():
    """A step that failed established no facts, so its request rests on nothing."""
    def _runner(step, ctx, **kw):
        raise RuntimeError("boom")

    out = _run.execute([{"brief": "book it"}], _runner,
                       judge=lambda *a, **k: "testdrive")
    assert all(not s.proposals for s in out.steps)


def test_actions_reach_aisha_in_the_compose_payload(monkeypatch):
    """She may only confirm what is listed as executed — so it must be there."""
    from app.orchestrator import turn, write

    monkeypatch.setattr(turn, "execute", lambda *a, **k: _run.TurnResult(
        steps=[_step_with([{"action": "create_booking", "args": {}, "agent": "testdrive"}])]))
    monkeypatch.setattr(write, "execute_all", lambda *a, **k: [
        write.WriteResult(action="create_booking", status="executed",
                          result={"booking_ref": "TD-2026-0042"})])
    _patch_state(monkeypatch, {"create_booking"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)

    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    state = {"messages": [_assistant(_briefs("book it", tid="b"))], "session_facts": {}}
    body = _result_for(
        graph._orchestrator_node(state, {"configurable": {"db": db, "session_id": "s1",
                                                          "deadline_monotonic": None}}),
        "b")
    assert body["actions"] == [{"action": "create_booking", "status": "executed",
                                "detail": "", "booking_ref": "TD-2026-0042"}]


# --------------------------------------------------------------------------- #
# Cross-turn memory (found live, 2026-08-29)
# --------------------------------------------------------------------------- #
# Entities threaded between STEPS from the start; they did not survive the TURN.
# _known_entities reads session_facts and only tool_node wrote it, so a
# sub-agent's vehicle_id was discarded at the end of every turn. Turn 1
# established the T2; turn 3 asked for slots and TestDrive was blocked on
# vehicle_id all over again. Every component was correct and the booking still
# could not happen, because the memory between them was missing.
def _result_with(entities, steps=None):
    r = _run.TurnResult(steps=steps or [])
    r.entities = entities
    return r


def test_cascade_entities_reach_session_facts():
    facts = graph._fold_cascade_facts({"session_facts": {}},
                                      _result_with({"vehicle_id": 7, "variant_id": 21}))
    assert facts["vehicle_id"] == 7
    assert facts["variant_id"] == 21


def test_identity_already_held_is_not_dropped():
    """session_facts is last-value, so a partial write loses the customer."""
    facts = graph._fold_cascade_facts(
        {"session_facts": {"name": "Test Driver", "mobile": "+971509998877"}},
        _result_with({"vehicle_id": 7}))
    assert facts["name"] == "Test Driver"
    assert facts["mobile"] == "+971509998877"
    assert facts["vehicle_id"] == 7


def test_only_the_entities_that_are_consumed_are_kept():
    """Not a scratchpad — whatever lands here is re-stated to the customer every
    turn by the facts anchor."""
    facts = graph._fold_cascade_facts(
        {"session_facts": {}},
        _result_with({"vehicle_id": 7, "emi_estimate": 2459, "on_road_price": 96500}))
    assert set(facts) == {"vehicle_id"}


def test_nothing_established_writes_nothing():
    assert graph._fold_cascade_facts({"session_facts": {"a": 1}}, _result_with({})) is None


def test_a_null_entity_is_not_recorded():
    """run.execute seeds entities from `known` with None values."""
    assert graph._fold_cascade_facts({"session_facts": {}},
                                     _result_with({"vehicle_id": None})) is None


def test_the_car_name_comes_from_a_successful_sales_step():
    facts = graph._fold_cascade_facts(
        {"session_facts": {}},
        _result_with({"vehicle_id": 7}, steps=[
            _run.StepResult(brief="b", agent="sales", status="ok",
                            findings={"name": "Jetour X70 FL Plus"}),
        ]))
    assert facts["vehicle_name"] == "Jetour X70 FL Plus"


def test_a_failed_steps_name_is_not_trusted():
    facts = graph._fold_cascade_facts(
        {"session_facts": {}},
        _result_with({"vehicle_id": 7}, steps=[
            _run.StepResult(brief="b", agent="sales", status="failed",
                            findings={"name": "Something Invented"}),
        ]))
    assert "vehicle_name" not in facts


def test_the_node_returns_session_facts(monkeypatch):
    """The whole point: it must leave the node, or the next turn cannot see it."""
    from app.orchestrator import turn

    monkeypatch.setattr(turn, "execute", lambda *a, **k: _result_with({"vehicle_id": 7}))
    monkeypatch.setattr(graph, "_execute_proposals", lambda *a, **k: [])
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {}}
    out = graph._orchestrator_node(state, _config(db=SimpleNamespace()))
    assert out["session_facts"]["vehicle_id"] == 7


def test_the_next_turn_can_see_it():
    """_known_entities is what order.resolve consults; close the loop."""
    facts = graph._fold_cascade_facts({"session_facts": {}}, _result_with({"vehicle_id": 7}))
    assert graph._known_entities({"session_facts": facts}) == {"vehicle_id"}


# --------------------------------------------------------------------------- #
# The binding is enforced where the call EXECUTES (found live, 2026-08-29)
# --------------------------------------------------------------------------- #
# The architecture said "a prompt can be argued with; an absent tool cannot".
# That was not true: a tool was absent from the SCHEMA LIST handed to the model
# and present in the DISPATCH TABLE. Aisha's prompt is the ported single-agent
# journey prose and it tells her to call lock_slot and create_booking; she is
# granted neither on the multi-agent path; she emitted the tool_use anyway and
# tool_node RAN IT. Inventory was held without the orchestrator involved at all.
def test_a_tool_the_caller_was_not_given_is_refused():
    from app.agent import tool_node

    out = tool_node._dispatch(
        None, "s", "lock_slot", {"slot_id": 2}, None, "corr",
        allow=frozenset({"offer_options"}),
    )
    assert out["error"] == "tool_not_available"
    assert out["tool"] == "lock_slot"


def test_the_refusal_tells_her_the_right_way_to_do_it():
    """A refusal she cannot act on is a dead end; she must reach emit_briefs."""
    from app.agent import tool_node

    out = tool_node._dispatch(
        None, "s", "create_booking", {}, None, "corr", allow=frozenset(),
    )
    assert "emit_briefs" in out["detail"]


def test_no_allowlist_means_no_restriction():
    """Every existing caller passes None and must behave byte-identically."""
    from app.agent import tool_node

    out = tool_node._dispatch(None, "s", "nope_not_a_tool", {}, None, "corr")
    assert out["error"] == "unknown_tool"


def test_the_allowlist_matches_what_the_model_was_handed(monkeypatch):
    """Two lists that must agree, from one source — or the gap reopens silently."""
    from app.agent import tool_node
    from app.tools import aisha_schemas

    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    assert tool_node._bound_tools({}, {}) == frozenset(
        s["name"] for s in aisha_schemas()
    )


def test_no_commercial_write_survives_the_gate(monkeypatch):
    from app.agent import tool_node
    from app.tools import COMMERCIAL_WRITE_TOOLS

    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    assert tool_node._bound_tools({}, {}) & COMMERCIAL_WRITE_TOOLS == frozenset()


def test_the_single_agent_path_is_unrestricted(monkeypatch):
    """It holds the whole registry, so an allowlist is every key and pure cost."""
    from app.agent import tool_node

    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", False)
    assert tool_node._bound_tools({}, {}) is None


def test_a_routed_agent_is_held_to_its_journey(monkeypatch):
    from app.agent import tool_binding, tool_node

    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", False)
    bound = tool_node._bound_tools({"agent": "bookingmanagement"}, {})
    assert bound == frozenset(tool_binding.tools_for("bookingmanagement"))
    assert "create_booking" not in bound


# --------------------------------------------------------------------------- #
# The DELEGATING section reaches the LIVE prompt (2026-08-29)
#
# It is deliberately outside SINGLE_AGENT_ORDER so the shipped body stays
# byte-identical to prompt_body_original.txt. The trap: multi-agent Aisha reads
# that same body, so "absent from the single-agent body" was mistaken for
# "present on the multi-agent path". It was present in body_for("aisha") — the
# SUB-AGENT loader, which Aisha never goes through — and nowhere she could read
# it. Three live turns ran on ported prose telling her to call tools she no
# longer holds. These assert the live path, not the loader.
# --------------------------------------------------------------------------- #
_DELEGATION_RULES = (
    "WHAT IS NOT YOURS ANY MORE",     # she no longer books
    "ASK FOR EVERYTHING IN ONE GO",   # one call, not three round trips
    "ALWAYS SAY SOMETHING FIRST",     # never delegate in silence
    "ONLY THAT IS TRUE",              # confirm nothing that is not executed
)


def test_the_multi_agent_prompt_carries_the_delegation_rules():
    from app.agent.prompt import system_prompt

    live = system_prompt(multi_agent=True)
    for rule in _DELEGATION_RULES:
        assert rule in live, f"multi-agent Aisha cannot read {rule!r}"


def test_the_single_agent_prompt_is_untouched_by_any_of_it():
    """The other half, and the reason the section sits outside the order: with
    the flag off the prompt must stay byte-identical, or the cached prefix dies
    and the ported-prompt fixture fails."""
    from app.agent.prompt import system_prompt

    shipped = system_prompt()
    for rule in _DELEGATION_RULES:
        assert rule not in shipped


def test_the_agent_node_asks_for_the_multi_agent_prompt_when_the_flag_is_on():
    """The prose existing is not the same as it being requested. This asserts
    the CALL — the defect was a correct section nothing ever passed the flag
    for."""
    import inspect

    from app.agent import graph

    src = inspect.getsource(graph._agent_node)
    assert "multi_agent=_multi_agent(cfg)" in src, (
        "the agent node must pass the flag through to system_prompt, or the "
        "delegation rules are unreachable again"
    )


# --------------------------------------------------------------------------- #
# The same card is never drawn twice in one turn (2026-08-29)
#
# Live: a sales sub-agent called search_vehicles twice on ONE brief — two
# different queries, the same two cars back — and the customer saw the X70 FL
# Plus and X70 FL rendered twice, one pair directly under the other. A sub-agent
# is freer to repeat a tool than Aisha ever was, because its loop is its own.
# --------------------------------------------------------------------------- #
def _two_cars():
    return {"matches": [
        {"vehicle_id": 7, "name": "X70 FL Plus"},
        {"vehicle_id": 8, "name": "X70 FL"},
    ]}


def test_two_searches_that_find_the_same_cars_draw_one_card():
    from app.agent.runner import _card_anchor

    a = _card_anchor("search_vehicles", _two_cars())
    b = _card_anchor("search_vehicles", _two_cars())
    assert a == b, "identical results must produce an identical anchor to dedupe on"


def test_a_search_that_finds_different_cars_still_draws_its_own_card():
    """The dedupe keys on WHAT IS DRAWN, not on the tool name — otherwise a
    second, genuinely different search would be silently swallowed."""
    from app.agent.runner import _card_anchor

    a = _card_anchor("search_vehicles", _two_cars())
    b = _card_anchor("search_vehicles", {"matches": [{"vehicle_id": 1, "name": "G700"}]})
    assert a != b


def test_a_non_card_tool_is_never_deduped():
    """Slot chips, option chips and the working label all ride on tool_events
    with no anchor, and a repeat there is meaningful — a new slot offer must
    replace the old one, not be dropped as a duplicate."""
    from app.agent.runner import _card_anchor

    assert _card_anchor("get_available_slots", {"slots": [{"slot_id": 1}]}) is None
    assert _card_anchor("offer_options", {"options": ["a", "b"]}) is None


def test_the_runner_suppresses_the_repeat_rather_than_only_the_row():
    """Row and event must agree. Suppressing only the persisted row would leave
    the duplicate on screen and remove it on reload; suppressing only the event
    would do the reverse."""
    import inspect

    from app.agent import runner

    src = inspect.getsource(runner.run_turn if hasattr(runner, "run_turn") else runner)
    assert "drawn_anchors" in src
    # The `continue` is what skips on_event as well as the row.
    assert "if key in drawn_anchors:" in src and "continue" in src


# --------------------------------------------------------------------------- #
# The cascade reaches turn_trace.steps (2026-08-29)
#
# `steps` was NULL on all 24 rows ever written. run.trace_steps has built the
# structure since T3.5 and turn_trace.record has accepted a `steps=` argument
# the whole time — nothing ever carried it out of the orchestrator node. The
# routing view showed `routed_to='testdrive'` for turns sales handled end to
# end, because that column is the legacy journey router, not the cascade.
# --------------------------------------------------------------------------- #
def test_the_orchestrator_emits_its_routing_on_the_stream():
    import inspect

    from app.agent import graph

    src = inspect.getsource(graph._orchestrator_node)
    assert '"type": "cascade"' in src
    assert "trace_steps(result)" in src


def test_the_runner_collects_the_cascade_and_records_it():
    import inspect

    from app.agent import runner

    src = inspect.getsource(runner)
    assert 'kind == "cascade"' in src
    assert "cascade_steps.extend" in src
    assert "steps=cascade_steps or None" in src, (
        "collected but not passed to record() is the defect this replaces"
    )


def test_trace_steps_carries_the_decision_and_never_the_brief():
    """The brief holds the customer's profile — family size, area, budget — and
    this column is written on every turn."""
    from app.orchestrator.run import StepResult, TurnResult, trace_steps

    result = TurnResult(steps=[StepResult(
        brief="a seven-seater for a family in Deira under 130k",
        agent="sales", status="ok", layer="embedding", score=0.81,
        margin=0.2, latency_ms=4176,
    )])
    rows = trace_steps(result)
    assert rows[0]["agent"] == "sales"
    assert rows[0]["latency_ms"] == 4176
    blob = repr(rows)
    for leaked in ("seven-seater", "Deira", "130k"):
        assert leaked not in blob
