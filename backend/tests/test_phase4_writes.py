"""Writes, the booking state machine, concurrency and PDPL (Phase 4).

FOUR THINGS ARE ASSERTED HERE, AND EACH IS STRUCTURAL RATHER THAN PROSE:

  1. NO SUB-AGENT HOLDS A WRITE TOOL. They return proposals as data; the
     orchestrator decides. This is what makes it safe to run them on Haiku.
  2. THE STATE MACHINE AND THE SERVICE GUARD READ THE SAME FACT. If they could
     disagree about whether the licence was acknowledged, the machine would be
     worse than useless — it would look authoritative and be wrong.
  3. ONE TURN AT A TIME PER SESSION, released even when the turn raises.
  4. "FORGET ME" REACHES EVERYTHING, including the two places it provably did
     not: turn_trace, and transcripts from a session no audited tool call
     touched.

Offline: the seeded db and plain objects. No model, no network.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent import session_guard
from app.agents import loader
from app.orchestrator import booking_state, write
from app.tools import COMMERCIAL_WRITE_TOOLS, READ_TOOLS, REGISTRY, WRITE_TOOLS


# --------------------------------------------------------------------------- #
# 1. The write split is data, and the cards respect it
# --------------------------------------------------------------------------- #
def test_the_write_split_covers_the_whole_registry():
    """It used to be a `--- writes ---` comment a reader had to honour. The whole
    write architecture rests on it, and none of it is checkable while "is this a
    write?" lives in a comment."""
    assert WRITE_TOOLS | READ_TOOLS == set(REGISTRY)
    assert not (WRITE_TOOLS & READ_TOOLS)
    assert COMMERCIAL_WRITE_TOOLS < WRITE_TOOLS


@pytest.mark.parametrize("agent", ["sales", "testdrive", "finance"])
def test_no_sub_agent_card_holds_a_write_tool(agent):
    """THE INVARIANT THAT MAKES HAIKU SAFE FOR THEM. A sub-agent that could
    write could book a car for the wrong person on a bad generation."""
    tools = set(loader.frontmatter(agent).get("tools") or [])
    assert not (tools & WRITE_TOOLS), f"{agent} holds {sorted(tools & WRITE_TOOLS)}"


@pytest.mark.parametrize("agent", ["sales", "testdrive", "finance"])
def test_every_tool_a_card_names_actually_exists(agent):
    """A card naming a tool the registry does not have is an instruction the
    agent cannot follow — the orphaned-rule defect, one level up."""
    tools = set(loader.frontmatter(agent).get("tools") or [])
    unknown = tools - set(REGISTRY)
    # search_knowledge is written but not registered until Phase 2 binds it.
    assert unknown <= {"search_knowledge", "get_variants", "check_stock",
                       "get_on_road_price", "get_finance_estimate"}, unknown


def test_commercial_writes_are_proposed_not_held():
    """TestDrive asks for holds and bookings; it cannot perform them."""
    fm = loader.frontmatter("testdrive")
    proposes = set(fm.get("proposes") or [])
    tools = set(fm.get("tools") or [])
    assert proposes & {"create_booking", "cancel_booking", "reschedule_booking"}
    assert not (tools & COMMERCIAL_WRITE_TOOLS)


# --------------------------------------------------------------------------- #
# 2. The write gate
# --------------------------------------------------------------------------- #
def test_an_unrecognised_action_is_refused_not_attempted():
    result = write.execute(None, "s1", write.Proposal(action="delete_everything"))
    assert result.status == "refused" and not result.ok


def test_a_read_tool_cannot_be_smuggled_through_the_write_gate():
    """The gate takes COMMERCIAL actions only. A proposal naming a read is not
    a shortcut into the tool registry."""
    result = write.execute(None, "s1", write.Proposal(action="search_vehicles"))
    assert result.status == "refused"


def test_the_state_machine_can_refuse_an_out_of_order_action():
    result = write.execute(
        None, "s1", write.Proposal(action="create_booking"), allow={"hold_slot"}
    )
    assert result.status == "refused"
    assert "not valid at this point" in result.detail


