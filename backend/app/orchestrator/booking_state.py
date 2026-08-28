"""The booking state machine: hold -> recap -> licence -> consent -> create (T4.3).

WHAT THIS IS AND, MORE IMPORTANTLY, WHAT IT IS NOT. It is a gate that says which
commercial action is valid RIGHT NOW. It is NOT the thing that keeps a booking
safe — that is still the service layer, unchanged beneath it:

    create_booking      refuses without the licence acknowledgement
    lock_slot           enforces lock ownership by session
    record_consent      fails safe on an ambiguous yes
    tool_node           injects customer_id so a write cannot name its subject

Those guards were load-bearing before this existed and remain load-bearing now.
This machine exists so the ORDER is explicit and inspectable, not so the guards
can relax. A second write path that happened to skip one is exactly the defect
the whole architecture is arranged to prevent.

THE STATE IS DERIVED, NOT STORED, and that is deliberate. There is no
booking_state column and no in-memory session map: the state is READ from the
facts that already exist — is a slot locked by this session, has the licence been
acknowledged, has consent been recorded. A stored copy is a copy that goes stale
the moment a lock expires or a tap writes directly, and then the machine and the
database disagree about where a booking is, which is worse than having no machine.

CONSENT IS NOT PART OF THE BOOKING GATE, and this is the subtle one. Messaging
consent is required to SEND the confirmation, not to CREATE the booking, and
conflating them would let a customer who declines WhatsApp be refused a test
drive. It appears in the sequence because it is asked for around then, not
because create_booking depends on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("app.orchestrator.booking_state")

# The ordered stages. Names are the plan's.
STAGES: tuple[str, ...] = ("hold", "recap", "licence", "create", "done")

# Phrases that answer "shall I book it?". Deliberately SMALL and exact — a fuzzy
# yes-detector is how a booking gets created from "yes, but on Sunday". Anything
# not on this list falls through to the model, which is the safe direction.
_AFFIRMATIONS = frozenset({
    "yes", "yes please", "yeah", "yep", "yup", "ok", "okay", "sure",
    "confirm", "confirmed", "go ahead", "please do", "book it", "do it",
    "نعم", "أيوه", "ايوه", "تمام", "أوكي", "اوكي", "احجز",
})


@dataclass(frozen=True)
class BookingState:
    stage: str
    slot_id: int | None = None
    licence_acknowledged: bool = False
    detail: str = ""

    @property
    def allowed(self) -> set[str]:
        """Which commercial actions are valid from here.

        RESCHEDULE AND CANCEL ARE ALWAYS ALLOWED. They act on a booking that
        already exists and have nothing to do with this flow — gating them
        behind it would mean a customer could not cancel while mid-way through
        booking something else.
        """
        always = {"reschedule_booking", "cancel_booking"}
        if self.stage == "hold":
            return always | {"hold_slot", "lock_slot"}
        if self.stage in ("recap", "licence"):
            # The hold can still be moved — a customer changing their mind about
            # the time before confirming is ordinary, not an error.
            return always | {"hold_slot", "lock_slot"}
        if self.stage == "create":
            return always | {"create_booking", "hold_slot", "lock_slot"}
        return always


def read(db, session_id: str, *, customer_id: int | None = None) -> BookingState:
    """Where this session's booking has got to. Derived from live facts.

    Never raises: an unreadable state degrades to `hold`, which is the most
    restrictive stage and therefore the safe default — it permits holding and
    refuses creating.
    """
    try:
        from sqlalchemy import select

        from app.models import Slot, SlotStatus

        slot = db.scalar(
            select(Slot).where(
                Slot.locked_by_session == session_id,
                Slot.status == SlotStatus.held,
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("could not read booking state — defaulting to hold")
        return BookingState(stage="hold", detail="state unreadable")

    if slot is None:
        return BookingState(stage="hold", detail="no slot held by this session")

    acknowledged = _licence_acknowledged(slot)
    if not acknowledged:
        return BookingState(
            stage="licence", slot_id=slot.id,
            detail="slot held; licence reminder not yet acknowledged",
        )
    return BookingState(
        stage="create", slot_id=slot.id, licence_acknowledged=True,
        detail="slot held and licence acknowledged",
    )


def _licence_acknowledged(slot) -> bool:
    """Has the licence reminder been acknowledged for this held slot?

    READ FROM `slot.licence_ack` — THE SAME COLUMN create_booking's guard reads.
    That is the point: the state machine and the guard beneath it must never be
    able to disagree about where a booking is.

    An earlier version of this scanned the audit trail for the tool call, which
    would have drifted the moment anything else set the column — including the
    licence-ack TAP intent, which writes it directly without an agent turn.
    Reading the column makes a tap and a typed acknowledgement indistinguishable
    here, which is exactly what T4.4 needs.
    """
    return bool(getattr(slot, "licence_ack", False))


def pending_confirmation(db, session_id: str):
    """The callable app/orchestrator/cascade.decide() takes.

    Returns what THIS MESSAGE confirms, or None. Takes the message because
    "is anything pending?" is the wrong question — with a hold open, "and
    Sunday?" is a new question about a different day, not a confirmation.
    """

    def _check(message: str) -> str | None:
        text = (message or "").strip().lower().rstrip("!.,")
        if text not in _AFFIRMATIONS:
            return None
        state = read(db, session_id)
        if state.stage in ("licence", "create") and state.slot_id is not None:
            return f"the held slot {state.slot_id}"
        return None

    return _check


def next_step(state: BookingState) -> str:
    """What has to happen next, in words Aisha can act on."""
    return {
        "hold": "offer times and hold the one they choose",
        "recap": "read the booking back before confirming",
        "licence": "ask them to acknowledge the driving-licence reminder",
        "create": "confirm and create the booking",
        "done": "nothing — the booking exists",
    }.get(state.stage, "unknown")
