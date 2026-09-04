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
        graph, "_known_entities", lambda state, cfg=None: set()
    )
    state = {
        "messages": [_assistant(_tool_use("get_showrooms", "a"), _briefs("x", tid="b"))],
        "session_facts": {},
    }
    out = graph._orchestrator_node(state, _config())
    ids = {b["tool_use_id"] for b in out["messages"][0]["content"]}
    assert ids == {"a", "b"}


def test_the_car_the_customer_HELD_beats_the_car_search_led_with(db):
    """FOUND LIVE, 2026-08-31, and it cost a booking.

    Sales led with the X70 FL Plus, so _harvest_entities wrote that vehicle_id
    into session_facts — "the first row of a ranked list" is what it recommended.
    The customer then chose the cheaper X70 FL and held a slot for it. Nothing
    corrected the note. At booking time the specialist was handed the stale car
    as established fact beside a brief naming the real one, could not tell which
    was true, and proposed nothing — five times. Aisha escalated to the helpline
    for a booking that had everything it needed.

    tool_node has always folded facts_for_slot on a lock; the multi-agent write
    path walked round it. The held slot is the customer's CHOICE and outranks
    anything a search returned."""
    from app.models import LocationType, Slot, SlotStatus
    from datetime import datetime, timedelta
    from app.orchestrator import run as _run

    start = datetime(2026, 9, 3, 13, 0)
    slot = Slot(location_type=LocationType.showroom, showroom_id=1, vehicle_id=8,
                start=start, end=start + timedelta(minutes=90),
                status=SlotStatus.open)
    db.add(slot)
    db.commit()

    step = _run.StepResult(
        brief="hold it", agent="testdrive", status="ok",
        proposals=[{"action": "hold_slot", "args": {"slot_id": slot.id},
                    "agent": "testdrive"}],
    )
    rows, facts = graph._execute_proposals(
        _run.TurnResult(steps=[step]),
        # the stale note: sales led with vehicle 7
        {"session_facts": {"vehicle_id": 7}, "customer_id": None},
        {"db": db, "session_id": "s1"},
    )
    assert [r["status"] for r in rows] == ["executed"], rows
    assert facts["vehicle_id"] == 8, "the held slot's car must win, not the search's"


# --------------------------------------------------------------------------- #
# The cascade ceiling (2026-08-31)
# --------------------------------------------------------------------------- #
def test_a_turn_may_go_back_to_the_specialists_only_so_many_times(monkeypatch):
    """MEASURED, on a 15-turn conversation against a clean db. One question
    about Islamic finance and the on-road price ran THREE cascades back to back
    and took 33 seconds; a consent chip ran two and took 22. Every model call
    costs ~2.2s before it writes a word, so a re-briefing spiral IS the latency.

    Her prompt already forbids it in those words and she does it anyway — the
    same lesson sanitise.py records about rule 17."""
    monkeypatch.setattr("app.config.MAX_CASCADES_PER_TURN", 2)
    state = {
        "messages": [_assistant(_briefs("and the on-road price?", tid="b9"))],
        "session_facts": {},
        "cascades": 2,
    }
    out = graph._orchestrator_node(state, _config())
    body = _result_for(out, "b9")

    # Refused BEFORE dispatch — no specialist ran, so the round cost nothing
    # but the generation that asked for it.
    assert body["findings"] == [] and body["nothing_worked"] is False
    assert "answer the customer from what you have" in body["detail"].lower()


def test_the_ceiling_counts_a_failed_cascade_too(monkeypatch):
    """A turn that cannot reach the specialists must not be free to keep
    trying — that is the spiral in its other costume."""
    monkeypatch.setattr("app.config.MAX_CASCADES_PER_TURN", 5)
    state = {"messages": [_assistant(_briefs("x", tid="b1"))], "session_facts": {},
             "cascades": 0}
    out = graph._orchestrator_node(state, _config())
    assert out["cascades"] == 1


def test_the_ceiling_can_be_switched_off(monkeypatch):
    """Whether this costs answer quality is an open question a re-run settles,
    so 0 disables it without a deploy."""
    monkeypatch.setattr("app.config.MAX_CASCADES_PER_TURN", 0)
    state = {"messages": [_assistant(_briefs("x", tid="b1"))], "session_facts": {},
             "cascades": 99}
    out = graph._orchestrator_node(state, _config())
    # It RAN (and found nothing, with no db) rather than being refused.
    assert _result_for(out, "b1")["nothing_worked"] is True


