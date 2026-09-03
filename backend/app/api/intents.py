"""Tap intents — the DIRECT path (AD-I/AD-J), no LLM in the loop.

    POST /api/intent/lock-slot    {session_id, slot_id}
    POST /api/intent/consent      {session_id, whatsapp}
    POST /api/intent/licence-ack  {session_id}

WHY THESE EXIST. When a customer TAPS a slot chip, the tap already carries
slot_id — the answer is fully determined. Routing it through the model costs a
whole turn (two model calls, ~11.7k prompt tokens each) for Claude to hand back
a fact the UI already had, plus the seconds of latency the audience watches. So
the tap goes straight to the same service the tool calls.

This is the pattern CLAUDE.md describes for the one-tap WhatsApp reminder
buttons ("tapping Cancel on a reminder must never invoke a language model"),
applied to the three in-chat taps that COMMIT state.

WHAT IS DELIBERATELY *NOT* HERE. Showroom-vs-doorstep is not a commit: no
service records it (location_type is only an argument to get_available_slots),
and per FR-SL-07 choosing doorstep must then collect an address and explicit
consent. That is a question, not a determined answer, so it stays conversational
and the UI merely offers buttons that send a normal turn. Adding it here would
route around a signed requirement to save one turn.

THREE THINGS EVERY TAP MUST DO, and the reason each matters:

 1. CALL THE TOOL FUNCTION, not the service directly. The tool wrappers already
    validate, inject trusted context and shape the error envelope; calling them
    means the tap and the agent cannot drift, and the result dict is *already*
    the exact payload a tool_result carries.

 2. AUDIT AS actor=customer. The tool node's choke point stamps actor=agent
    because the agent chose to act. Here a PERSON tapped. Logging it as the
    agent would have the audit console — the screen built to prove honesty —
    quietly crediting Aisha with the customer's decision (rule 15/16, FR-AD-02).

 3. WRITE THE ACTION INTO THE AGENT'S MEMORY. If the slot is locked but the
    checkpoint never hears about it, the next turn re-offers a slot that is gone
    or re-asks a settled question — the agent contradicting reality, which is
    exactly what rule 2 exists to prevent. We append a synthetic
    tool_use/tool_result pair so the history keeps one uniform shape.

ON THE SYNTHETIC PAIR'S HONESTY. It records a tool call the model never emitted.
That is a real trade, made deliberately: the alternative shapes (a prose note,
or re-reading state next turn) either break the message shape or spend the turn
we just saved. The tool_use id is prefixed `tap_` so the pair is always
identifiable as tap-originated, and the AUDIT — the actual record of what
happened — says actor=customer. The checkpoint is the model's working memory,
not the evidence trail; the audit is the evidence trail, and it tells the truth.
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent import build_graph, new_sqlite_checkpointer
from app.config import AGENT_CHECKPOINT_PATH
from app.db import get_db
from app.models import Actor, AuditType
from app.services import audit, slots
from app.tools import REGISTRY

router = APIRouter(prefix="/api/intent")


class LockSlotBody(BaseModel):
    session_id: str
    slot_id: int


class ConsentBody(BaseModel):
    session_id: str
    whatsapp: bool


class LicenceAckBody(BaseModel):
    session_id: str


def _thread(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _synthetic_pair(call_id: str, tool: str, tool_input: dict, result: dict) -> list[dict]:
    """The tap, written in the shape the rest of the history already uses.

    Ends on the tool_result (a user-role message), so the customer's next typed
    message simply follows it. Two consecutive user messages is fine — the API
    combines same-role turns.
    """
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    # Same serialisation as tool_node, so a replayed tap is
                    # byte-identical to a replayed tool call.
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
        },
    ]


def _tap(db: Session, session_id: str, tool: str, tool_input: dict) -> dict:
    """Run one tap: audit it, execute it, and tell the agent it happened."""
    entry = REGISTRY.get(tool)
    if entry is None:  # unreachable via the typed routes; a guard, not a path
        raise HTTPException(status_code=500, detail="unknown_tool")

    graph = build_graph(new_sqlite_checkpointer(AGENT_CHECKPOINT_PATH))
    config = _thread(session_id)
    snapshot = graph.get_state(config)
    state = snapshot.values if snapshot else {}

    # customer_id is TRUSTED CONTEXT and is read from the checkpoint — the same
    # place the tool node reads it. It is never accepted from the request body:
    # a client-supplied customer id would be an assertion, not a fact, and
    # record_consent writes against it.
    customer_id = state.get("customer_id") if isinstance(state, dict) else None

    correlation_id = uuid4().hex
    audit.record(
        db,
        session=session_id,
        actor=Actor.customer,  # a person tapped — see (2) in the module docstring
        type=AuditType.tool_call,
        payload={"tool": tool, "input": tool_input, "via": "tap"},
        correlation_id=correlation_id,
    )

    result = entry["fn"](
        db=db,
        session_id=session_id,
        customer_id=customer_id,
        correlation_id=correlation_id,
        **tool_input,
    )
    # The service layer flushes but never commits (audit.record's contract): the
    # audit row must live or die with the write it describes.
    db.commit()

    # Only now tell the agent. Committing first means a failed write can never
    # leave the model believing something happened that did not.
    update: dict = {"messages": _synthetic_pair(correlation_id, tool, tool_input, result)}
    # A LOCKED SLOT ESTABLISHES THE CAR (found live, 2026-09-03). tool_node folds
    # slots.facts_for_slot into session_facts when the MODEL calls lock_slot;
    # the tap path told the agent about the lock but left the facts alone, so
    # the "established" vehicle stayed whatever an earlier search led with —
    # and the specialist briefed to book the customer's actual car refused
    # over the contradiction. Same fold, same source row, here.
    if tool == "lock_slot" and isinstance(result, dict) and result.get("locked") is True:
        held = slots.facts_for_slot(db, tool_input.get("slot_id"))
        if held:
            facts = dict(state.get("session_facts") or {}) if isinstance(state, dict) else {}
            update["session_facts"] = {**facts, **held}
    graph.update_state(config, update)
    return result


@router.post("/lock-slot")
def lock_slot_intent(body: LockSlotBody, db: Session = Depends(get_db)) -> dict:
    """Tapping a slot chip. The chip carries slot_id, so nothing needs inferring.

    Returns the tool's own envelope verbatim — including the failure case
    ({"locked": false, "reason": "slot_no_longer_available"}) so the UI can
    re-offer gracefully, exactly as the agent would have.

    GATED BY THE BOOKING STATE MACHINE (T4.4). This is the one tap that is a
    COMMERCIAL write — it moves inventory — and a tap that bypassed the machine
    would leave it and the database disagreeing about where the booking is. The
    other two taps are conversational writes (a yes, an acknowledgement) and
    belong to whoever is talking, so they stay direct.

    THE MACHINE READS COLUMNS, NOT AGENT TURNS, which is what makes a tapped
    licence acknowledgement advance it identically to a typed one — the tap sets
    `slot.licence_ack` through the same tool the agent would have called.
    """
    from app.orchestrator import booking_state

    state = booking_state.read(db, body.session_id)
    if "lock_slot" not in state.allowed:
        # Reported as data, not raised: the UI re-offers, exactly as it does for
        # a slot that was taken.
        return {
            "locked": False,
            "reason": "not_valid_at_this_stage",
            "stage": state.stage,
            "next": booking_state.next_step(state),
        }
    return _tap(db, body.session_id, "lock_slot", {"slot_id": body.slot_id})


@router.post("/consent")
def consent_intent(body: ConsentBody, db: Session = Depends(get_db)) -> dict:
    """Tapping yes/no on WhatsApp consent (FR-CF-05).

    A refusal is a first-class answer, not an error: it is recorded the same way
    and must never block the booking.
    """
    return _tap(db, body.session_id, "record_consent", {"messaging": body.whatsapp})


@router.post("/licence-ack")
def licence_ack_intent(body: LicenceAckBody, db: Session = Depends(get_db)) -> dict:
    """Tapping "got it" on the driving-licence reminder (FR-BK-06).

    Zero-argument by nature — the tool is a state flag, which is what makes it a
    natural button rather than a sentence the customer has to type.
    """
    return _tap(db, body.session_id, "acknowledge_licence_reminder", {})
