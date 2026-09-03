"""Tap intents — the DIRECT path (AD-I/AD-J).

No model anywhere in this file, which is the point: a tap already carries the
answer, so it must never reach one. Two properties matter more than the happy
path and are asserted explicitly:

  - the agent's MEMORY matches reality afterwards (otherwise the next turn
    re-offers a slot that is gone — the agent contradicting the world, which is
    what rule 2 exists to prevent);
  - the AUDIT says a PERSON did it (actor=customer), not the agent. The console
    is the screen built to prove honesty; crediting Aisha with the customer's
    decision there would be a rule-15/16 failure in the one place it shows.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.db import get_db
from app.main import app
from app.models import Actor, AuditEvent, AuditType, Slot, SlotStatus

SESSION = "tap-session"


@pytest.fixture()
def checkpoint(tmp_path, monkeypatch) -> str:
    """Point the route at a THROWAWAY checkpoint file.

    Without this the tests write into ./agent_checkpoints.db — the real dev
    store — leaving a browser holding that session_id rehydrating an empty
    visible thread while the agent silently remembers a test conversation. That
    is precisely the stale-checkpoint failure CLAUDE.md warns reset-demo exists
    to prevent, so the suite must not be able to cause it.
    """
    path = str(tmp_path / "checkpoints.db")
    monkeypatch.setattr("app.api.intents.AGENT_CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _open_slot(db) -> Slot:
    slot = db.scalars(select(Slot).where(Slot.status == SlotStatus.open).limit(1)).first()
    assert slot is not None, "fixture must generate an open slot"
    return slot


def test_tap_locks_the_slot_without_a_model(db, client, slots, checkpoint):
    slot = _open_slot(db)
    r = client.post("/api/intent/lock-slot", json={"session_id": SESSION, "slot_id": slot.id})

    assert r.status_code == 200
    assert r.json()["locked"] is True
    db.expire_all()
    assert db.get(Slot, slot.id).status == SlotStatus.locked


def test_tap_is_audited_as_the_customer_not_the_agent(db, client, slots, checkpoint):
    """The agent path stamps actor=agent at the tool_call choke point. A tap is a
    PERSON acting, and the audit console must say so."""
    slot = _open_slot(db)
    client.post("/api/intent/lock-slot", json={"session_id": SESSION, "slot_id": slot.id})

    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.tool_call
        )
    ).all()
    assert rows, "the tap must appear in the audit trail (rule 15, FR-AD-02)"
    assert all(r.actor is Actor.customer for r in rows)
    assert rows[0].payload["tool"] == "lock_slot"
    assert rows[0].payload["via"] == "tap"


def test_the_agent_can_see_the_tap_on_the_next_turn(db, client, slots, checkpoint):
    """The load-bearing test. Without the synthetic pair the agent would re-offer
    a slot it no longer has."""
    slot = _open_slot(db)
    client.post("/api/intent/lock-slot", json={"session_id": SESSION, "slot_id": slot.id})

    seen: list = []

    def spy(messages, *, model, system, tools, config=None):
        seen.extend(messages)
        return {"role": "assistant", "content": [{"type": "text", "text": "Locked for you."}]}

    # The SAME store the route wrote to — a fresh checkpointer would see nothing
    # and the assertion below would fail for the wrong reason.
    graph = build_graph(new_sqlite_checkpointer(checkpoint))
    graph.invoke(
        {"messages": [{"role": "user", "content": "is that booked?"}], "iterations": 0,
         "customer_id": None},
        build_config(db, SESSION, generate=spy),
    )

    tool_uses = [
        b
        for m in seen
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert any(b["name"] == "lock_slot" for b in tool_uses), (
        "the tap must be in the agent's history, or it will contradict reality"
    )

    results = [
        b
        for m in seen
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert results and json.loads(results[0]["content"])["locked"] is True


def test_synthetic_pair_is_identifiable_as_a_tap(db, client, slots, checkpoint):
    """The pair records a call the model never made — a deliberate trade. It must
    at least be traceable: the tool_use id is the tap's correlation_id, so the
    checkpoint entry and the audit row point at each other."""
    slot = _open_slot(db)
    client.post("/api/intent/lock-slot", json={"session_id": SESSION, "slot_id": slot.id})

    row = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.tool_call
        )
    ).first()
    correlation_id = row.payload["correlation_id"]

    graph = build_graph(new_sqlite_checkpointer(checkpoint))
    state = graph.get_state({"configurable": {"thread_id": SESSION}})
    ids = [
        b["id"]
        for m in state.values["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert correlation_id in ids


def test_licence_ack_needs_no_arguments(db, client, slots, checkpoint):
    r = client.post("/api/intent/licence-ack", json={"session_id": SESSION})
    assert r.status_code == 200


def test_consent_refusal_is_recorded_not_rejected(db, client, slots, checkpoint):
    """A "no" is a first-class answer (FR-CF-05) — recorded, never an error, and
    it must never block the booking. Without an identified customer the tool's
    own envelope comes back, exactly as it would on the agent path."""
    r = client.post("/api/intent/consent", json={"session_id": SESSION, "whatsapp": False})
    assert r.status_code == 200
    assert "error" in r.json() or r.json().get("messaging") is False


def test_a_slot_tap_establishes_the_car_in_the_facts(db, client, slots, checkpoint):
    """tool_node folds the locked slot's car into session_facts when the model
    calls lock_slot; a tap must do the same, or the 'established' vehicle stays
    whatever an earlier search led with (found live, 2026-09-03)."""
    slot = _open_slot(db)
    graph = build_graph(new_sqlite_checkpointer(checkpoint))
    config = {"configurable": {"thread_id": SESSION}}
    graph.update_state(config, {"session_facts": {"vehicle_id": slot.vehicle_id + 100, "name": "Ali"}})
    r = client.post("/api/intent/lock-slot", json={"session_id": SESSION, "slot_id": slot.id})
    assert r.json()["locked"] is True
    facts = graph.get_state(config).values["session_facts"]
    assert facts["vehicle_id"] == slot.vehicle_id
    assert facts["name"] == "Ali", "other facts survive the fold"
    assert facts.get("daypart")