def test_the_first_cascade_of_a_turn_is_never_refused(monkeypatch):
    monkeypatch.setattr("app.config.MAX_CASCADES_PER_TURN", 2)
    state = {"messages": [_assistant(_briefs("x", tid="b1"))], "session_facts": {},
             "cascades": 0}
    out = graph._orchestrator_node(state, _config())
    assert "answer the customer" not in json.dumps(out["messages"][0]["content"]).lower()


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
    assert graph._known_entities({"session_facts": {"vehicle_id": 7}}) == {"vehicle_id": 7}


def test_unrelated_facts_are_not_treated_as_entities():
    known = graph._known_entities({"session_facts": {"name": "Ahmed", "mobile": "050..."}})
    assert known == {}


def test_a_booking_the_anchor_already_names_counts_as_known(db, monkeypatch):
    """Found live, 2026-08-31. `_agent_node` reads the customer's bookings from
    the DB every generation and renders them into the prompt, so Aisha opened
    the turn with "you have a booking, TD-2026-7QHB" — and the orchestrator then
    blocked her TestDrive brief for want of a booking_ref, because only
    tool_node and _fold_cascade_facts write session_facts and neither had run.
    A whole cascade died on a fact that was already on screen."""
    monkeypatch.setattr(
        "app.services.booking.for_customer",
        lambda db_, customer_id=None: [{"booking_ref": "TD-2026-7QHB"}],
    )
    known = graph._known_entities(
        {"session_facts": {}, "customer_id": 6},
        {"db": db, "session_id": "s1"},
    )
    assert "booking_ref" in known


def test_a_known_entity_carries_its_value_to_the_specialist():
    """Found live, 2026-08-31. Names alone unblocked Finance and then handed it
    an empty context: asked for the instalment on a trim it had never been told,
    it called no tool at all. _fold_cascade_facts fixed the ordering half; this
    is the half that was missing."""
    known = graph._known_entities({"session_facts": {"variant_id": 12}})
    assert known == {"variant_id": 12}, "the value must travel, not just the name"


def test_no_db_leaves_the_ordering_exactly_as_it_was():
    """The read is an anchor, not a dependency: without a session it degrades to
    session_facts alone rather than failing the turn."""
    assert graph._known_entities({"session_facts": {"vehicle_id": 7}}, {}) == {"vehicle_id": 7}


def test_a_broken_read_never_costs_the_turn(db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db is gone")

    monkeypatch.setattr("app.services.booking.for_customer", _boom)
    known = graph._known_entities(
        {"session_facts": {"vehicle_id": 7}, "customer_id": 6},
        {"db": db, "session_id": "s1"},
    )
    assert known == {"vehicle_id": 7}


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
    assert graph._execute_proposals(_run.TurnResult(steps=[_step_with([])]), {}, {}) == ([], {})


def test_a_proposal_reaches_write_execute_all(monkeypatch):
    from app.orchestrator import write

    seen = {}

    def _fake(db, sid, proposals, *, customer_id=None, allow=None, allow_for=None,
              next_step_for=None):
        seen["proposals"] = proposals
        seen["allow"] = allow
        seen["allow_for"] = allow_for
        seen["next_step_for"] = next_step_for
        return [write.WriteResult(action="create_booking", status="executed",
                                  result={"booking_ref": "TD-2026-0001"})]

    monkeypatch.setattr(write, "execute_all", _fake)
    _patch_state(monkeypatch, {"create_booking"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)

    result = _run.TurnResult(steps=[
        _step_with([{"action": "create_booking", "args": {"slot_id": 5}, "agent": "testdrive"}])
    ])
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    out, _ = graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})

    assert seen["allow"] == {"create_booking"}
    # The gate is now re-derived per proposal, not just snapshotted.
    assert callable(seen["allow_for"])
    # AND IT CAN EXPLAIN ITSELF. A refusal that named only the allowed tools
    # left Aisha with nothing to act on and cost a live booking (2026-09-03);
    # asserted on the WIRING, because write.execute_all cannot call a hook the
    # graph never hands it.
    assert callable(seen["next_step_for"])
    assert seen["next_step_for"]() == "confirm and create the booking"
    assert seen["proposals"][0].action == "create_booking"
    assert out == [{"action": "create_booking", "status": "executed",
                    "detail": "", "booking_ref": "TD-2026-0001"}]


