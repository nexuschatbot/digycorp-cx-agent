"""STANDING GUARD: rule 9 precondition 3 / FR-BK-06 — the driving-licence reminder.

WHY THIS FILE EXISTS. Rule 9 lists four preconditions for create_booking. Three of
them left traces the code could check (a lock owned by this session, a consent row,
the booking write itself); the licence reminder was free text, so nothing verified
it. A live probe during persona step 1 found 2 of 2 completed bookings had never
given it — the model batched lock_slot + record_consent + create_booking into a
SINGLE generation, which removes the turn boundary where the reminder would have
been spoken. It arrived one turn AFTER the booking was already written.

TWO GUARDS, MATCHED TO WHAT EACH CAN PROVE:

  - ORDER, in code (always runs). create_booking refuses unless
    acknowledge_licence_reminder ran against the live lock. Deterministic, no
    model, no API key. This is what closes the batching hole: the acknowledgement
    cannot be back-dated into the same generation that books.

  - DELIVERY, in behaviour (live). The order guard proves a tool ran, NOT that
    Aisha said anything — a model could acknowledge in silence. So a live booking
    conversation is checked for the reminder actually appearing in the agent's own
    words before create_booking. String-matching the AGENT'S OUTPUT is legitimate
    here in a way it would NOT be inside the guard: a test asserting what was said
    is checking behaviour, whereas a guard grepping prose would be trusting
    display text as truth — the mistake this codebase has made four times.

DELIBERATELY NOT STRUCTURAL BEYOND THIS. Guaranteeing delivery would mean the
server rendering the reminder itself, which costs an API-contract change across
both repos and turns a warm line in Aisha's voice into a UI chip that cannot speak
Arabic (LOC-01). The licence reminder is a courtesy — forgetting it means someone
forgets their licence, not a compliance breach. Enforcement is matched to stakes:
disclosure and double-booking get the sledgehammer, this gets order + a test.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.models import Booking, LocationType, Slot, SlotStatus
from app.services import booking as booking_service
from app.services import slots as slots_service
from app.tools import REGISTRY


# EN both spellings, and Arabic from day one: step 3 rewrites this prompt into
# Gulf Arabic, and a guard that only reads English would report a regression the
# moment Aisha correctly says "رخصة القيادة".
LICENCE = re.compile(r"licen[cs]e|رخصة", re.I)

SESSION = "sess-licence"


def _open_slot(db) -> Slot:
    return db.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
    ).first()


def _customer_id(db) -> int:
    from app.services import customers as customers_service

    return customers_service.find_or_create(db, mobile="+971501234567", name="Hanif")[
        "customer"
    ]["customer_id"]


# --------------------------------------------------------------------------- #
# ORDER — always runs. No model, no API key.
# --------------------------------------------------------------------------- #
def test_booking_is_refused_without_the_acknowledgement(slots) -> None:
    """THE REGRESSION CASE. Lock, then book — skipping the reminder entirely."""
    db = slots
    slot = _open_slot(db)
    cid = _customer_id(db)
    assert slots_service.lock(db, session_id=SESSION, slot_id=slot.id)["locked"]

    result = booking_service.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
    )
    assert result.get("error") == "licence_reminder_not_acknowledged", result
    assert db.scalar(select(Booking)) is None, "a booking was written without the reminder"


def test_booking_succeeds_once_acknowledged(slots) -> None:
    """The same call, after the acknowledgement — the guard must not be a wall."""
    db = slots
    slot = _open_slot(db)
    cid = _customer_id(db)
    slots_service.lock(db, session_id=SESSION, slot_id=slot.id)
    assert slots_service.acknowledge_licence_reminder(db, session_id=SESSION)["acknowledged"]

    result = booking_service.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
    )
    assert result.get("created") is True, result
    assert result["booking"]["booking_ref"].startswith("TD-")


def test_acknowledgement_requires_a_live_lock(slots) -> None:
    """"Remind, then lock" is not the rule 9 sequence. Without a lock there is no
    slot for the reminder to belong to, so there is nothing to record."""
    out = slots_service.acknowledge_licence_reminder(slots, session_id="sess-no-lock")
    assert out == {"acknowledged": False, "reason": "no_locked_slot"}


def test_acknowledgement_does_not_survive_a_relock(slots) -> None:
    """THE SCOPE GUARD, and the reason the flag lives on the LOCK.

    "Actually, make it 8pm" re-locks a different slot. The customer never heard a
    reminder for THAT slot, so the earlier acknowledgement must not satisfy it —
    otherwise one reminder silently licenses every later booking in the session.
    """
    db = slots
    first, second = db.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
        .limit(2)
    ).all()

    slots_service.lock(db, session_id=SESSION, slot_id=first.id)
    slots_service.acknowledge_licence_reminder(db, session_id=SESSION)
    slots_service.lock(db, session_id=SESSION, slot_id=second.id)  # changed their mind

    db.refresh(second)
    assert second.licence_ack is False, "the ack leaked from the abandoned slot to the new one"

    cid = _customer_id(db)
    result = booking_service.create(
        db,
        session_id=SESSION,
        customer_id=cid,
        vehicle_id=second.vehicle_id,
        slot_id=second.id,
    )
    assert result.get("error") == "licence_reminder_not_acknowledged", result


def test_acknowledgement_is_scoped_to_this_session(slots) -> None:
    """One conversation must not acknowledge on another's behalf. Enforceable
    only because session_id is injected by the executor, never modelled."""
    db = slots
    slot = _open_slot(db)
    slots_service.lock(db, session_id=SESSION, slot_id=slot.id)

    other = slots_service.acknowledge_licence_reminder(db, session_id="sess-someone-else")
    assert other == {"acknowledged": False, "reason": "no_locked_slot"}
    db.refresh(slot)
    assert slot.licence_ack is False


def test_tool_is_registered_and_takes_no_modelled_arguments() -> None:
    """The schema is the contract. A slot_id the model could pass would let it
    acknowledge one slot and book another — the exact ordering this enforces."""
    entry = REGISTRY.get("acknowledge_licence_reminder")
    assert entry is not None, "the tool is not registered — the agent cannot call it"
    assert entry["schema"]["input_schema"]["properties"] == {}
    assert entry["schema"]["input_schema"].get("required", []) == []


def test_create_booking_schema_tells_the_model_how_to_recover() -> None:
    """An error the model cannot act on is a dead end mid-booking (rule 2)."""
    description = REGISTRY["create_booking"]["schema"]["description"]
    assert "licence_reminder_not_acknowledged" in description
    assert "acknowledge_licence_reminder" in description


def test_prompt_carries_the_step() -> None:
    """Rule 9's four preconditions must still be four."""
    from app.agent.prompt import SYSTEM_PROMPT

    body = SYSTEM_PROMPT.lower()
    assert "licence" in body
    assert "acknowledge_licence_reminder" in body
