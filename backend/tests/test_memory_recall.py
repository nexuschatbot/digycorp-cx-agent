"""Memory through the real agent graph (S-07, S-08, rule 12).

Deterministic and API-key-free: a ScriptedModel replays canned assistant turns
via the injectable `generate` hook, so the graph, the tool node, the injection
wiring and the real service guards are all production code — only the model is
scripted. Same pattern as tests/test_agent_graph.py.

What these cover that tests/test_memory.py cannot: that memory actually REACHES
the model (and in the right shape), that customer_id is injected rather than
declared, and that the forget handshake survives a round trip through the graph.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.models import AuditEvent, AuditType, MemoryProfile
from app.services import consent, customers, memory
from app.tools.memory_tools import FORGET_ME_SCHEMA, SAVE_MEMORY_SCHEMA

MOBILE = "+971505550103"  # Ravi — the seeded returning customer (S-07)
FRESH_MOBILE = "+971509998877"


class ScriptedModel:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.i = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        msg = self.steps[self.i]
        self.i += 1
        return msg


def _tool_use(tid: str, tool: str, /, **inp) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}],
    }


def _text(s: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": s}]}


def _run(db, session_id: str, steps: list[dict]) -> dict:
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=ScriptedModel(steps))
    return graph.invoke(
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "iterations": 0,
            "customer_id": None,
        },
        config,
    )


def _tool_results_for(final: dict, tool_name: str, steps: list[dict]) -> list[dict]:
    """Every tool_result for a tool, in call order (forget_me is called twice)."""
    tids = [
        b["id"]
        for s in steps
        for b in s["content"]
        if b.get("type") == "tool_use" and b["name"] == tool_name
    ]
    by_tid: dict[str, dict] = {}
    for m in final["messages"]:
        content = getattr(m, "content", None) or (
            m.get("content") if isinstance(m, dict) else None
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("tool_use_id") in tids:
                by_tid[block["tool_use_id"]] = json.loads(block["content"])
    if not by_tid:
        raise AssertionError(f"no tool_result found for {tool_name}")
    return [by_tid[t] for t in tids if t in by_tid]


def _tool_result_for(final: dict, tool_name: str, steps: list[dict]) -> dict:
    return _tool_results_for(final, tool_name, steps)[0]


def _seed_ravi(db) -> int:
    """A consented customer with something worth recalling."""
    cid = customers.find_or_create(db, mobile=MOBILE)["customer"]["customer_id"]
    consent.record(db, session_id="seed", customer_id=cid, memory=True)
    db.add(
        MemoryProfile(
            customer_id=cid,
            preferred_models=[2],
            preferred_showroom_id=3,
            preferred_daypart="evening",
            notes="Prefers a quick call before arriving.",
        )
    )
    db.flush()
    return cid


# --------------------------------------------------------------------------- #
# S-07 — a returning customer's memory reaches the model
# --------------------------------------------------------------------------- #
def test_recall_reaches_the_model_as_a_sibling_of_customer(db):
    _seed_ravi(db)
    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _text("Good to hear from you. What would you like to see?"),
    ]
    final = _run(db, "sess-recall-1", steps)

    result = _tool_result_for(final, "find_or_create_customer", steps)
    assert "memory" in result, "a consented profile must reach the model"
    assert "memory" not in result["customer"], "two gates must not hide behind one key"
    assert result["memory"]["preferred_showroom"]["name"] == "Jetour Deira"
    assert result["memory"]["preferred_daypart"] == "evening"
    assert result["memory"]["preferred_models"][0]["name"] == "Jetour T2 Luxury Plus"


def test_a_new_customer_gets_no_memory_key_at_all(db):
    steps = [
        # A name rides along — the agent tool refuses to CREATE nameless
        # (the 2026-07-31 nameless-booking fix).
        _tool_use("t1", "find_or_create_customer", mobile=FRESH_MOBILE, name="Noor Khan"),
        _text("Welcome. What can I show you?"),
    ]
    final = _run(db, "sess-recall-2", steps)

    result = _tool_result_for(final, "find_or_create_customer", steps)
    assert "memory" not in result
    assert result["created"] is True


def test_an_unconsented_customer_recalls_nothing_through_the_graph(db):
    """Rule 12's negative case, end to end."""
    cid = customers.find_or_create(db, mobile=MOBILE)["customer"]["customer_id"]
    db.add(MemoryProfile(customer_id=cid, preferred_daypart="evening"))
    db.flush()

    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _text("How can I help?"),
    ]
    final = _run(db, "sess-recall-3", steps)

    assert "memory" not in _tool_result_for(final, "find_or_create_customer", steps)


