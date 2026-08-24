"""WhatsApp INBOUND handoff — identify, mint a link, redeem into a bound session.

DB-ONLY. WHATSAPP_ENABLED is never set, so services/whatsapp.send_text returns
`skipped` — no network, no Meta call, the same discipline as test_dispatcher. The
things worth asserting are the ones a live demo depends on and a refactor could
silently break:

  - an inbound message from a KNOWN number is identified, its single active
    booking resolved, and a token minted (the reply names the customer; it never
    quotes booking contents — grounding stays on the web);
  - the webhook is idempotent per Meta message id (Meta retries until it 200s);
  - the signature gate rejects a forged body when an app secret is set;
  - redeeming BINDS the fresh web session to the customer in the agent checkpoint
    (so Aisha resumes already identified) and is strictly SINGLE-USE;
  - an unknown number still gets a link, binding no one.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config as app_config
from app.agent import build_graph, new_sqlite_checkpointer
from app.db import get_db
from app.main import app
from app.models import AuditEvent, AuditType, HandoffToken, Slot, SlotStatus
from app.services import booking as booking_service
from app.services import consent as consent_service
from app.services import customers as customers_service
from app.services import slots as slots_service
from app.services import whatsapp_inbound

# A Pakistani test number, digits-only as Meta delivers `from` (no leading '+').
PK_MSISDN = "923409297758"
PK_MOBILE = "+923409297758"


@pytest.fixture()
def wa_env(tmp_path, monkeypatch) -> str:
    """Turn the inbound channel on, relax the UAE-only mobile rule so +92
    resolves, and point the checkpoint bind at a THROWAWAY file (never the real
    ./agent_checkpoints.db).

    WHATSAPP_ENABLED is forced OFF here even though the dev .env sets it on — the
    suite must NEVER make a live Meta call (a real message to a real phone). With
    it off, services/whatsapp.send_text short-circuits to `skipped` before any
    network I/O, which is the whole reason the send path is optional."""
    monkeypatch.setattr(app_config, "WHATSAPP_ENABLED", False)
    monkeypatch.setattr(app_config, "WHATSAPP_INBOUND_ENABLED", True)
    monkeypatch.setattr(app_config, "STRICT_UAE_MOBILE", False)
    monkeypatch.setattr(app_config, "PUBLIC_WEB_BASE_URL", "https://demo.example")
    path = str(tmp_path / "checkpoints.db")
    monkeypatch.setattr("app.api.whatsapp.AGENT_CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _customer_with_booking(db) -> tuple[int, str]:
    """A +92 customer (consented) with one active booking. Returns (cid, ref)."""
    cid = customers_service.find_or_create(db, mobile=PK_MOBILE, name="Hanif")["customer"]["customer_id"]
    consent_service.record(db, session_id="seed", customer_id=cid, messaging=True)
    slot = db.scalars(select(Slot).where(Slot.status == SlotStatus.open).order_by(Slot.start)).first()
    slots_service.lock(db, session_id="seed", slot_id=slot.id)
    slots_service.acknowledge_licence_reminder(db, session_id="seed")
    out = booking_service.create(
        db, session_id="seed", customer_id=cid,
        vehicle_id=slot.vehicle_id, slot_id=slot.id, lang="en",
    )
    db.commit()
    return cid, out["booking"]["booking_ref"]


def test_inbound_known_number_mints_a_token_and_resolves_the_booking(db, slots, wa_env):
    cid, ref = _customer_with_booking(db)
    result = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn=PK_MSISDN, text="I'd like to change my booking time", wamid="wamid.1",
    )
    assert result["ok"] is True
    assert result["identified"] is True
    assert result["intent"] == "reschedule"
    assert result["booking_ref"] == ref
    # No network: the send was skipped because WHATSAPP_ENABLED is off.
    assert result["send"].get("skipped") is True

    row = db.scalar(select(HandoffToken).where(HandoffToken.token == result["token"]))
    assert row is not None and row.customer_id == cid and row.booking_ref == ref


def test_reply_names_the_customer_but_never_quotes_the_booking(db, slots, wa_env):
    """The WhatsApp text may greet by name; every concrete fact stays for the web
    agent (rule 2). So the reply carries the link and NOT the slot/date/price."""
    _cid, ref = _customer_with_booking(db)
    booking = booking_service.find(db, ref=ref)["bookings"][0]
    body = whatsapp_inbound.compose_reply(
        link="https://demo.example/?h=tok", intent="reschedule", lang="en", name="Hanif",
    )
    assert "Hanif" in body and "https://demo.example/?h=tok" in body
    assert ref not in body
    assert booking["start_iso"][:10] not in body  # no date leaked


def test_webhook_is_idempotent_per_message_id(db, slots, wa_env, client):
    _customer_with_booking(db)
    payload = _meta_payload(PK_MSISDN, "change my booking", "wamid.dup")
    r1 = client.post("/api/whatsapp/webhook", json=payload)
    r2 = client.post("/api/whatsapp/webhook", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    tokens = db.scalars(select(HandoffToken).where(HandoffToken.wamid == "wamid.dup")).all()
    assert len(tokens) == 1, "a retried delivery must not mint a second token"


def test_webhook_rejects_a_forged_signature(db, slots, wa_env, client, monkeypatch):
    monkeypatch.setattr(app_config, "WHATSAPP_APP_SECRET", "s3cr3t")
    _customer_with_booking(db)
    payload = _meta_payload(PK_MSISDN, "change my booking", "wamid.sig")
    raw = json.dumps(payload).encode()

    bad = client.post("/api/whatsapp/webhook", content=raw,
                      headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert bad.json()["ok"] is False
    assert db.scalar(select(HandoffToken).where(HandoffToken.wamid == "wamid.sig")) is None

    good_sig = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
    ok = client.post("/api/whatsapp/webhook", content=raw,
                     headers={"X-Hub-Signature-256": f"sha256={good_sig}",
                              "Content-Type": "application/json"})
    assert ok.json()["ok"] is True
    assert db.scalar(select(HandoffToken).where(HandoffToken.wamid == "wamid.sig")) is not None


def test_redeem_binds_the_session_and_is_single_use(db, slots, wa_env, client):
    cid, ref = _customer_with_booking(db)
    minted = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn=PK_MSISDN, text="change my booking", wamid="wamid.redeem",
    )
    token = minted["token"]

    r = client.post("/api/whatsapp/handoff/redeem", json={"token": token})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    session_id = data["session_id"]
    # The greeting directive names the customer and the booking ref for get_booking.
    assert "Hanif" in data["kickoff"] and ref in data["kickoff"]

    # The checkpoint the web agent will resume from is bound to this customer.
    graph = build_graph(new_sqlite_checkpointer(wa_env))
    state = graph.get_state({"configurable": {"thread_id": session_id}}).values
    assert state.get("customer_id") == cid

    # Single-use: a second open of the same link fails, cleanly (200 + ok:false).
    again = client.post("/api/whatsapp/handoff/redeem", json={"token": token})
    assert again.status_code == 200 and again.json()["ok"] is False
    assert again.json()["reason"] == "already_redeemed"


def test_redeem_of_a_bad_token_degrades_to_start_fresh(db, slots, wa_env, client):
    r = client.post("/api/whatsapp/handoff/redeem", json={"token": "nope"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["reason"] == "invalid_token"


def test_unknown_number_still_gets_a_link_binding_no_one(db, slots, wa_env, client):
    result = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn="447700900123", text="hello", wamid="wamid.unknown",
    )
    assert result["ok"] is True and result["identified"] is False
    r = client.post("/api/whatsapp/handoff/redeem", json={"token": result["token"]})
    data = r.json()
    assert data["ok"] is True
    graph = build_graph(new_sqlite_checkpointer(wa_env))
    state = graph.get_state({"configurable": {"thread_id": data["session_id"]}}).values
    assert not state.get("customer_id")  # nobody bound


def test_inbound_is_audited_as_the_customer(db, slots, wa_env):
    _customer_with_booking(db)
    out = whatsapp_inbound.handle_inbound_text(
        db, from_msisdn=PK_MSISDN, text="change my booking", wamid="wamid.audit",
    )
    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.session == f"wa:{out['token']}")
    ).all()
    assert rows and rows[0].type is AuditType.intent
    assert rows[0].payload["via"] == "whatsapp_inbound"


def test_webhook_get_verifies_only_with_the_right_token(client, monkeypatch):
    monkeypatch.setattr(app_config, "WHATSAPP_VERIFY_TOKEN", "verify-me")
    ok = client.get("/api/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345",
    })
    assert ok.status_code == 200 and ok.text == "12345"

    bad = client.get("/api/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345",
    })
    assert bad.status_code == 403


def _meta_payload(frm: str, text: str, wamid: str) -> dict:
    """A minimal Meta `messages` webhook payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "0",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1253912171136258"},
                    "contacts": [{"wa_id": frm, "profile": {"name": "Hanif"}}],
                    "messages": [{
                        "from": frm, "id": wamid, "timestamp": "1700000000",
                        "type": "text", "text": {"body": text},
                    }],
                },
            }],
        }],
    }
