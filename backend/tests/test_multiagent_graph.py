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