# --------------------------------------------------------------------------- #
# S-08 — the forget handshake, through the graph
# --------------------------------------------------------------------------- #
def test_an_unconfirmed_forget_deletes_nothing_through_the_graph(db):
    cid = _seed_ravi(db)
    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _tool_use("t2", "forget_me"),
        _text("We have your usual showroom and evening times. Shall I clear all of it?"),
    ]
    final = _run(db, "sess-forget-1", steps)

    first = _tool_result_for(final, "forget_me", steps)
    assert first["confirmation_required"] is True
    assert first["has_profile"] is True
    assert first["summary"]["preferred_daypart"] == "evening", "she needs this to ask in words"
    assert db.scalar(
        select(MemoryProfile).where(MemoryProfile.customer_id == cid)
    ) is not None, "the unconfirmed call must not delete"
    assert (
        list(db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.erasure)).all())
        == []
    ), "nothing was erased, so nothing may be logged"


def test_the_two_call_forget_handshake(db):
    """Both calls in ONE conversation — the customer answers the question and the
    model calls again. A fresh session would have no identified customer, which is
    the point of injecting it rather than letting the model carry it."""
    cid = _seed_ravi(db)
    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _tool_use("t2", "forget_me"),
        _tool_use("t3", "forget_me", confirmed=True),
        _text("All cleared. I won't keep anything about you."),
    ]
    final = _run(db, "sess-forget-2", steps)

    unconfirmed, confirmed = _tool_results_for(final, "forget_me", steps)
    assert unconfirmed["forgotten"] is False
    # THE UNCONFIRMED CALL NOW CARRIES THE SCOPE (2026-07-23). It is what Aisha
    # reads out before asking, and erasure takes the customer's bookings — so
    # this key existing is what stands between a customer agreeing to lose their
    # preferences and unknowingly losing Saturday's test drive.
    assert "would_delete" in unconfirmed

    # A to Z: the envelope reports a per-table breakdown instead of a single
    # `deleted` bool. Asserted by shape, not by literal equality — see the note
    # on test_a_confirmed_forget_hard_deletes.
    assert confirmed["forgotten"] is True
    assert confirmed["scope"] == "full"
    assert confirmed["customer_id"] == cid
    assert db.scalar(select(MemoryProfile).where(MemoryProfile.customer_id == cid)) is None
    from app.models import Customer

    assert db.get(Customer, cid) is None, "the customer row goes too"


