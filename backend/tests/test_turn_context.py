"""TURN_CONTEXT_IN_MESSAGE (2026-09-02): per-message context behind the history.

THE BUG THIS PINS. The prompt cache is a prefix cache in the order tools ->
system -> messages. Facts, bookings and the pre-fetched passages used to live
in the volatile SYSTEM tail — so anything that changed them (every message,
once RETRIEVAL_FIRST put passages there) sat ahead of the whole conversation
and re-wrote it. Measured on the test VM: 0 of 20 turns in a 38-turn chat read
their history back; 462k tokens re-written at the write rate.

The fix moves that context to a trailing block on the customer's message and
keeps it there, so a later request is byte-for-byte a superset of the earlier
one. These tests assert exactly that property, offline, with a capturing
generate. Nothing here calls a model.
"""

from __future__ import annotations

from app.agent import build_config, build_graph, graph, new_sqlite_checkpointer
from app.agent.prompt import (
    _TURN_CONTEXT_CLOSE,
    _TURN_CONTEXT_OPEN,
    system_prompt_parts,
    turn_context,
)

FACTS = {"name": "Ali", "mobile": "+971509998877"}
PASSAGE = "<<<SOURCE warranty#Petrol models>>>\n10 years or 1,000,000 km\n<<<END SOURCE>>>"


# --------------------------------------------------------------------------- #
# The prompt half: the same prose, rendered in a different place
# --------------------------------------------------------------------------- #
def test_turn_context_renders_the_anchors_verbatim_inside_a_fence():
    block = turn_context(session_facts=FACTS, retrieved_passages=PASSAGE)
    assert block.startswith(_TURN_CONTEXT_OPEN)
    assert block.endswith(_TURN_CONTEXT_CLOSE)
    assert "WHO YOU ARE ALREADY SPEAKING WITH" in block and "Ali" in block
    assert "PRE-FETCHED FOR THIS MESSAGE" in block and "10 years" in block
    # Facts before passages, the order the system tail used.
    assert block.index("WHO YOU ARE") < block.index("PRE-FETCHED")


def test_turn_context_is_empty_when_there_is_nothing_to_say():
    """A fresh customer's message must be sent exactly as it arrived."""
    assert turn_context() == ""
    assert turn_context(session_facts={}, live_bookings=[], held_slot=None) == ""


def test_context_in_message_strips_the_moved_anchors_from_the_system_tail():
    stable, volatile = system_prompt_parts(
        multi_agent=True, session_facts=FACTS, retrieved_passages=PASSAGE,
        context_in_message=True,
    )
    bare_stable, bare_volatile = system_prompt_parts(multi_agent=True)
    assert stable == bare_stable
    assert volatile == bare_volatile  # facts and passages are gone from here
    assert "Ali" not in volatile and "10 years" not in volatile


def test_default_placement_is_unchanged():
    """Every existing caller and test reads the anchors from the system tail;
    without the flag nothing moves."""
    _, volatile = system_prompt_parts(
        multi_agent=True, session_facts=FACTS, retrieved_passages=PASSAGE
    )
    assert "Ali" in volatile and "10 years" in volatile


# --------------------------------------------------------------------------- #
# The message half: attach, never mutate; the window start holds still
# --------------------------------------------------------------------------- #
def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def test_attach_context_appends_a_trailing_block_and_copies():
    msgs = [_user("hi"), _assistant("hello"), _user("colours?")]
    out = graph._attach_context(msgs, {"2": "CTX"})
    assert out[2]["content"] == [
        {"type": "text", "text": "colours?"},
        {"type": "text", "text": "CTX"},
    ]
    assert out[0] is msgs[0] and out[1] is msgs[1]
    assert msgs[2]["content"] == "colours?"  # state untouched


def test_attach_context_ignores_keys_that_are_not_turn_starts():
    tool_echo = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}
    msgs = [_user("hi"), _assistant("a"), tool_echo]
    assert graph._attach_context(msgs, {"1": "CTX", "2": "CTX", "9": "CTX", "x": "CTX"}) == msgs
    assert graph._attach_context(msgs, {}) is msgs


def test_window_start_holds_still_until_the_history_outgrows_the_ceiling():
    msgs = []
    for i in range(graph.TRIM_KEEP):
        msgs.append(_user(f"q{i}") if i % 2 == 0 else _assistant(f"a{i}"))
    assert graph._window_start(msgs, 0) == 0
    # One over the ceiling: jump to the last TRIM_LOW, on a turn boundary.
    msgs.append(_user("over"))
    start = graph._window_start(msgs, 0)
    assert start > 0
    assert graph._is_turn_start(msgs[start])
    assert len(msgs) - start <= graph.TRIM_LOW + 1
    # ...and then it does not move for a while, however the history grows.
    for i in range(10):
        msgs.append(_assistant("a") if i % 2 == 0 else _user("q"))
        assert graph._window_start(msgs, start) == start


