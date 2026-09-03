"""Speculative brief selection (2026-09-02): the judge overlaps Aisha's typing.

The emit_briefs input streams over ~1-2s after her opening line, and the
selection (embedding fast lane, ~1.1s Haiku judge on the rest) used to start
only after the whole generation finished. Completed brief strings are now
offered to a small pool the moment their closing quote arrives; plan_turn
collects the answers with a bounded wait and falls back to selecting in-line
on any miss. Selection only — no events, no writes — so a discarded
generation wastes at most one Haiku read.

Offline: selections are stubbed; nothing reaches a model or the network.
"""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from app.orchestrator import run, speculate


@pytest.fixture(autouse=True)
def _clean_registry():
    speculate.reset()
    yield
    speculate.reset()


# --------------------------------------------------------------------------- #
# Extracting completed briefs from a streaming input
# --------------------------------------------------------------------------- #
def test_a_complete_brief_is_read_out_of_a_partial_input():
    assert speculate.completed_briefs('{"briefs": ["what is the warr') == []
    assert speculate.completed_briefs('{"briefs": ["what is the warranty?"') == [
        "what is the warranty?"
    ]


def test_several_briefs_arrive_one_by_one():
    partial = '{"briefs": ["first question", "second one", "still typi'
    assert speculate.completed_briefs(partial) == ["first question", "second one"]


def test_escapes_are_decoded_and_objects_are_left_alone():
    partial = '{"briefs": ["say \\"hi\\" to them", {"brief": "object form", "depends_on": [0]}, "third"'
    out = speculate.completed_briefs(partial)
    assert 'say "hi" to them' in out
    assert "third" in out
    assert "object form" not in out, "object entries take the old serial path"


def test_no_briefs_key_means_nothing():
    assert speculate.completed_briefs('{"other": ["x"]}') == []


# --------------------------------------------------------------------------- #
# plan_turn consumes speculative selections
# --------------------------------------------------------------------------- #
def _done(selection) -> Future:
    f: Future = Future()
    f.set_result(selection)
    return f


def test_a_speculative_hit_skips_the_judge(monkeypatch):
    from app.orchestrator import select as select_mod

    def boom(text, judge=None):  # noqa: ARG001
        raise AssertionError("selection ran in-line despite a speculative hit")

    monkeypatch.setattr(select_mod, "select", boom)
    hit = select_mod.Selection(agent="sales", layer="judge", reason="speculated")
    plan, unroutable = run.plan_turn(
        ["what is the warranty?"],
        judge=None,
        preselected={"what is the warranty?": _done(hit)},
    )
    assert not unroutable and plan.steps[0].agent == "sales"


def test_a_dead_future_falls_back_to_selecting_in_line(monkeypatch):
    from app.orchestrator import select as select_mod

    calls = []

    def fallback(text, judge=None):  # noqa: ARG001
        calls.append(text)
        return select_mod.Selection(agent="sales", layer="fast_lane", reason="ok")

    monkeypatch.setattr(select_mod, "select", fallback)
    dead: Future = Future()
    dead.set_exception(RuntimeError("judge died mid-speculation"))
    plan, unroutable = run.plan_turn(
        ["what is the warranty?"],
        judge=None,
        preselected={"what is the warranty?": dead},
    )
    assert calls == ["what is the warranty?"]
    assert plan.steps[0].agent == "sales"


def test_take_pops_so_a_discarded_generation_cannot_leak():
    f: Future = Future()
    f.set_result(object())
    with speculate._lock:
        speculate._store["s1"] = {"q": f}
    assert speculate.take("s1") == {"q": f}
    assert speculate.take("s1") == {}


# --------------------------------------------------------------------------- #
# The wiring
# --------------------------------------------------------------------------- #
def test_the_stream_offers_and_the_orchestrator_takes():
    import inspect

    from app.agent import graph

    src = inspect.getsource(graph)
    assert '_speculate.offer(session_id, "".join(spec_buf))' in src
    assert "preselected=_speculate_take(" in src
    assert "_speculate_take(\n                    session_id, first_generation=" in src