def test_a_hold_is_ordered_before_the_booking_it_enables(monkeypatch):
    """THE 2026-09-02 BOOKING BUG, ordering half. A sub-agent sometimes proposes
    create_booking before the hold_slot it depends on; execute_all stops at the
    first refusal, so the hold beside it never ran. _execute_proposals now sends
    holds to the front."""
    from app.orchestrator import write

    seen = {}

    def _fake(db, sid, proposals, *, customer_id=None, allow=None, allow_for=None,
              next_step_for=None):
        seen["order"] = [p.action for p in proposals]
        return [write.WriteResult(action=p.action, status="executed") for p in proposals]

    monkeypatch.setattr(write, "execute_all", _fake)
    _patch_state(monkeypatch, {"create_booking", "hold_slot"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)

    result = _run.TurnResult(steps=[_step_with([
        {"action": "create_booking", "args": {"slot_id": 5}, "agent": "testdrive"},
        {"action": "hold_slot", "args": {"slot_id": 5}, "agent": "testdrive"},
    ])])
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})

    assert seen["order"] == ["hold_slot", "create_booking"]


def test_the_reference_surfaces_from_the_nested_booking_result(monkeypatch):
    """The REAL create_booking result nests everything Aisha may say under
    "booking" ({"booking": _booking_dict(...), "created": True}). Reading only
    the top level meant an EXECUTED booking reached her with no reference —
    she told the customer it had not come through while the write had in fact
    landed (found live, 2026-08-31)."""
    from app.orchestrator import write

    def _fake(db, sid, proposals, *, customer_id=None, allow=None, allow_for=None,
              next_step_for=None):
        return [write.WriteResult(action="create_booking", status="executed",
                                  result={"created": True, "booking": {
                                      "booking_ref": "TD-2026-0002",
                                      "slot_id": 814,
                                      "start_human": "Monday, 31 August, 1:00 pm",
                                      "start_iso": "2026-08-31T13:00:00",
                                      "showroom_name": "Jetour Al Quoz",
                                      "customer_mobile": "+971500000000",
                                  }})]

    monkeypatch.setattr(write, "execute_all", _fake)
    _patch_state(monkeypatch, {"create_booking"})
    events = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: (lambda p: events.append(p)))

    result = _run.TurnResult(steps=[
        _step_with([{"action": "create_booking", "args": {"slot_id": 814}, "agent": "testdrive"}])
    ])
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    out, _ = graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})

    assert out[0]["booking_ref"] == "TD-2026-0002"
    assert out[0]["start_human"] == "Monday, 31 August, 1:00 pm"
    assert out[0]["showroom_name"] == "Jetour Al Quoz"
    # Only what she may read back — never the write's internals.
    assert "customer_mobile" not in out[0]
    # The UI card event fires from the nested ref too.
    assert {"type": "booking", "booking_ref": "TD-2026-0002"} in events


def test_the_state_machine_gate_is_passed_through(monkeypatch):
    """write.execute refuses an action the machine does not allow RIGHT NOW."""
    _patch_state(monkeypatch, {"hold_slot", "lock_slot"})
    monkeypatch.setattr(graph, "_safe_writer", lambda: None)
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    result = _run.TurnResult(steps=[_step_with([{"action": "create_booking", "args": {}}])])
    out, _ = graph._execute_proposals(result, {}, {"db": db, "session_id": "s1"})
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
    out, _ = graph._execute_proposals(
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
    monkeypatch.setattr(graph, "_execute_proposals", lambda *a, **k: ([], {}))
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {}}
    out = graph._orchestrator_node(state, _config(db=SimpleNamespace()))
    assert out["session_facts"]["vehicle_id"] == 7


def test_the_next_turn_can_see_it():
    """_known_entities is what order.resolve consults; close the loop."""
    facts = graph._fold_cascade_facts({"session_facts": {}}, _result_with({"vehicle_id": 7}))
    assert graph._known_entities({"session_facts": facts}) == {"vehicle_id": 7}


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


