"""The reschedule that did not happen (found live, 2026-08-31).

WHAT THIS REPLAYS. A customer with a confirmed booking asked to move it. Aisha
briefed the TestDrive specialist; the specialist's card body is ported
single-agent prose that SAYS "call reschedule_booking", so it emitted a tool_use
for a write it has never held. Dispatch refused it — correctly, that is the
binding being enforced — and the step settled with no proposal. Aisha re-briefed,
the same thing happened, she tried the tool herself, was refused again, and told
the customer to phone the helpline. The booking never moved, and the turn took
21 seconds to fail.

Half of every cascade that day ended in one of these two shapes.

THE TEST IS END TO END ON PURPOSE. Each half passed its own unit test: the
sub-agent correctly refused a write, and write.execute correctly performed a
proposal. What was missing was the join between them, so that is what is
asserted here — a booking row that actually moves.

Offline: the seeded db and a scripted client. No model, no network.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest

from app.models import Booking, BookingStatus, LocationType, Slot, SlotStatus
from app.orchestrator import run, subagent, write
from app.orchestrator.order import Step


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Client:
    def __init__(self, script):
        self.script = list(script)
        self.messages = types.SimpleNamespace(create=lambda **kw: _Block(content=self.script.pop(0)))


@pytest.fixture()
def booked(db):
    """A confirmed booking on `old`, and a `new` slot this session already holds.

    THE HOLD IS PART OF THE FLOW, not scaffolding: reschedule_booking refuses a
    slot the conversation does not own, which is the service guard underneath
    the write gate and is exactly what happened live before lock_slot ran.
    """
    base = datetime(2026, 9, 1, 13, 0)
    old = Slot(location_type=LocationType.showroom, showroom_id=1, vehicle_id=1,
               start=base, end=base + timedelta(minutes=90), status=SlotStatus.booked)
    new = Slot(location_type=LocationType.showroom, showroom_id=1, vehicle_id=1,
               start=base + timedelta(hours=5), end=base + timedelta(hours=6, minutes=30),
               status=SlotStatus.open)
    db.add_all([old, new])
    db.flush()
    booking = Booking(
        ref="TD-2026-7QHB", customer_id=1, vehicle_id=1, slot_id=old.id,
        location=LocationType.showroom,
        status=BookingStatus.confirmed,
    )
    db.add(booking)
    db.flush()

    # Held through the real service, exactly as lock_slot did live — rather than
    # by writing the lock columns, which would encode this test's guess about
    # the TTL clock instead of the behaviour.
    from app.tools import TOOL_FNS

    held = TOOL_FNS["lock_slot"](db, "s1", slot_id=new.id)
    assert held.get("locked"), held
    db.commit()
    return types.SimpleNamespace(booking=booking, old=old, new=new)


def test_the_specialists_tool_call_moves_the_booking(db, booked, monkeypatch):
    """THE JOIN. The sub-agent reaches for the tool exactly as it did live; the
    request survives all the way to a booking row on the new slot."""
    client = _Client([
        # Byte-for-byte the shape seen live, `booking_ref` and a string id and all.
        [_Block(type="tool_use", name="reschedule_booking", id="u1",
                input={"booking_ref": "TD-2026-7QHB", "new_slot_id": str(booked.new.id)})],
        [_Block(type="text", text='{"findings": {}, "note": "asked for the move"}')],
    ])
    monkeypatch.setattr(subagent, "_client", lambda: client)

    findings = subagent.run_step(
        Step(brief="move TD-2026-7QHB to the 6:30 PM slot", agent="testdrive", index=0),
        {}, db=db, session_id="s1", customer_id=1,
    )

    # It asked. It did NOT do it, and it did not report it as done.
    proposals = findings["_proposals"]
    assert proposals == [{
        "action": "reschedule_booking",
        "args": {"ref": "TD-2026-7QHB", "new_slot_id": booked.new.id},
        "agent": "testdrive",
    }]
    db.refresh(booked.booking)
    assert booked.booking.slot_id == booked.old.id, "a sub-agent must never write"

    # And the orchestrator performs it, through the gate that has always been there.
    results = write.execute_all(
        db, "s1",
        [write.Proposal(**p) for p in proposals],
        customer_id=1,
        allow={"reschedule_booking"},
    )
    db.commit()

    assert [r.status for r in results] == ["executed"], [r.detail for r in results]
    db.refresh(booked.booking)
    assert booked.booking.slot_id == booked.new.id


def test_the_step_reports_the_request_not_a_failure(db, booked, monkeypatch):
    """Live, this step came back with nothing and `nothing_worked: true`, which
    is what sent Aisha round again. It must now come back ok."""
    client = _Client([
        [_Block(type="tool_use", name="reschedule_booking", id="u1",
                input={"ref": "TD-2026-7QHB", "new_slot_id": booked.new.id})],
        [_Block(type="text", text='{"findings": {}}')],
    ])
    monkeypatch.setattr(subagent, "_client", lambda: client)

    def _runner(step, ctx, **kw):
        return subagent.run_step(step, ctx, db=db, session_id="s1", customer_id=1, **kw)

    result = run.execute(
        [{"brief": "move booking TD-2026-7QHB to the evening slot"}],
        _runner,
        known={"booking_ref"},
        judge=lambda brief, agents: "testdrive",
    )

    assert [s.status for s in result.steps] == ["ok"]
    assert result.steps[0].proposals[0]["action"] == "reschedule_booking"
