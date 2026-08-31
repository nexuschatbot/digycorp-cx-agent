"""Human takeover tests (2026-08-31) — service, agent gate, and the WS channel.

Four things under test: the service derives takeover state from the audit trail
(escalated-only, open-only, first admin wins), messages persist as ordinary
Speaker.staff / Speaker.customer transcript turns, the gate in run_turn refuses
the AI on a taken-over session before it writes or generates anything, and the
WebSocket endpoint relays frames between the two roles and closes through the
same service every other exit uses.

All DB-only and in-process — no model calls, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import runner as runner_module
from app.api import takeover as takeover_api
from app.models import Actor, AuditEvent, AuditType, ConversationTurn, Language, Speaker
from app.services import conversation as conversation_service
from app.services import console
from app.services import customers
from app.services import escalation as escalation_service
from app.services import takeover as takeover_service

SESSION = "sess-takeover-1"


def _customer(db, mobile: str = "+971501112233") -> int:
    return customers.find_or_create(db, mobile=mobile, name="Test Customer")[
        "customer"
    ]["customer_id"]


def _escalate(db, session: str = SESSION, reason: str = "explicit_request") -> int:
    """The precondition every takeover has: request_human_callback fired."""
    cid = _customer(db)
    escalation_service.create_callback(
        db, session_id=session, customer_id=cid, reason=reason, topic="talk to a person"
    )
    # An attributed turn, so append_turn has a customer_id to copy (T4.6).
    db.add(
        ConversationTurn(
            session_id=session,
            customer_id=cid,
            speaker=Speaker.customer,
            lang=Language.en,
            text="I want to speak to a human",
        )
    )
    db.commit()
    return cid


# --------------------------------------------------------------------------- #
# Service layer — state derived from the trail
# --------------------------------------------------------------------------- #
def test_start_refuses_a_session_nobody_escalated(db):
    out = takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    assert out == {"error": "not_escalated"}
    assert takeover_service.state(db, session_id=SESSION)["taken_over"] is False


def test_start_refuses_a_closed_session(db):
    _escalate(db)
    conversation_service.close(db, session_id=SESSION, actor=Actor.customer)
    db.commit()
    out = takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    assert out == {"error": "conversation_closed"}


def test_start_refuses_a_blank_name(db):
    _escalate(db)
    assert takeover_service.start(db, session_id=SESSION, admin_name="  ") == {
        "error": "admin_name_required"
    }


def test_start_writes_one_intent_row_and_state_reads_it_back(db):
    _escalate(db)
    out = takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    db.commit()
    assert out["taken_over"] is True
    assert out["already"] is False

    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.intent
        )
    ).all()
    marks = [
        r for r in rows
        if (r.payload or {}).get("action") == takeover_service.TAKEOVER_ACTION
    ]
    assert len(marks) == 1
    assert marks[0].actor == Actor.admin
    assert marks[0].payload["admin_name"] == "Sara"

    state = takeover_service.state(db, session_id=SESSION)
    assert state["taken_over"] is True
    assert state["admin_name"] == "Sara"


def test_first_admin_wins(db):
    _escalate(db)
    takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    second = takeover_service.start(db, session_id=SESSION, admin_name="Omar")
    assert second["already"] is True
    assert second["admin_name"] == "Sara"
    # No second row was stacked.
    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.intent
        )
    ).all()
    marks = [
        r for r in rows
        if (r.payload or {}).get("action") == takeover_service.TAKEOVER_ACTION
    ]
    assert len(marks) == 1


def test_append_turn_persists_a_staff_turn_with_the_sessions_customer(db):
    cid = _escalate(db)
    out = takeover_service.append_turn(
        db, session_id=SESSION, speaker=Speaker.staff, text="Hello, Sara here."
    )
    assert out["speaker"] == "staff"
    assert out["lang"] == "en"

    turn = db.get(ConversationTurn, out["turn_id"])
    assert turn.speaker == Speaker.staff
    assert turn.customer_id == cid  # the erasure edge survives the human half


def test_append_turn_detects_arabic(db):
    _escalate(db)
    out = takeover_service.append_turn(
        db, session_id=SESSION, speaker=Speaker.customer, text="مرحبا"
    )
    assert out["lang"] == "ar"


def test_append_turn_refuses_empty_and_agent_speaker(db):
    _escalate(db)
    assert takeover_service.append_turn(
        db, session_id=SESSION, speaker=Speaker.staff, text="   "
    ) == {"error": "empty_message"}
    assert takeover_service.append_turn(
        db, session_id=SESSION, speaker=Speaker.agent, text="hi"
    ) == {"error": "invalid_speaker"}


def test_history_carries_escalated_and_takeover(db):
    _escalate(db)
    before = conversation_service.history(db, session_id=SESSION)
    assert before["escalated"] is True
    assert before["takeover"]["taken_over"] is False

    takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    after = conversation_service.history(db, session_id=SESSION)
    assert after["takeover"]["taken_over"] is True
    assert after["takeover"]["admin_name"] == "Sara"


def test_handoff_queue_shows_who_holds_the_session(db):
    _escalate(db)
    takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    db.commit()
    rows = console.list_handoffs(db)["rows"]
    row = next(r for r in rows if r["session"] == SESSION)
    assert row["taken_over_by"] == "Sara"
    assert row["taken_over_ts"] is not None
    assert row["session_active"] is True


def test_handoff_queue_marks_a_closed_session_inactive(db):
    """A closed chat has nobody in it to talk to — the queue must say so, so the
    UI can withhold the live 'Take over' and leave the row callback-only."""
    _escalate(db)
    conversation_service.close(db, session_id=SESSION, actor=Actor.customer)
    db.commit()
    row = next(
        r for r in console.list_handoffs(db)["rows"] if r["session"] == SESSION
    )
    assert row["session_active"] is False


# --------------------------------------------------------------------------- #
# The agent gate — the AI never runs on a taken-over session
# --------------------------------------------------------------------------- #
def test_run_turn_refuses_a_taken_over_session(db):
    _escalate(db)
    takeover_service.start(db, session_id=SESSION, admin_name="Sara")
    db.commit()
    turns_before = db.scalars(select(ConversationTurn)).all()

    engine = db.get_bind()
    events: list[tuple[str, dict]] = []

    def _never_generate(*a, **k):  # the gate must fire before any model call
        raise AssertionError("the model was invoked on a taken-over session")

    import threading

    result = runner_module.run_turn(
        SESSION,
        "hello?",
        on_event=lambda e, d: events.append((e, d)),
        cancel=threading.Event(),
        generate=_never_generate,
        session_factory=lambda: Session(engine),
    )

    assert result.status == "refused"
    assert ("taken_over", {}) in events
    # Nothing was persisted — the WS path owns takeover messages.
    assert len(db.scalars(select(ConversationTurn)).all()) == len(turns_before)


# --------------------------------------------------------------------------- #
# The WebSocket channel
# --------------------------------------------------------------------------- #
# ONE PORTAL, DELIBERATELY. Outside a `with` block, Starlette's TestClient runs
# every websocket_connect on its OWN portal thread and event loop — and a
# broadcast from the admin's loop to a customer socket waiting on a different
# loop loses the wakeup (anyio memory streams are not cross-loop safe), so the
# multi-socket tests deadlock. Production has no such split: uvicorn runs every
# socket on one loop, which is the assumption the hub documents. Entering the
# context manager shares a single portal, restoring the production shape — but
# it also runs the app lifespan, and app.main's lifespan migrates the REAL
# database. So these tests run the takeover router on a bare FastAPI app: no
# lifespan, one portal, the endpoint under test unchanged.
@pytest.fixture()
def client(db, monkeypatch):
    from fastapi import FastAPI

    engine = db.get_bind()
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(takeover_api, "SessionLocal", factory)
    mini = FastAPI()
    mini.include_router(takeover_api.router)
    with TestClient(mini) as c:
        yield c


def _ws(client, role: str, name: str | None = None, session: str = SESSION):
    qs = f"role={role}" + (f"&name={name}" if name else "")
    return client.websocket_connect(f"/api/takeover/{session}?{qs}")


def test_ws_admin_refused_without_an_escalation(db, client):
    with _ws(client, "admin", "Sara") as ws:
        assert ws.receive_json() == {"type": "error", "error": "not_escalated"}


def test_ws_second_admin_told_who_holds_it(db, client):
    _escalate(db)
    with _ws(client, "admin", "Sara") as sara:
        assert sara.receive_json()["type"] == "state"
        with _ws(client, "admin", "Omar") as omar:
            frame = omar.receive_json()
            assert frame == {
                "type": "error", "error": "already_taken", "admin_name": "Sara"
            }


def test_ws_customer_message_refused_before_takeover(db, client):
    _escalate(db)
    with _ws(client, "customer") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "message", "text": "anyone there?"})
        assert ws.receive_json() == {"type": "error", "error": "not_taken_over"}


def test_ws_full_takeover_exchange_then_close(db, client):
    _escalate(db)
    with _ws(client, "customer") as cust:
        state = cust.receive_json()
        assert state["type"] == "state" and state["taken_over"] is False

        with _ws(client, "admin", "Sara") as admin:
            assert admin.receive_json()["type"] == "state"
            # Both sides hear the person join — the customer's listener
            # predates the takeover, which is exactly the case that matters.
            assert admin.receive_json()["type"] == "takeover_started"
            joined = cust.receive_json()
            assert joined == {"type": "takeover_started", "admin_name": "Sara"}

            # No echo to the sender — each client appends its own message
            # locally on send; the broadcast reaches only the other side.
            admin.send_json({"type": "message", "text": "Hi, Sara here — how can I help?"})
            staff_frame = cust.receive_json()
            assert staff_frame["type"] == "message"
            assert staff_frame["speaker"] == "staff"
            assert staff_frame["admin_name"] == "Sara"

            cust.send_json({"type": "message", "text": "About that discount..."})
            assert admin.receive_json()["speaker"] == "customer"

            # Both messages are ordinary transcript turns.
            db.expire_all()
            turns = db.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == SESSION)
                .order_by(ConversationTurn.id)
            ).all()
            assert [t.speaker for t in turns[-2:]] == [Speaker.staff, Speaker.customer]

            admin.send_json({"type": "close"})
            assert cust.receive_json() == {"type": "closed"}
            assert admin.receive_json() == {"type": "closed"}

            db.expire_all()
            assert conversation_service.is_closed(db, session_id=SESSION) is True

            # After the close, the channel refuses further messages as data.
            admin.send_json({"type": "message", "text": "too late"})
            assert admin.receive_json() == {
                "type": "error", "error": "conversation_closed"
            }
