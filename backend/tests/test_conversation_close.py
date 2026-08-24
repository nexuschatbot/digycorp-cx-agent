"""Rule 19 proof: closing a conversation is an AUDITED, GUARDED action.

The close is a tool call like any other write, so it gets the same treatment as
create_booking: trusted context it cannot forge, a service-layer guard the prompt
cannot relax, idempotency because LLMs retry, and a row in the append-only trail
(rule 15, FR-AD-02). A conversation that ENDED should be as auditable as one that
booked — that is the console's whole story.

What these lock in:
- a close RELEASES this session's live slot lock (no stranded inventory);
- a close BATCHED with other tool calls is refused — closing is clean-end-only;
- a repeat close is idempotent: current state, one audit row, not a stutter;
- the graph ENDS on a close: no extra generation after the farewell;
- the lock-release count never reaches the model (rule 14's corollary).
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.models import AuditEvent, AuditType, Slot, SlotStatus
from app.services import conversation, slots as slots_service
from app.tools import REGISTRY
from app.tools.conversation_tools import CLOSE_CONVERSATION_SCHEMA

SESSION = "sess-close-19"


def _tool_use(tid: str, tool: str, /, **inp) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}


class ScriptedModel:
    """Replays canned assistant turns; counts how many generations it was asked
    for, which is how the 'no turn after the farewell' test proves itself."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.i = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        msg = self.steps[self.i]
        self.i += 1
        return msg


def _run(db, session_id: str, model: ScriptedModel) -> dict:
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=model)
    return graph.invoke(
        {
            "messages": [{"role": "user", "content": "No thanks, that's everything. Bye!"}],
            "iterations": 0,
            "customer_id": None,
            "closed": False,
        },
        config,
    )


def _results(final: dict) -> dict[str, dict]:
    """tool_use_id -> parsed tool_result JSON."""
    out: dict[str, dict] = {}
    for msg in final["messages"]:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out[b["tool_use_id"]] = json.loads(b["content"])
    return out


def _close_events(db, session_id: str) -> list[AuditEvent]:
    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == session_id, AuditEvent.type == AuditType.intent
        ).order_by(AuditEvent.id)
    ).all()
    return [r for r in rows if (r.payload or {}).get("action") == conversation.CLOSE_ACTION]


# --------------------------------------------------------------------------- #
# The schema contract — trusted context stays out of it
# --------------------------------------------------------------------------- #
def test_close_schema_takes_no_arguments():
    """Which conversation to close is session_id, INJECTED. If it ever appears in
    the schema, the model could close somebody else's chat by asserting an id."""
    props = CLOSE_CONVERSATION_SCHEMA["input_schema"].get("properties", {})
    assert props == {}
    assert not CLOSE_CONVERSATION_SCHEMA["input_schema"].get("required")
    assert "close_conversation" in REGISTRY


# --------------------------------------------------------------------------- #
# Guard 1: no stranded state
# --------------------------------------------------------------------------- #
def test_close_releases_a_held_slot(slots, open_slot):
    """'Actually never mind, bye' with a slot held must not park that slot for the
    full TTL. A clean close leaves clean inventory."""
    locked = slots_service.lock(slots, session_id=SESSION, slot_id=open_slot.id)
    assert locked["locked"] is True
    assert slots.get(Slot, open_slot.id).status == SlotStatus.locked

    result = conversation.close(slots, session_id=SESSION)

    assert result == {"closed": True, "already_closed": False, "released_locks": 1}
    freed = slots.get(Slot, open_slot.id)
    assert freed.status == SlotStatus.open
    assert freed.locked_by_session is None
    assert freed.locked_at is None


def test_close_releases_only_this_sessions_lock(slots, open_slot):
    """Scoped by the injected session_id — a close can never free another
    customer's hold."""
    other = slots.scalars(
        select(Slot).where(Slot.status == SlotStatus.open, Slot.id != open_slot.id).order_by(Slot.id)
    ).first()
    slots_service.lock(slots, session_id=SESSION, slot_id=open_slot.id)
    slots_service.lock(slots, session_id="someone-else", slot_id=other.id)

    conversation.close(slots, session_id=SESSION)

    assert slots.get(Slot, open_slot.id).status == SlotStatus.open
    assert slots.get(Slot, other.id).status == SlotStatus.locked  # untouched