# --------------------------------------------------------------------------- #
# The trace records what RAN, not what was drawn (2026-09-01)
#
# `tool_calls` was empty on all 110 rows on the test VM — turns that provably
# locked slots and created bookings included — because the runner derived it
# from card_rows, which holds only the calls that drew a card. Diagnosing
# "why did the agent say it has no data" then needed SSH archaeology instead
# of one SQL query, which is the failure the flight recorder exists to
# prevent.
# --------------------------------------------------------------------------- #
def test_the_runner_records_every_tool_call_not_just_the_cards():
    import inspect

    from app.agent import runner

    src = inspect.getsource(runner)
    assert "tools_called.append" in src
    assert "tool_calls=tools_called" in src, (
        "derived from card_rows again is the defect this replaces"
    )


# --------------------------------------------------------------------------- #
# Prewarm warms only what the live path can read (2026-09-01)
#
# With MULTI_AGENT_ENABLED on, every chat call sends aisha_schemas() — yet
# prewarm unconditionally wrote the `full` and journey prefixes beside the
# multi-agent pair: three 1h-TTL cache writes per boot (~83k tokens, 2x rate)
# for keys no live turn could read, on a VM that restarted 14 times in a day.
# And the third LIVE multi-agent prefix — agent=None, the un-routed first turn
# — was served daily and never warmed at all.
# --------------------------------------------------------------------------- #
def _warm_labels(monkeypatch, multi_agent: bool) -> list[str]:
    from app.orchestrator import turn as turn_module

    monkeypatch.setattr(
        turn_module, "enabled", lambda *a, **k: multi_agent
    )
    from app.agent import graph

    return [label for label, _, _ in graph._prefixes_to_warm()]


def test_prewarm_on_the_multiagent_path_warms_only_readable_prefixes(monkeypatch):
    labels = _warm_labels(monkeypatch, multi_agent=True)
    assert "aisha:unrouted" in labels, "the un-routed first turn is live and must be warm"
    # ONE Aisha prefix since 2026-09-04: the journey no longer changes her body
    # on this path, so warming a per-journey label would write the same key
    # again and pay for it (one boot on the VM measured ~$0.55-0.90 of writes).
    assert "aisha:testdrive" not in labels and "aisha:bookingmanagement" not in labels
    assert {"subagent:sales", "subagent:testdrive", "subagent:finance"} <= set(labels)

    for unreadable in ("full", "testdrive", "bookingmanagement"):
        assert unreadable not in labels, (
            f"{unreadable!r} is a single-agent prefix no multi-agent call can read"
        )


def test_prewarm_on_the_single_agent_path_warms_no_specialists(monkeypatch):
    labels = _warm_labels(monkeypatch, multi_agent=False)
    assert labels[0] == "full"
    assert not any(label.startswith(("aisha:", "subagent:")) for label in labels), (
        "specialist prefixes are unreadable while the fan-out is off"
    )


# --------------------------------------------------------------------------- #
# Token usage reaches turn_trace (2026-09-01)
#
# tokens_in / tokens_out / cache_hits existed since T3.5 and every row was
# NULL: the numbers lived only in the `agent usage` journald line, so a token
# audit took ssh archaeology instead of one SQL query.
# --------------------------------------------------------------------------- #
def test_usage_is_emitted_on_the_stream_and_recorded_per_turn():
    import inspect

    from app.agent import graph, runner

    src = inspect.getsource(graph._log_usage)
    assert '"type": "usage"' in src

    rsrc = inspect.getsource(runner)
    assert 'kind == "usage"' in rsrc
    assert 'tokens_in=' in rsrc and 'tokens_out=' in rsrc and 'cache_hits=' in rsrc, (
        "accumulated but not passed to record() is the defect this replaces"
    )


# --------------------------------------------------------------------------- #
# cascade_start — the customer is told the specialists are running (2026-09-04)
# --------------------------------------------------------------------------- #
def test_the_node_announces_the_cascade_before_running_it(monkeypatch):
    """Measured on the test VM: 24 of 33 cascades began with no opening line,
    so the screen showed an empty bubble for the whole 10-30s wait. The node
    is the one place that KNOWS a cascade is starting; it says so on the
    custom stream BEFORE turn.execute, and carries a count, never a brief."""
    from app.orchestrator import turn

    seen: list[dict] = []
    order: list[str] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    monkeypatch.setattr(graph, "_known_entities", lambda state, cfg=None: set())

    def _fake(*a, **k):
        order.append("execute")
        return _run.TurnResult(steps=[])

    monkeypatch.setattr(turn, "execute", _fake)
    state = {"messages": [_assistant(_briefs("what is the warranty", tid="b"))],
             "session_facts": {}}
    graph._orchestrator_node(state, _config())

    starts = [c for c in seen if c.get("type") == "cascade_start"]
    assert starts == [{"type": "cascade_start", "briefs": 1}]
    assert "warranty" not in json.dumps(starts), "a brief must never ride on it"
    # Emitted before the specialists run, or it is not a "starting" signal.
    assert seen.index(starts[0]) == 0 and order == ["execute"]