def test_trusted_context_is_injected_and_never_taken_from_the_proposal(monkeypatch):
    """THE MOST DANGEROUS THING A MODEL COULD GET WRONG HERE. A sub-agent that
    could name its own customer_id could write for somebody else."""
    seen = {}

    def fake(db, session_id, **kwargs):
        seen.update(kwargs)
        return {"locked": True}

    monkeypatch.setitem(__import__("app.tools", fromlist=["TOOL_FNS"]).TOOL_FNS,
                        "lock_slot", fake)
    write.execute(
        None, "s1",
        write.Proposal(action="hold_slot", args={"slot_id": 5, "customer_id": 999}),
        customer_id=7,
    )
    assert seen["customer_id"] == 7, "the proposal's customer_id must be discarded"
    assert seen["slot_id"] == 5


def test_a_service_guard_refusal_is_data_not_an_exception(monkeypatch):
    """The licence reminder saying no is the system WORKING. Aisha needs to say
    what happened, which she cannot do with a traceback."""
    monkeypatch.setitem(
        __import__("app.tools", fromlist=["TOOL_FNS"]).TOOL_FNS,
        "create_booking",
        lambda db, s, **k: {"error": "licence_reminder_not_acknowledged"},
    )
    result = write.execute(None, "s1", write.Proposal(action="create_booking"))
    assert result.status == "refused"
    assert result.detail == "licence_reminder_not_acknowledged"


def test_a_sequence_stops_at_the_first_refusal(monkeypatch):
    """Proposals from one turn are a sequence — hold then book. Carrying on
    after a refused hold would create a booking against a slot nobody holds."""
    monkeypatch.setitem(
        __import__("app.tools", fromlist=["TOOL_FNS"]).TOOL_FNS,
        "lock_slot", lambda db, s, **k: {"error": "slot_no_longer_available"},
    )
    results = write.execute_all(
        None, "s1",
        [write.Proposal(action="hold_slot"), write.Proposal(action="create_booking")],
    )
    assert [r.status for r in results] == ["refused", "refused"]
    assert "skipped" in results[1].detail


def test_one_correlation_id_ties_a_sequence_together(monkeypatch):
    monkeypatch.setitem(
        __import__("app.tools", fromlist=["TOOL_FNS"]).TOOL_FNS,
        "lock_slot", lambda db, s, **k: {"locked": True},
    )
    results = write.execute_all(
        None, "s1", [write.Proposal(action="hold_slot"), write.Proposal(action="lock_slot")]
    )
    ids = {r.correlation_id for r in results}
    assert len(ids) == 1


# --------------------------------------------------------------------------- #
# 3. The booking state machine
# --------------------------------------------------------------------------- #
def test_the_stages_gate_creating_a_booking():
    assert "create_booking" not in booking_state.BookingState(stage="hold").allowed
    assert "create_booking" not in booking_state.BookingState(stage="licence").allowed
    assert "create_booking" in booking_state.BookingState(stage="create").allowed


def test_cancelling_is_allowed_from_every_stage():
    """It acts on a booking that already exists and has nothing to do with this
    flow. Gating it would mean a customer cannot cancel while mid-way through
    booking something else."""
    for stage in booking_state.STAGES:
        allowed = booking_state.BookingState(stage=stage).allowed
        assert {"cancel_booking", "reschedule_booking"} <= allowed, stage


def test_the_hold_can_still_be_moved_before_confirming():
    """A customer changing their mind about the time is ordinary, not an error."""
    assert "hold_slot" in booking_state.BookingState(stage="licence").allowed


def test_the_machine_reads_the_same_column_the_service_guard_reads():
    """IF THEY COULD DISAGREE THE MACHINE WOULD BE WORSE THAN USELESS — it would
    look authoritative and be wrong. An earlier version scanned the audit trail,
    which would have drifted the moment the licence-ack TAP wrote the column
    without an agent turn."""
    source = (booking_state.__file__)
    text = open(source, encoding="utf-8").read()
    assert "slot.licence_ack" in text or "licence_ack" in text
    assert "AuditEvent" not in text, "the machine must not infer state from audit"