# --------------------------------------------------------------------------- #
# Guard 2: idempotency + audit (rule 15)
# --------------------------------------------------------------------------- #
def test_close_is_audited_once_and_is_idempotent(db):
    """LLMs retry. A second close returns current state, not an error — and does
    NOT append a second row, so the trail shows one close per conversation."""
    first = conversation.close(db, session_id=SESSION, correlation_id="corr-1")
    second = conversation.close(db, session_id=SESSION, correlation_id="corr-2")
    db.commit()

    assert first["already_closed"] is False
    assert second == {"closed": True, "already_closed": True, "released_locks": 0}

    events = _close_events(db, SESSION)
    assert len(events) == 1
    assert events[0].payload["correlation_id"] == "corr-1"
    assert conversation.is_closed(db, session_id=SESSION) is True


def test_close_audit_correlates_with_the_tool_call(db):
    """One action, two facets, ONE correlation id — the console groups them."""
    graph_model = ScriptedModel([_tool_use("t1", "close_conversation"), {"role": "assistant", "content": []}])
    _run(db, SESSION, graph_model)

    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.session == SESSION).order_by(AuditEvent.id)
    ).all()
    call = next(r for r in rows if r.type == AuditType.tool_call)
    intent = next(r for r in rows if r.type == AuditType.intent)
    assert call.payload["tool"] == "close_conversation"
    assert intent.payload["action"] == conversation.CLOSE_ACTION
    assert call.payload["correlation_id"] == intent.payload["correlation_id"]


# --------------------------------------------------------------------------- #
# Guard 3: clean-end-only
# --------------------------------------------------------------------------- #
def test_close_batched_with_other_tools_is_refused(slots, open_slot):
    """Closing mid-sequence is the one way this action does damage. A close sent
    alongside other work is refused as DATA — the siblings still run, the
    conversation stays open, and the agent simply keeps talking."""
    batched = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "lock_slot", "input": {"slot_id": open_slot.id}},
            {"type": "tool_use", "id": "t2", "name": "close_conversation", "input": {}},
        ],
    }
    model = ScriptedModel([batched, {"role": "assistant", "content": []}])
    final = _run(slots, SESSION, model)
    results = _results(final)

    # The close was refused...
    assert results["t2"]["closed"] is False
    assert results["t2"]["reason"] == "close_not_alone"
    assert conversation.is_closed(slots, session_id=SESSION) is False
    assert not _close_events(slots, SESSION)

    # ...and its sibling ran normally: a refusal is not a turn-wide abort.
    assert results["t1"]["locked"] is True
    assert slots.get(Slot, open_slot.id).status == SlotStatus.locked

    # The conversation is still live, so the graph went BACK to the agent.
    assert model.i == 2


# --------------------------------------------------------------------------- #
# Guard 4: the farewell is the last word
# --------------------------------------------------------------------------- #
def test_graph_ends_on_close_without_another_generation(db):
    """The agent says goodbye in the SAME message that closes. Routing back for
    one more generation would let the model staple a second sign-off onto the
    first — so the post-tools edge must END."""
    farewell = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "It was my pleasure, Hanif. Have a wonderful day."},
            {"type": "tool_use", "id": "t1", "name": "close_conversation", "input": {}},
        ],
    }
    never = {"role": "assistant", "content": [{"type": "text", "text": "SHOULD NEVER BE GENERATED"}]}
    model = ScriptedModel([farewell, never])

    final = _run(db, SESSION, model)

    assert model.i == 1, "the agent was given a generation AFTER its farewell"
    assert final["closed"] is True
    assert conversation.is_closed(db, session_id=SESSION) is True
    texts = [
        b.get("text", "")
        for m in final["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert not any("SHOULD NEVER" in t for t in texts)


# --------------------------------------------------------------------------- #
# Rule 14's corollary: withhold the fact, don't ask the prompt to resist it
# --------------------------------------------------------------------------- #
def test_lock_release_count_never_reaches_the_model(slots, open_slot):
    """Inventory hygiene is not a conversational fact. A model told it released a
    held slot will eventually narrate that to someone trying to say goodbye — so
    the count goes to the audit trail and NOT into the tool result."""
    slots_service.lock(slots, session_id=SESSION, slot_id=open_slot.id)
    model = ScriptedModel([_tool_use("t1", "close_conversation"), {"role": "assistant", "content": []}])

    final = _run(slots, SESSION, model)

    result = _results(final)["t1"]
    assert result == {"closed": True, "already_closed": False}
    assert "released_locks" not in result
    # ...but it IS in the trail, where it belongs.
    assert _close_events(slots, SESSION)[0].payload["released_locks"] == 1