def test_window_start_never_goes_backwards():
    msgs = [_user("q") if i % 2 == 0 else _assistant("a") for i in range(50)]
    assert graph._window_start(msgs, 30) == 30


# --------------------------------------------------------------------------- #
# The property itself: turn N+1's request is a superset of turn N's
# --------------------------------------------------------------------------- #
class _Capture:
    """A scripted generate that records exactly what the node would send."""

    def __init__(self) -> None:
        self.calls: list[tuple[list, tuple]] = []

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        self.calls.append((messages, system))
        return _assistant(f"reply {len(self.calls)}")


def _turn(g, cfg, text: str, **state):
    return g.invoke({"messages": [_user(text)], "iterations": 0, **state}, cfg)


def test_a_later_request_is_a_byte_superset_of_the_earlier_one(db, monkeypatch):
    monkeypatch.setattr(graph, "TURN_CONTEXT_IN_MESSAGE", True)
    cap = _Capture()
    g = build_graph(new_sqlite_checkpointer())
    cfg = build_config(db, "cache-1", generate=cap)

    _turn(g, cfg, "hello there", session_facts=FACTS)
    _turn(g, cfg, "and what colours are there")
    _turn(g, cfg, "thanks")

    (m1, s1), (m2, s2), (m3, s3) = cap.calls
    assert s1 == s2 == s3, "the system prompt must not move between turns"
    # Every earlier request is a prefix of the later one, message for message.
    assert m2[: len(m1)] == m1
    assert m3[: len(m2)] == m2
    # The facts rode in the message, not the system tail, and were sent again.
    assert "Ali" in m1[0]["content"][-1]["text"]
    assert "Ali" not in "".join(s1)
    assert m3[0] == m1[0]


def test_passages_ride_in_the_message_on_the_multi_agent_path(db, monkeypatch):
    from app.retrieval import index
    from app.retrieval.chunk import Chunk

    monkeypatch.setattr(graph, "TURN_CONTEXT_IN_MESSAGE", True)
    monkeypatch.setattr(graph, "RETRIEVAL_FIRST", True)
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr("app.orchestrator.turn.MULTI_AGENT_ENABLED", True, raising=False)
    chunk = Chunk(text="x", body="10 years or 1,000,000 km", source="warranty",
                  collection="knowledge", heading="Petrol models")
    monkeypatch.setattr(index, "search", lambda q, **k: [(chunk, 0.9)])

    cap = _Capture()
    g = build_graph(new_sqlite_checkpointer())
    cfg = build_config(db, "cache-2", generate=cap)
    _turn(g, cfg, "what is the warranty of the Dashing")
    _turn(g, cfg, "and how long is delivery")

    (m1, s1), (m2, s2) = cap.calls
    assert s1 == s2
    assert "10 years" in m1[0]["content"][-1]["text"]
    assert "10 years" not in "".join(s1)
    assert m2[: len(m1)] == m1


def test_flag_off_leaves_the_request_exactly_as_it_was(db, monkeypatch):
    monkeypatch.setattr(graph, "TURN_CONTEXT_IN_MESSAGE", False)
    cap = _Capture()
    g = build_graph(new_sqlite_checkpointer())
    cfg = build_config(db, "cache-3", generate=cap)
    _turn(g, cfg, "hello there", session_facts=FACTS)
    (m1, s1), = cap.calls
    assert m1[0]["content"] == "hello there"
    assert "Ali" in "".join(s1)


def test_the_gate_expression_is_still_pinned():
    import inspect

    src = inspect.getsource(graph._agent_node)
    assert "RETRIEVAL_FIRST and _multi_agent(cfg) and iterations == 0" in src
    assert "context_in_message=in_message" in src


# --------------------------------------------------------------------------- #
# Neighbours shipped in the same change
# --------------------------------------------------------------------------- #
def test_specialist_client_is_pooled(monkeypatch):
    from app.orchestrator import subagent

    built = []
    monkeypatch.setattr(subagent, "_pooled_client", None)
    monkeypatch.setattr("app.agent.graph._build_client", lambda: built.append(1) or object())
    a = subagent._client()
    b = subagent._client()
    assert a is b and built == [1]


def test_cache_keepalive_stops_on_its_event(monkeypatch):
    calls = []
    monkeypatch.setattr(graph, "prewarm_cache", lambda: calls.append(1))
    stop = graph.start_cache_keepalive(1)
    stop.set()  # the loop is parked on stop.wait(60s); setting it ends it
    assert calls == []