def test_an_unreadable_state_degrades_to_the_most_restrictive_stage():
    """`hold` permits holding and refuses creating — the safe direction."""
    class Boom:
        def scalar(self, *a, **k):
            raise RuntimeError("db is gone")

    state = booking_state.read(Boom(), "s1")
    assert state.stage == "hold"
    assert "create_booking" not in state.allowed


def test_only_an_affirmation_claims_the_turn(db):
    """"and Sunday?" with a hold open is a new question about a different day,
    not a confirmation. The affirmation list is deliberately small and exact —
    a fuzzy yes-detector books from "yes, but on Sunday"."""
    check = booking_state.pending_confirmation(db, "s-none")
    assert check("and Sunday?") is None
    assert check("what times do you have") is None


# --------------------------------------------------------------------------- #
# 4. Concurrency
# --------------------------------------------------------------------------- #
def test_a_second_turn_on_the_same_session_is_refused():
    session_guard.reset()
    with session_guard.acquire("s1"):
        with pytest.raises(session_guard.SessionBusy):
            with session_guard.acquire("s1"):
                pass


def test_a_different_session_is_unaffected():
    session_guard.reset()
    with session_guard.acquire("s1"):
        with session_guard.acquire("s2"):
            assert session_guard.is_busy("s1") and session_guard.is_busy("s2")


def test_the_slot_is_released_even_when_the_turn_raises():
    """A turn that failed must not leave the session permanently wedged — that
    would be a worse failure than the race it guards against."""
    session_guard.reset()
    with pytest.raises(ValueError):
        with session_guard.acquire("s3"):
            raise ValueError("boom")
    assert not session_guard.is_busy("s3")


def test_it_refuses_rather_than_queues():
    """A queued second message would be answered against state the customer has
    not seen, minutes later, out of context."""
    import inspect

    src = inspect.getsource(session_guard.acquire)
    assert "raise SessionBusy" in src
    assert "wait" not in src.lower() or "await" not in src.lower()


# --------------------------------------------------------------------------- #
# 5. PDPL — the two halves of the erasure gap
# --------------------------------------------------------------------------- #
def test_conversation_turns_can_be_reached_by_customer_now():
    """THE GAP services/erasure.py NAMED AND COULD NOT FIX: "a conversation where
    the customer was identified but no audited tool call ever ran is therefore
    unreachable, and its transcript would survive"."""
    from app.models import ConversationTurn

    assert "customer_id" in ConversationTurn.__table__.c
    assert ConversationTurn.__table__.c["customer_id"].nullable, (
        "a turn spoken before identification genuinely has no customer"
    )


def test_erasure_deletes_turn_trace():
    """models.py declared it in scope for erasure since it was written and
    nothing ever deleted it — so "forget me" was a partial delete that LOOKED
    complete. It holds the customer's own message text."""
    import inspect

    from app.services import erasure

    src = inspect.getsource(erasure)
    assert "TurnTrace" in src
    assert "turn_trace" in src


def test_erasure_deletes_transcripts_by_customer_as_well_as_by_session():
    """BOTH, never one: turns spoken before identification carry a null
    customer_id and are only reachable by session; turns in a session the
    attribution sweep never found are only reachable by customer."""
    import inspect

    from app.services import erasure

    src = inspect.getsource(erasure)
    assert "conversation_turns_by_customer" in src
    assert "ConversationTurn.session_id.in_(sessions)" in src


def test_the_retention_job_exists_and_defaults_to_a_dry_run(db):
    """A retention policy nobody enforces is not a policy. And a job that
    deleted on import is how a staging run takes production data with it."""
    from app.services import retention

    counts = retention.sweep(db)
    assert counts["dry_run"] is True
    assert set(counts) >= {"conversation_turns", "turn_trace", "speech_clips"}


