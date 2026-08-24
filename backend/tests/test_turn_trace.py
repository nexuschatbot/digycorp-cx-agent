"""turn_trace: the router's flight recorder (doc §9).

Two properties matter more than the schema. First, the NULLs are the
measurement — a pre-filter turn genuinely has no confidence, and storing a
placeholder there would destroy the "what did the model actually cost" statistic.
Second, a trace failure must never take a turn down; telemetry is not worth a
conversation.
"""

from __future__ import annotations

from app.agent.router.policy import RouteDecision, Routing, apply
from app.services import turn_trace


def test_a_rules_turn_stores_no_confidence(db):
    routing = Routing("bookingmanagement", "pre-filter", layer="prefilter")
    row_id = turn_trace.record(
        db, trace_id="t1", session_id="s1", routing=routing,
        user_message="cancel my booking",
    )
    assert row_id is not None

    from app.models import TurnTrace
    row = db.get(TurnTrace, row_id)
    assert row.routed_by == "prefilter"
    assert row.routed_to == "bookingmanagement"
    # The point: absent, not zero, not one.
    assert row.router_confidence is None
    assert row.router_margin is None


def test_a_classifier_turn_stores_the_numbers(db):
    decision = RouteDecision("testdrive", 0.96, "bookingmanagement", 0.9)
    routing = apply(decision, None)
    row_id = turn_trace.record(
        db, trace_id="t2", session_id="s1", routing=routing,
        user_message="what SUVs do you have?", router_latency_ms=340,
    )
    from app.models import TurnTrace
    row = db.get(TurnTrace, row_id)
    assert row.routed_by == "classifier"
    assert row.router_confidence == 0.96
    assert row.router_margin == 0.9
    assert row.router_second_best == "bookingmanagement"
    assert row.router_latency_ms == 340


def test_a_switch_is_flagged(db):
    decision = RouteDecision("bookingmanagement", 0.95, "testdrive", 0.6)
    routing = apply(decision, "testdrive")
    row_id = turn_trace.record(
        db, trace_id="t3", session_id="s1", routing=routing,
        previous_agent="testdrive",
    )
    from app.models import TurnTrace
    assert db.get(TurnTrace, row_id).switched is True


def test_staying_is_not_a_switch(db):
    decision = RouteDecision("testdrive", 0.95, "fallback", 0.9)
    routing = apply(decision, "testdrive")
    row_id = turn_trace.record(
        db, trace_id="t4", session_id="s1", routing=routing,
        previous_agent="testdrive",
    )
    from app.models import TurnTrace
    assert db.get(TurnTrace, row_id).switched is False


def test_a_broken_write_never_raises(db):
    """Telemetry is not worth a conversation."""
    assert turn_trace.record(
        db, trace_id="t5", session_id="s1",
        tool_calls=object(),          # not JSON-serialisable
    ) is None


def test_misroute_candidates_surfaces_switches_and_clarifies(db):
    turn_trace.record(db, trace_id="a", session_id="s",
                      routing=apply(RouteDecision("testdrive", 0.95, "fallback", 0.9), None))
    turn_trace.record(db, trace_id="b", session_id="s", previous_agent="testdrive",
                      routing=apply(RouteDecision("bookingmanagement", 0.86, "testdrive", 0.04),
                                    "testdrive"))
    refs = {r.trace_id for r in turn_trace.misroute_candidates(db)}
    assert "b" in refs      # thin margin -> withheld switch -> clarify
