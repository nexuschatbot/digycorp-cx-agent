"""Consent tests (FR-CF-05, PDPL).

The three properties under test: the axes are independent, withdrawal works, and
every change in both directions is audited.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, AuditType
from app.services import consent, customers

SESSION = "sess-consent-1"


def _customer(db):
    return customers.find_or_create(db, mobile="+971501234567")["customer"]["customer_id"]


def _consent_events(db) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.consent).order_by(AuditEvent.id)).all()
    )


# --------------------------------------------------------------------------- #
# The two axes are SEPARATE decisions
# --------------------------------------------------------------------------- #
def test_messaging_yes_memory_no(db):
    """The common real case: happy to be messaged, not happy to be remembered."""
    cid = _customer(db)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=True, memory=False)
    assert out["consent_messaging"] is True
    assert out["consent_memory"] is False


def test_memory_yes_messaging_no(db):
    cid = _customer(db)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=False, memory=True)
    assert out["consent_messaging"] is False
    assert out["consent_memory"] is True


def test_granting_one_axis_never_touches_the_other(db):
    """Neither may imply the other — they are different questions."""
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    out = consent.record(db, session_id=SESSION, customer_id=cid, memory=True)
    assert out["consent_messaging"] is True  # untouched by the memory call

    consent.record(db, session_id=SESSION, customer_id=cid, memory=False)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    assert out["consent_memory"] is False, "withdrawing memory must not be undone by messaging"


def test_omitted_axis_is_left_alone(db):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True, memory=True)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=False)
    assert out["consent_memory"] is True
    assert out["recorded"] == [{"axis": "messaging", "granted": False, "was": True}]


# --------------------------------------------------------------------------- #
# Consent can be WITHDRAWN — false is a decision, not a no-op
# --------------------------------------------------------------------------- #
def test_withdrawal_is_recorded_not_ignored(db):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True, memory=True)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=False, memory=False)
    assert out["consent_messaging"] is False
    assert out["consent_memory"] is False


def test_withdrawal_nulls_the_granted_at_column(db):
    """The column means "when consent was GRANTED". With no grant, there is no
    grant time — the audit event carries when it was withdrawn.
    """
    from app.models import Customer

    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True, memory=True)
    c = db.get(Customer, cid)
    assert c.whatsapp_consent_at is not None and c.memory_consent_at is not None

    consent.record(db, session_id=SESSION, customer_id=cid, messaging=False, memory=False)
    db.refresh(c)
    assert c.whatsapp_consent_at is None
    assert c.memory_consent_at is None


def test_grant_sets_a_timestamp(db):
    from app.models import Customer

    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    assert db.get(Customer, cid).whatsapp_consent_at is not None


def test_withdrawal_then_regrant_works(db):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=False)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    assert out["consent_messaging"] is True


# --------------------------------------------------------------------------- #
# Audit in BOTH directions — the PDPL evidence trail (FR-AD-02)
# --------------------------------------------------------------------------- #
def test_grant_is_audited(db):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    events = _consent_events(db)
    assert len(events) == 1
    assert events[0].payload["axis"] == "messaging"
    assert events[0].payload["granted"] is True
    assert events[0].session == SESSION


def test_withdrawal_is_audited_and_flagged_as_a_withdrawal(db):
    """A withdrawal that leaves no trace is indistinguishable from one that
    never happened.
    """
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=False)

    events = _consent_events(db)
    assert len(events) == 2
    last = events[-1].payload
    assert last["granted"] is False
    assert last["withdrawal"] is True
    assert last["changed_from"] is True and last["changed_to"] is False


def test_each_axis_gets_its_own_audit_event(db):
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True, memory=False)
    assert {e.payload["axis"] for e in _consent_events(db)} == {"messaging", "memory"}


def test_reconfirmation_is_still_audited(db):
    """"They confirmed again" is a real event; a re-confirmation we failed to
    log looks identical to never having asked.
    """
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    consent.record(db, session_id=SESSION, customer_id=cid, messaging=True)
    events = _consent_events(db)
    assert len(events) == 2
    assert events[-1].payload["withdrawal"] is False


# --------------------------------------------------------------------------- #
# Errors as data
# --------------------------------------------------------------------------- #
def test_unknown_customer_is_an_error_not_a_crash(db):
    out = consent.record(db, session_id=SESSION, customer_id=9999, messaging=True)
    assert out["error"] == "customer_not_found"


def test_no_axis_supplied_is_an_error_not_a_silent_noop(db):
    cid = _customer(db)
    out = consent.record(db, session_id=SESSION, customer_id=cid)
    assert out["error"] == "no_consent_specified"
    assert _consent_events(db) == []


def test_unreadable_value_is_not_treated_as_consent(db):
    """Consent is the one field where guessing the customer's answer is
    unacceptable — an unreadable value is an omission, not a yes.
    """
    from app.models import Customer

    cid = _customer(db)
    out = consent.record(db, session_id=SESSION, customer_id=cid, messaging="maybe")
    assert out["error"] == "no_consent_specified"
    assert db.get(Customer, cid).whatsapp_consent is False
    assert _consent_events(db) == []