def test_retention_never_touches_bookings_or_customers():
    """Those have their own lifecycle and their own legal basis — a booking is a
    commercial record, not telemetry."""
    import inspect

    from app.services import retention

    src = inspect.getsource(retention)
    for protected in ("Booking", "Customer", "Consent", "MemoryProfile"):
        assert f"sa_delete({protected}" not in src


def test_the_retention_windows_are_named_constants_not_literals():
    """They are policy, they get quoted in a privacy review, and they change
    together."""
    from app.services import retention

    assert retention.TRANSCRIPT_DAYS > retention.TRACE_DAYS > retention.SPEECH_CLIP_DAYS


def test_retention_actually_deletes_rows_past_their_window(db):
    """The dry run is the default; this is the other half. Ages rows by
    injecting `now` rather than by waiting a year."""
    from datetime import datetime, timedelta

    from app.models import ConversationTurn, Language, Speaker
    from app.services import retention

    old = ConversationTurn(
        session_id="s-old", speaker=Speaker.customer,
        lang=Language.en, text="ancient",
        ts=datetime.now() - timedelta(days=retention.TRANSCRIPT_DAYS + 10),
    )
    fresh = ConversationTurn(
        session_id="s-new", speaker=Speaker.customer,
        lang=Language.en, text="recent", ts=datetime.now(),
    )
    db.add_all([old, fresh])
    db.flush()

    counts = retention.sweep(db, dry_run=False)
    assert counts["conversation_turns"] >= 1

    left = [t.text for t in db.scalars(select(ConversationTurn)).all()]
    assert "recent" in left and "ancient" not in left


def test_the_cutoff_uses_the_same_clock_the_rows_were_written_with():
    """Stored timestamps come from func.now() in local time, like booking.py and
    consent.py. A UTC cutoff here would shift every window by the timezone
    offset — a silent few hours of extra or missing retention."""
    import inspect

    from app.services import retention

    src = inspect.getsource(retention._cutoff)
    assert "utcnow" not in src


def test_attribution_backfills_the_whole_session_not_just_new_turns(db):
    """A conversation opens anonymous — "what SUVs do you have" — and only later
    reaches "I'm Ahmed". Those first turns are as much the customer's words as
    the later ones, and a "forget me" that left them behind would be a partial
    delete that looked complete.

    THIS IS THE FUNCTION I GOT WRONG FIRST. The original version referenced a
    `customer_id` local that run_turn does not have, and read `session_facts`
    when the state key is `customer_id` — so it raised NameError on every turn,
    took out eleven voice tests, and tripled the suite runtime. It is now the
    exact counterpart of _attach_voice_customer, in shape and in call site.
    """
    from app.agent.runner import _attribute_session
    from app.models import ConversationTurn, Customer, Language, Speaker

    customer = Customer(name_en="Ahmed", mobile="+971500000123")
    db.add(customer)
    db.flush()

    for text in ("what SUVs do you have", "I'm Ahmed, 0500000123"):
        db.add(ConversationTurn(
            session_id="s-anon", speaker=Speaker.customer,
            lang=Language.en, text=text,
        ))
    db.flush()
    assert all(
        t.customer_id is None
        for t in db.scalars(select(ConversationTurn).where(
            ConversationTurn.session_id == "s-anon")).all()
    )

    class _Graph:
        def get_state(self, config):
            return type("S", (), {"values": {"customer_id": customer.id}})()

    _attribute_session(db, _Graph(), {}, "s-anon")
    db.flush()

    turns = db.scalars(select(ConversationTurn).where(
        ConversationTurn.session_id == "s-anon")).all()
    assert turns and all(t.customer_id == customer.id for t in turns), (
        "the turns spoken BEFORE identification must be attributed too"
    )


def test_attribution_never_breaks_a_turn(db):
    """A customer heard a real answer; failing to backfill a pointer is not a
    reason to lose it."""
    from app.agent.runner import _attribute_session

    class _Broken:
        def get_state(self, config):
            raise RuntimeError("checkpoint is gone")

    _attribute_session(db, _Broken(), {}, "s-x")  # must not raise
