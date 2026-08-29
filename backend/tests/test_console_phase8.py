"""The Handoffs queue and the Routing view (Phase 8).

Both are READS of data the system was already recording and never showed. The
tests below check the two things that make them useful rather than decorative:
the ORDER each is sorted in, and that an empty multi-agent record reads as "not
applicable" rather than "broken".

Offline: the seeded db, no HTTP, no vendor.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import Actor, AuditEvent, AuditType, Customer, TurnTrace
from app.services import console


@pytest.fixture()
def handoffs(db):
    """Three escalations, deliberately inserted newest-first so a naive query
    would return them in the wrong order."""
    customer = Customer(name_en="Ahmed", mobile="+971500000111")
    db.add(customer)
    db.flush()
    now = datetime.now()
    for hours, reason, topic in (
        (1, "price", "discount on the T2"),
        (30, "finance", "instalments for the G700"),
        (5, "complaint", None),
    ):
        db.add(AuditEvent(
            ts=now - timedelta(hours=hours),
            session=f"s-{hours}",
            actor=Actor.agent,
            type=AuditType.escalation,
            payload={"reason": reason, "topic": topic, "customer_id": customer.id},
        ))
    db.flush()
    return customer


# --------------------------------------------------------------------------- #
# T8.1 — the queue
# --------------------------------------------------------------------------- #
def test_the_queue_is_oldest_first(db, handoffs):
    """THE OPPOSITE ORDER TO EVERY OTHER BROWSER HERE, on purpose. Newest-first
    suits an audit log, read to find out what just happened. This is a WORK
    QUEUE: the person waiting longest is the one being let down most, and
    sorting them to the bottom is how a queue starves its oldest entry."""
    rows = console.list_handoffs(db)["rows"]
    stamps = [r["ts"] for r in rows]
    assert stamps == sorted(stamps), "the longest wait must be first"
    assert rows[0]["reason"] == "finance", "the 30-hour-old one leads"


def test_each_entry_carries_who_to_call_and_what_about(db, handoffs):
    """A callback queue without a name and a number is not a callback queue."""
    row = console.list_handoffs(db)["rows"][0]
    assert row["customer_name"] == "Ahmed"
    assert row["customer_mobile"] == "+971500000111"
    # The customer's OWN words, so the operator knows what they are calling
    # about. Never spoken back as policy (rule 13).
    assert row["topic"] == "instalments for the G700"


def test_a_missing_topic_is_shown_as_missing_not_invented(db, handoffs):
    complaint = next(
        r for r in console.list_handoffs(db)["rows"] if r["reason"] == "complaint"
    )
    assert complaint["topic"] is None


def test_the_queue_can_be_filtered_by_reason(db, handoffs):
    out = console.list_handoffs(db, reason="price")
    assert out["total"] == 1 and out["rows"][0]["reason"] == "price"


def test_an_empty_queue_is_not_an_error(db):
    out = console.list_handoffs(db)
    assert out["rows"] == [] and out["total"] == 0


def test_the_queue_does_not_query_per_row(db, handoffs):
    """One query for every customer on the page, not one per row."""
    from sqlalchemy import event

    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "FROM customers" in statement:
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        console.list_handoffs(db)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert len(seen) <= 1, f"{len(seen)} customer queries for one page"


# --------------------------------------------------------------------------- #
# T8.2 — the routing view
# --------------------------------------------------------------------------- #
@pytest.fixture()
def traces(db):
    now = datetime.now()
    db.add(TurnTrace(
        trace_id="t-single", session_id="s1", created_at=now - timedelta(minutes=5),
        user_message="hello", routed_to="testdrive", latency_ms=1200,
        steps=None, prompt_versions={"aisha": "v1"},
    ))
    db.add(TurnTrace(
        trace_id="t-multi", session_id="s2", created_at=now,
        user_message="a 7 seater and book me in", latency_ms=5400,
        steps=[
            {"agent": "sales", "status": "ok", "layer": "fast_lane",
             "score": 0.84, "margin": 0.031, "latency_ms": 900, "detail": None},
            {"agent": "testdrive", "status": "failed", "layer": "judge",
             "score": None, "margin": None, "latency_ms": 20000,
             "detail": "timed out after 20s"},
        ],
        prompt_versions={"aisha": "v1", "sales": "v1"},
    ))
    db.flush()


def test_the_routing_view_is_newest_first(db, traces):
    """The opposite of the handoffs queue, and deliberately so: this is read to
    find out what just happened, not worked through."""
    rows = console.list_routing(db)["rows"]
    assert rows[0]["trace_id"] == "t-multi"


def test_a_single_agent_turn_reports_no_steps_rather_than_a_failure(db, traces):
    """Until the kill switch flips that is EVERY turn. An empty list must read
    as "not applicable", never as "it went wrong"."""
    row = next(r for r in console.list_routing(db)["rows"] if r["trace_id"] == "t-single")
    assert row["steps"] == [] and row["step_count"] == 0
    assert row["failed_steps"] == 0


def test_the_view_counts_how_each_agent_was_chosen(db, traces):
    """The distinction is the whole point: fast_lane cost nothing, judge cost a
    Haiku call."""
    row = next(r for r in console.list_routing(db)["rows"] if r["trace_id"] == "t-multi")
    assert row["fast_lane_steps"] == 1
    assert row["judged_steps"] == 1
    assert row["failed_steps"] == 1


def test_the_view_keeps_the_margin_so_the_threshold_can_be_retuned(db, traces):
    """MIN_MARGIN is 0.02 because 32 SYNTHETIC utterances said so. These rows are
    the only place real briefs report what they scored."""
    row = next(r for r in console.list_routing(db)["rows"] if r["trace_id"] == "t-multi")
    assert row["steps"][0]["margin"] == 0.031


def test_the_view_records_which_prompt_answered(db, traces):
    """Prompts are editable without a deploy, so this is what pins a quality
    change to a prompt change."""
    row = next(r for r in console.list_routing(db)["rows"] if r["trace_id"] == "t-multi")
    assert row["prompt_versions"] == {"aisha": "v1", "sales": "v1"}


def test_the_view_can_be_narrowed_to_multi_agent_turns(db, traces):
    out = console.list_routing(db, only_multi_agent=True)
    assert out["total"] == 1 and out["rows"][0]["trace_id"] == "t-multi"


def test_the_summary_answers_whether_the_fast_lane_earns_its_place(db, traces):
    """THE NUMBER THE SCREEN EXISTS FOR. The calibration predicted ~1/3 of briefs
    settle without a model call, on synthetic utterances. If the real share is
    far lower, the honest move is to delete the fast lane and always judge."""
    summary = console.routing_summary(db)
    assert summary["steps"] == 2
    assert summary["fast_lane"] == 1 and summary["judged"] == 1
    assert summary["fast_lane_share"] == 0.5
    assert summary["by_agent"] == {"sales": 1, "testdrive": 1}


def test_the_summary_is_honest_when_there_is_nothing_to_summarise(db):
    """A share of 0% would read as "the fast lane never fires". None reads as
    "no multi-agent turn has happened yet", which is the truth."""
    summary = console.routing_summary(db)
    assert summary["steps"] == 0
    assert summary["fast_lane_share"] is None