def test_a_refused_cascade_announces_nothing(monkeypatch):
    """The ceiling refuses before anything is dispatched — nothing is starting."""
    from app import config as _config_mod

    seen: list[dict] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    monkeypatch.setattr(_config_mod, "MAX_CASCADES_PER_TURN", 1)
    state = {"messages": [_assistant(_briefs("x", tid="b"))], "session_facts": {},
             "cascades": 1}
    graph._orchestrator_node(state, _config())
    assert not [c for c in seen if c.get("type") == "cascade_start"]


def test_the_runner_forwards_cascade_start_as_an_sse_event():
    """runner.run_turn is the only reader of the custom stream; a chunk it does
    not forward is a chunk the UI never sees."""
    import inspect

    from app.agent import runner

    src = inspect.getsource(runner.run_turn)
    assert 'kind == "cascade_start"' in src
    assert 'on_event("cascade_start"' in src


# --------------------------------------------------------------------------- #
# Fixes from the 24-turn browser run (2026-09-04)
# --------------------------------------------------------------------------- #
def test_the_journey_never_changes_aishas_body_on_the_multi_agent_path():
    """"Can we move the test drive" flipped the legacy router to
    bookingmanagement and, with it, Aisha's whole stable prefix: one live turn
    wrote 36,478 tokens with cache_read=0. On this path she holds no journey
    tools, so the journey is a record and nothing more."""
    from app.agent.prompt import system_prompt

    unrouted = system_prompt(multi_agent=True)
    assert system_prompt(agent="testdrive", multi_agent=True) == unrouted
    assert system_prompt(agent="bookingmanagement", multi_agent=True) == unrouted
    # The single-agent path still binds a journey body, exactly as before.
    assert system_prompt(agent="bookingmanagement") != system_prompt(agent="testdrive")


def test_a_trim_drops_enough_to_be_rare():
    """Every jump re-writes the whole kept window, so the price of trimming is
    (jumps x TRIM_LOW). The ceiling is high enough that a jump drops several
    dozen messages, not sixteen — four rebuilds in a 24-turn browser run."""
    assert graph.TRIM_KEEP - graph.TRIM_LOW >= 48


def _writes_result(action: str, ref: str):
    from app.orchestrator import write

    return write.WriteResult(action=action, status="executed",
                             result={"booking": {"booking_ref": ref, "status": "cancelled"}})


def test_a_cancellation_greys_the_card_instead_of_drawing_one(monkeypatch):
    """The `booking` event makes the UI fetch and redraw the booking, and a
    cancelled booking is not returned — so the fallback pill read "Booked
    TD-…" directly under "your booking has been cancelled". A cancel is a
    tool_event, which the UI supersedes the old card on, like tool_node's."""
    from app.orchestrator import turn, write

    seen: list[dict] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    monkeypatch.setattr(turn, "execute", lambda *a, **k: _run.TurnResult(
        steps=[_step_with([{"action": "cancel_booking", "args": {"ref": "TD-2026-CMGG"},
                            "agent": "testdrive"}])]))
    monkeypatch.setattr(write, "execute_all",
                        lambda *a, **k: [_writes_result("cancel_booking", "TD-2026-CMGG")])
    _patch_state(monkeypatch, {"cancel_booking"})
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    state = {"messages": [_assistant(_briefs("cancel it", tid="b"))], "session_facts": {}}
    graph._orchestrator_node(state, {"configurable": {"db": db, "session_id": "s1",
                                                      "deadline_monotonic": None}})
    kinds = [(c.get("type"), c.get("tool")) for c in seen]
    assert ("tool_event", "cancel_booking") in kinds
    assert not [c for c in seen if c.get("type") == "booking"]
    event = next(c for c in seen if c.get("tool") == "cancel_booking")
    assert event["input"] == {"ref": "TD-2026-CMGG"}
    assert event["output"]["booking"]["booking_ref"] == "TD-2026-CMGG"