def test_forget_writes_a_correlated_erasure_row(db):
    """S-08: the erasure is visible in the audit trail and grouped with its call."""
    _seed_ravi(db)
    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _tool_use("t2", "forget_me", confirmed=True),
        _text("Done."),
    ]
    _run(db, "sess-forget-3", steps)

    erasures = list(
        db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.erasure)).all()
    )
    assert len(erasures) == 1
    erasure = erasures[0]
    assert erasure.actor.value == "customer"
    # `deleted` (a bool about the profile) became `scope` + a `removed`
    # breakdown when erasure went A to Z on 2026-07-23.
    assert erasure.payload["scope"] == "full"

    # THE ERASURE DELETES ITS OWN CHOKE-POINT ROW, and this test now pins that
    # rather than the correlation it used to check.
    #
    # The tool node audits every call before dispatching it, so a `tool_call` row
    # for THIS forget_me existed — and then the purge swept the session's audit
    # history, itself included. A to Z means the deletion cannot spare the record
    # of being asked to delete. The receipt survives only because it is written
    # AFTER the sweep.
    #
    # ⚠️ THE CONSEQUENCE, recorded here because it is easy to miss: the receipt
    # still CARRIES a correlation_id, but its sibling is gone, so the id now
    # points at nothing. Correlation across the trail (rule 15) does not survive
    # an erasure — by design, but it is a real hole and not a bug to be "fixed"
    # by quietly exempting forget_me from its own purge.
    surviving = list(db.scalars(select(AuditEvent)).all())
    assert [e.type for e in surviving] == [AuditType.erasure], (
        "A to Z: nothing but the receipt may outlive the erasure"
    )
    assert erasure.payload.get("correlation_id"), (
        "the receipt still carries the correlation id it was written with — "
        "there is just no longer a row on the other end of it"
    )


def test_forget_before_identification_is_data_not_a_crash(db):
    """"Forget me" before we know who they are is a real conversational state."""
    steps = [
        _tool_use("t1", "forget_me", confirmed=True),
        _text("Of course — could I take your mobile number first?"),
    ]
    final = _run(db, "sess-forget-4", steps)

    result = _tool_result_for(final, "forget_me", steps)
    assert result["error"] == "customer_not_identified"
    assert final["messages"], "the turn must survive"


# --------------------------------------------------------------------------- #
# save_memory through the graph
# --------------------------------------------------------------------------- #
def test_save_memory_writes_notes_via_the_graph(db):
    cid = customers.find_or_create(db, mobile=MOBILE)["customer"]["customer_id"]
    consent.record(db, session_id="seed", customer_id=cid, memory=True)

    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE),
        _tool_use("t2", "save_memory", notes="Prefers a female consultant."),
        _text("Noted."),
    ]
    final = _run(db, "sess-save-1", steps)

    assert _tool_result_for(final, "save_memory", steps)["saved"] is True
    profile = db.scalar(select(MemoryProfile).where(MemoryProfile.customer_id == cid))
    assert profile.notes == "Prefers a female consultant."


def test_save_memory_without_consent_is_a_normal_answer(db):
    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=MOBILE, name="Noor Khan"),
        _tool_use("t2", "save_memory", notes="Likes evenings."),
        _text("Understood."),
    ]
    final = _run(db, "sess-save-2", steps)

    result = _tool_result_for(final, "save_memory", steps)
    assert result["error"] == "memory_consent_not_granted"
    assert db.scalar(select(MemoryProfile)) is None


# --------------------------------------------------------------------------- #
# Trusted context (the guard that makes the rest enforceable)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("schema", [SAVE_MEMORY_SCHEMA, FORGET_ME_SCHEMA])
def test_customer_id_is_injected_never_declared(schema):
    """A model-supplied customer_id on forget_me would let a sentence in the chat
    window choose whose data gets erased."""
    assert "customer_id" not in schema["input_schema"]["properties"]
    assert "customer_id" not in schema["input_schema"].get("required", [])


def test_a_model_supplied_customer_id_is_ignored(db):
    """Even if the model invents one, the injected identity wins."""
    victim = customers.find_or_create(db, mobile="+971505550101")["customer"]["customer_id"]
    consent.record(db, session_id="seed", customer_id=victim, memory=True)
    db.add(MemoryProfile(customer_id=victim, preferred_daypart="morning"))
    db.flush()

    steps = [
        _tool_use("t1", "find_or_create_customer", mobile=FRESH_MOBILE),
        _tool_use("t2", "forget_me", confirmed=True, customer_id=victim),
        _text("Done."),
    ]
    _run(db, "sess-inject-1", steps)

    assert db.scalar(
        select(MemoryProfile).where(MemoryProfile.customer_id == victim)
    ) is not None, "another customer's profile must be untouchable from the chat window"
