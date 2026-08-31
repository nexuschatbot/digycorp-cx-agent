"""Escalation stub tests (rule 13, FR-ES) — all DB-only, no model calls.

Three things under test: the service logs a handoff as an `escalation` audit row
(with dedupe), the tool node injects the customer_id / refuses an unidentified
session / forwards the correlation id, and the console dashboard counts real
escalations instead of the old hardcoded zero.
"""

from __future__ import annotations

from sqlalchemy import select

from app.agent import build_config
from app.agent.tool_node import tool_node
from app.models import Actor, AuditEvent, AuditType, ConversationTurn, Speaker
from app.services import console, customers
from app.services import escalation as escalation_service

SESSION = "sess-escalation-1"


def _customer(db, mobile: str = "+971501234567") -> int:
    return customers.find_or_create(db, mobile=mobile)["customer"]["customer_id"]


def _escalation_rows(db, session: str | None = None) -> list[AuditEvent]:
    q = select(AuditEvent).where(AuditEvent.type == AuditType.escalation)
    if session is not None:
        q = q.where(AuditEvent.session == session)
    return list(db.scalars(q.order_by(AuditEvent.id)).all())


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #
def test_create_callback_logs_an_escalation_row(db):
    cid = _customer(db)
    out = escalation_service.create_callback(
        db, session_id=SESSION, customer_id=cid, reason="price_negotiation",
        topic="discount on the T2 i-DM",
    )
    assert out["logged"] is True
    assert out["already_logged"] is False
    assert out["reason"] == "price_negotiation"
    assert out["helpline"] == "800-535483"

    rows = _escalation_rows(db, SESSION)
    assert len(rows) == 1
    assert rows[0].actor == Actor.agent
    assert rows[0].payload["reason"] == "price_negotiation"
    assert rows[0].payload["topic"] == "discount on the T2 i-DM"
    assert rows[0].payload["customer_id"] == cid


def test_unknown_reason_is_coerced_to_other(db):
    """A logged handoff with a fuzzy reason beats a lost one (rule 13)."""
    cid = _customer(db)
    out = escalation_service.create_callback(
        db, session_id=SESSION, customer_id=cid, reason="haggling",
    )
    assert out["reason"] == "other"
    assert _escalation_rows(db, SESSION)[0].payload["reason"] == "other"


def test_idempotent_per_session_and_reason(db):
    """Two calls for the same reason in one conversation are ONE handoff."""
    cid = _customer(db)
    first = escalation_service.create_callback(
        db, session_id=SESSION, customer_id=cid, reason="complaint",
    )
    second = escalation_service.create_callback(
        db, session_id=SESSION, customer_id=cid, reason="complaint",
    )
    assert first["already_logged"] is False
    assert second["already_logged"] is True
    assert len(_escalation_rows(db, SESSION)) == 1


def test_a_different_reason_logs_again(db):
    """A genuinely new reason in the same conversation is a new escalation."""
    cid = _customer(db)
    escalation_service.create_callback(db, session_id=SESSION, customer_id=cid, reason="complaint")
    escalation_service.create_callback(db, session_id=SESSION, customer_id=cid, reason="finance")
    assert len(_escalation_rows(db, SESSION)) == 2


def test_empty_topic_stored_as_none(db):
    cid = _customer(db)
    escalation_service.create_callback(
        db, session_id=SESSION, customer_id=cid, reason="explicit_request", topic="   ",
    )
    assert _escalation_rows(db, SESSION)[0].payload["topic"] is None


# --------------------------------------------------------------------------- #
# Tool node: injection, refusal, correlation
# --------------------------------------------------------------------------- #
def _tool_use_state(customer_id, /, **inp) -> dict:
    return {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "e1", "name": "request_human_callback", "input": inp}
                ],
            }
        ],
        "customer_id": customer_id,
        "closed": False,
    }


def test_tool_node_injects_customer_id_and_correlates(db):
    """The id is injected from the session binding, and the escalation row shares
    the tool_call's correlation id (rule 15) — one action, two facets."""
    cid = _customer(db)
    config = build_config(db, SESSION)
    tool_node(_tool_use_state(cid, reason="explicit_request"), config)

    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.session == SESSION).order_by(AuditEvent.id)
    ).all()
    call = next(r for r in rows if r.type == AuditType.tool_call)
    esc = next(r for r in rows if r.type == AuditType.escalation)
    assert call.payload["tool"] == "request_human_callback"
    assert esc.payload["customer_id"] == cid  # injected, not from the model
    assert call.payload["correlation_id"] == esc.payload["correlation_id"]


def test_tool_node_refuses_an_unidentified_session(db):
    """No customer bound -> refused as DATA, and nothing is logged as a handoff."""
    config = build_config(db, SESSION)
    out = tool_node(_tool_use_state(None, reason="explicit_request"), config)

    import json

    result = json.loads(out["messages"][0]["content"][0]["content"])
    assert result["error"] == "customer_not_identified"
    assert _escalation_rows(db, SESSION) == []


# --------------------------------------------------------------------------- #
# Console dashboard
# --------------------------------------------------------------------------- #
def _enquiry(db, session: str) -> None:
    """A customer turn makes the session count as one enquiry."""
    db.add(ConversationTurn(session_id=session, speaker=Speaker.customer, lang="en", text="hi"))
    db.flush()


def test_dashboard_counts_escalations_and_reasons(db):
    """The handoffs block is measured now, not a hardcoded zero."""
    c1, _ = _customer(db, "+971501111111"), _customer(db, "+971502222222")
    # Two enquiries, one handed off (twice, same reason -> still one).
    _enquiry(db, "conv-a")
    _enquiry(db, "conv-b")
    escalation_service.create_callback(db, session_id="conv-a", customer_id=c1, reason="finance")
    escalation_service.create_callback(db, session_id="conv-a", customer_id=c1, reason="finance")

    out = console.dashboard(db, window="all")["handoffs"]
    assert out["count"] == 1  # distinct sessions escalated
    assert out["resolved_without_human"] == 1  # conv-b
    assert out["pct"] == 50.0
    assert out["resolved_pct"] == 50.0
    assert out["reasons"] == [{"reason": "finance", "count": 1}]


def test_dashboard_handoffs_true_zero_when_none(db):
    _enquiry(db, "conv-a")
    out = console.dashboard(db, window="all")["handoffs"]
    assert out["count"] == 0
    assert out["resolved_without_human"] == 1
    assert out["resolved_pct"] == 100.0
    assert out["reasons"] == []
