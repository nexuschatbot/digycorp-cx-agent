"""A booking must survive a specialist whose reply was cut off (2026-09-04).

THE LIVE FAILURE. A customer with a held slot, an acknowledged licence and
recorded consent was told Aisha was "having a little trouble completing the
booking" and handed to a human. The specialist had proposed create_booking
correctly — twice — and both replies ended mid-string, unterminated, because
they did not fit `max_tokens`. _parse_findings could not load them, fell back
to _prose_note, and the proposals were discarded silently.

MEASURED over all 64 sub-agent steps in the test VM's journal: 25 (39%) were
discarded the same way. Parsed replies run 94..691 chars; discarded ones start
at 563. For a read that costs a note; for a write it costs the booking.
"""

from __future__ import annotations

from app.orchestrator import booking_state, select, subagent
from app.orchestrator.order import Step

SESSION = "truncated-session"

# The reply that was actually generated, cut where the ceiling cut it: the
# proposal is complete, the trailing note is not.
TRUNCATED_REPLY = """The slot is confirmed as held and available.

```json
{
  "findings": {"vehicle_id": %d, "slot_id": %d},
  "proposals": [
    {"action": "create_booking", "args": {"vehicle_id": %d, "slot_id": %d}}
  ],
  "note": "Slot %d is held and confirmed. Ready to create booking at 9"""


def _held(db, slot, *, licence: bool) -> None:
    """Put the session where the live one was: slot locked, licence recorded."""
    from app.models import SlotStatus
    from app.clock import now

    slot.status = SlotStatus.locked
    slot.locked_by_session = SESSION
    slot.locked_at = now()
    slot.licence_ack = licence
    db.commit()


# --------------------------------------------------------------------------- #
# The parse failure itself
# --------------------------------------------------------------------------- #
def test_a_truncated_reply_loses_its_proposal():
    """The defect, stated plainly: this is why the booking died."""
    text = TRUNCATED_REPLY % (1, 745, 1, 745, 745)
    out = subagent._parse_findings(text)
    assert out.get("_proposals") is None      # the ask is gone
    assert out.get("_note")                   # only prose survives
    # And it is not the model's fault — the proposal it wrote is right there.
    assert '"action": "create_booking"' in text


def test_the_same_reply_parses_when_it_fits():
    """Untruncated, the identical shape yields the proposal."""
    text = (TRUNCATED_REPLY % (1, 745, 1, 745, 745)) + ' AM."\n}\n```'
    out = subagent._parse_findings(text)
    assert out["_proposals"] == [
        {"action": "create_booking", "args": {"vehicle_id": 1, "slot_id": 745}}
    ]


# --------------------------------------------------------------------------- #
# The recovery
# --------------------------------------------------------------------------- #
def test_the_write_is_derived_when_the_state_determines_it(slots, open_slot):
    _held(slots, open_slot, licence=True)
    step = Step(
        brief=f"Please create a booking for the customer, slot {open_slot.id}.",
        agent="testdrive",
    )
    derived = subagent._derived_proposals(slots, SESSION, step, 1)

    assert derived == [{
        "action": "create_booking",
        "args": {"vehicle_id": open_slot.vehicle_id, "slot_id": open_slot.id},
    }]


def test_the_car_comes_from_the_slot_not_the_brief(slots, open_slot):
    """booking.create refuses any other vehicle for a slot, so there is nothing
    to choose — and a brief naming the wrong car must not change the answer."""
    _held(slots, open_slot, licence=True)
    step = Step(
        brief=f"Create a booking for vehicle_id 99, slot {open_slot.id}.",
        agent="testdrive",
    )
    derived = subagent._derived_proposals(slots, SESSION, step, 1)
    assert derived[0]["args"]["vehicle_id"] == open_slot.vehicle_id


# --------------------------------------------------------------------------- #
# What it must never do
# --------------------------------------------------------------------------- #
def test_silent_before_the_licence_is_acknowledged(slots, open_slot):
    """The gate, not a convenience: `licence` stage means the recap-yes and the
    licence reminder have not both happened. Deriving here would book somebody
    who never confirmed."""
    _held(slots, open_slot, licence=False)
    assert booking_state.read(slots, SESSION).stage == "licence"
    step = Step(brief="Create a booking for the customer.", agent="testdrive")
    assert subagent._derived_proposals(slots, SESSION, step, 1) == []


def test_silent_with_no_held_slot(slots):
    step = Step(brief="Create a booking for the customer.", agent="testdrive")
    assert subagent._derived_proposals(slots, SESSION, step, 1) == []


def test_silent_when_the_brief_asked_for_something_else(slots, open_slot):
    """A read brief must never produce a write, however ready the state is."""
    _held(slots, open_slot, licence=True)
    step = Step(brief="What times are free at Al Quoz on Saturday?", agent="testdrive")
    assert subagent._derived_proposals(slots, SESSION, step, 1) == []


def test_silent_for_a_card_that_may_not_propose_it(slots, open_slot):
    _held(slots, open_slot, licence=True)
    step = Step(brief="Create a booking for the customer.", agent="sales")
    assert subagent._derived_proposals(slots, SESSION, step, 1) == []


def test_reschedule_and_cancel_are_never_derived(slots, open_slot):
    """Their arguments name a booking the state machine does not determine."""
    _held(slots, open_slot, licence=True)
    for brief in ("Reschedule booking TD-2026-AAAA to another slot.",
                  "Cancel booking TD-2026-AAAA."):
        derived = subagent._derived_proposals(slots, SESSION, Step(brief=brief, agent="testdrive"), 1)
        assert all(d["action"] == "create_booking" for d in derived)


def test_a_broken_read_never_costs_the_turn(slots, open_slot, monkeypatch):
    _held(slots, open_slot, licence=True)

    def _boom(*a, **k):
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(booking_state, "read", _boom)
    step = Step(brief="Create a booking for the customer.", agent="testdrive")
    assert subagent._derived_proposals(slots, SESSION, step, 1) == []


# --------------------------------------------------------------------------- #
# Routing and recovery must read a brief the same way
# --------------------------------------------------------------------------- #
def test_action_owners_agrees_with_action_owner():
    brief = "Please create a booking for the customer, slot 745."
    owners = select.action_owners(brief)
    assert owners.get("create_booking") == {"testdrive"}
    chosen = select.action_owner(brief)
    assert chosen is not None and chosen.agent == "testdrive"


# --------------------------------------------------------------------------- #
# Telling Aisha the difference
# --------------------------------------------------------------------------- #
def _no_action_detail(findings: dict) -> str:
    from app.orchestrator import run, turn

    result = run.TurnResult(
        steps=[run.StepResult(brief="Create a booking.", agent="testdrive",
                              status="ok", findings=dict(findings))],
    )
    payload = turn.compose_input(result)
    rows = [u for u in payload["unavailable"] if u.get("status") == "no_action"]
    assert rows, "expected the step to be reported as no_action"
    return rows[0]["detail"]


def test_a_truncated_step_is_not_reported_as_silence():
    cut = _no_action_detail({"_note": "…", "_truncated": True})
    assert "CUT OFF" in cut
    # The handoff is FORBIDDEN here, not merely unmentioned: escalating is
    # exactly what happened live, and a ceiling is not a reason to fetch a human.
    assert "do NOT offer a person" in cut
    assert "Brief it again" in cut

    quiet = _no_action_detail({"_note": "…"})
    assert "or tell the customer plainly and offer a person" in quiet
    assert "CUT OFF" not in quiet