def test_a_created_booking_still_draws_its_card(monkeypatch):
    from app.orchestrator import turn, write

    seen: list[dict] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    monkeypatch.setattr(turn, "execute", lambda *a, **k: _run.TurnResult(
        steps=[_step_with([{"action": "create_booking", "args": {}, "agent": "testdrive"}])]))
    monkeypatch.setattr(write, "execute_all",
                        lambda *a, **k: [_writes_result("create_booking", "TD-2026-0042")])
    _patch_state(monkeypatch, {"create_booking"})
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    state = {"messages": [_assistant(_briefs("book it", tid="b"))], "session_facts": {}}
    graph._orchestrator_node(state, {"configurable": {"db": db, "session_id": "s1",
                                                      "deadline_monotonic": None}})
    assert {"type": "booking", "booking_ref": "TD-2026-0042"} in seen


def test_what_aisha_already_said_this_turn_rides_with_the_findings(monkeypatch):
    """She asked "May we send you WhatsApp reminders?" before the hold cascade
    and again after it, with the rule against it 10k tokens above. The sealed
    sentences now sit in the tool_result she composes from."""
    from app.orchestrator import turn

    monkeypatch.setattr(graph, "_safe_writer", lambda: None)
    monkeypatch.setattr(graph, "_known_entities", lambda state, cfg=None: set())
    monkeypatch.setattr(turn, "execute", lambda *a, **k: _run.TurnResult(steps=[]))
    state = {
        "messages": [
            {"role": "user", "content": "yes go ahead"},
            _assistant({"type": "text", "text": "Just remember your licence. May we send you WhatsApp reminders?"},
                       _tool_use("acknowledge_licence_reminder", "t1")),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
            _assistant({"type": "text", "text": "Holding that slot for you now."},
                       _briefs("lock slot 516", tid="b")),
        ],
        "session_facts": {},
    }
    body = _result_for(graph._orchestrator_node(state, _config()), "b")
    assert body["already_said"] == [
        "Just remember your licence. May we send you WhatsApp reminders?",
        "Holding that slot for you now.",
    ]
    assert "Do not write them again" in body["already_said_note"]
    # Only THIS turn: the previous turn's words are not "already said" now.
    earlier = {"messages": [
        _assistant({"type": "text", "text": "from last turn"}),
        {"role": "user", "content": "yes"},
        _assistant(_briefs("x", tid="b")),
    ], "session_facts": {}}
    assert "already_said" not in _result_for(graph._orchestrator_node(earlier, _config()), "b")


def test_a_car_described_from_the_prefetch_gets_its_card(db, monkeypatch):
    """"What 7-seaters do you have" was answered from the catalogue passages
    in one generation and the customer got a paragraph with no card. The
    passage carried the car's id; the card is the get_vehicle_details result
    a tool call would have drawn, emitted the same way."""
    from app.retrieval import vehicle_mentions

    vehicle_mentions.reset()
    seen: list[dict] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    from sqlalchemy import select

    from app.models import VehicleModel

    car = db.scalars(select(VehicleModel).where(VehicleModel.seats_max >= 7)).first()
    vehicles = [{"vehicle_id": car.id, "name": car.name}]
    assistant = _assistant({"type": "text", "text": f"The one to look at is the {car.name}, a true seven-seater."})
    drawn = graph._draw_prefetched_cards(assistant, vehicles, {"db": db}, "s1")
    assert drawn == 1
    event = seen[0]
    assert event["type"] == "tool_event" and event["tool"] == "get_vehicle_details"
    assert event["output"]["vehicle"]["vehicle_id"] == car.id


def test_no_card_when_the_generation_drew_its_own_or_delegated(db, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(graph, "_safe_writer", lambda: seen.append)
    vehicles = [{"vehicle_id": 1, "name": "Jetour X70 FL Plus"}]
    delegated = _assistant({"type": "text", "text": "Let me check the Jetour X70 FL Plus."},
                           _briefs("x", tid="b"))
    own_lookup = _assistant({"type": "text", "text": "The Jetour X70 FL Plus."},
                            _tool_use("get_vehicle_details", "t1"))
    unnamed = _assistant({"type": "text", "text": "We have several family SUVs."})
    for a in (delegated, own_lookup, unnamed):
        assert graph._draw_prefetched_cards(a, vehicles, {"db": db}, "s1") == 0
    assert seen == []
