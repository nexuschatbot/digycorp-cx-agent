"""WhatsApp INBOUND webhook + the handoff redeem — the DIRECT path (2026-07-22).

Three thin HTTP shells over services/whatsapp_inbound (AD-I); no LLM in any of
them (AD-J-style: the model is never in this loop):

    GET  /api/whatsapp/webhook           Meta's subscription verification handshake
    POST /api/whatsapp/webhook           inbound messages -> identify -> reply-with-link
    POST /api/whatsapp/handoff/redeem    open the link -> a web session bound to the customer

WHY A WEBHOOK AT ALL, WHEN OUTBOUND NEEDED NONE. Sending is a request WE make;
receiving is a push META makes, and a push needs a public endpoint to land on.
This is the "dont we need webhook?" answered: only inbound needs one. It stays
dormant unless WHATSAPP_INBOUND_ENABLED is set, so registering the route costs
nothing until the demo turns the channel on.

SECURITY, in one place so it is auditable:
  - The POST verifies Meta's X-Hub-Signature-256 HMAC over the RAW body when an
    app secret is configured; an unsigned/forged body is refused. With no secret
    set the check is skipped and SAID so in the log — a dev affordance, never a
    demo posture.
  - The redeem trusts the TOKEN and nothing else. customer identity is resolved
    server-side from the token row; the client sends no customer_id/mobile and
    could not be believed if it did (the trusted-context rule, same as intents).

THE CHECKPOINT BIND is the redeem's real work, and it reuses api/intents.py's
mechanism exactly: append a synthetic find_or_create_customer tool_use/tool_result
pair and set customer_id in graph state, so the web agent resumes ALREADY knowing
the customer — name, consent, memory — without re-identifying them. The greeting
directive (server-authored) is then sent by the frontend as the kickoff turn, and
Aisha speaks first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.agent import build_graph, new_sqlite_checkpointer
from app.config import AGENT_CHECKPOINT_PATH
from app.db import get_db
from app.models import Actor, AuditType
from app.services import audit
from app.services import customers as customers_service
from app.services import whatsapp_inbound

log = logging.getLogger("app.api.whatsapp")

router = APIRouter(prefix="/api/whatsapp")


# --------------------------------------------------------------------------- #
# GET /webhook — Meta subscription verification
# --------------------------------------------------------------------------- #
@router.get("/webhook")
def verify_webhook(request: Request) -> Response:
    """Meta calls this once when you set the callback URL. Echo hub.challenge iff
    hub.verify_token matches ours. A miss (or an unset token) is a 403, so an
    unconfigured channel can never be verified into existence."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and config.WHATSAPP_VERIFY_TOKEN and token == config.WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    log.warning("whatsapp webhook verification refused (mode=%s, token_match=%s)",
                mode, token == config.WHATSAPP_VERIFY_TOKEN)
    return Response(content="forbidden", status_code=403, media_type="text/plain")


# --------------------------------------------------------------------------- #
# POST /webhook — inbound messages
# --------------------------------------------------------------------------- #
def _signature_ok(raw: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 over the raw body. When no app secret is
    configured, verification is SKIPPED (dev only) — the caller logs that."""
    if not config.WHATSAPP_APP_SECRET:
        return True  # dev affordance; logged by the caller
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        config.WHATSAPP_APP_SECRET.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def _iter_inbound_messages(payload: dict):
    """Yield (from_msisdn, text, wamid, list_reply_id) per actionable message.

    TWO shapes are actionable now (2026-07-26). `text` is the original: a typed
    message we can parse an intent from. `interactive` / `list_reply` is new, and
    is how a feedback-survey rating arrives — the customer taps a row and Meta
    sends back the row id we ourselves minted ("q3:4"), plus its title as the
    human-readable text.

    That id is why a tap can never be misfiled: it names the question AND the
    score, so a customer scrolling back to an older question and tapping it is
    recorded against the question they were looking at rather than against
    whichever one happens to be outstanding.

    Everything else is still skipped — delivery/read `statuses`, reactions,
    images, and voice notes (feedback is text-only by product decision, so an
    inbound audio has no handler and downloading Meta media is deliberately not
    built). A button_reply is also ignored: nothing in this system sends buttons,
    since Meta caps them at three and a 1-5 scale needs five.
    """
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                frm = msg.get("from", "")
                wamid = msg.get("id")
                if not frm:
                    continue
                kind = msg.get("type")
                if kind == "text":
                    body = (msg.get("text") or {}).get("body", "")
                    if body:
                        yield frm, body, wamid, None
                elif kind == "interactive":
                    interactive = msg.get("interactive") or {}
                    if interactive.get("type") != "list_reply":
                        continue
                    reply = interactive.get("list_reply") or {}
                    reply_id = reply.get("id")
                    if reply_id:
                        yield frm, reply.get("title") or "", wamid, reply_id


@router.post("/webhook")
async def inbound_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Receive inbound WhatsApp messages. ALWAYS 200 quickly (Meta retries on
    anything else): parse, verify signature, hand each text to the service. A
    disabled channel silently accepts and drops."""
    raw = await request.body()
    if not _signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("whatsapp webhook: bad signature, rejecting")
        return {"ok": False, "reason": "bad_signature"}
    if not config.WHATSAPP_APP_SECRET:
        log.warning("whatsapp webhook: signature verification SKIPPED (no app secret set)")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": True}  # not JSON we understand — ack so Meta stops retrying

    handled = 0
    for frm, text, wamid, list_reply_id in _iter_inbound_messages(payload):
        try:
            whatsapp_inbound.handle_inbound_text(
                db, from_msisdn=frm, text=text, wamid=wamid, list_reply_id=list_reply_id
            )
            handled += 1
        except Exception:  # noqa: BLE001 — one bad message must not 500 the batch
            log.exception("whatsapp inbound handling failed for from=%s", frm)
    return {"ok": True, "handled": handled}


# --------------------------------------------------------------------------- #
# POST /handoff/redeem — open the link, get a bound web session
# --------------------------------------------------------------------------- #
class RedeemBody(BaseModel):
    token: str


def _synthetic_identify_pair(call_id: str, mobile: str, envelope: dict) -> list[dict]:
    """A find_or_create_customer tool_use/tool_result pair, so the checkpoint the
    web agent resumes from looks exactly as if it had identified the customer
    itself. Mirrors api/intents._synthetic_pair; the id is prefixed `wa_` so the
    pair is always identifiable as handoff-originated."""
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": "find_or_create_customer",
                 "input": {"mobile": mobile}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id,
                 "content": json.dumps(envelope, ensure_ascii=False, default=str)},
            ],
        },
    ]


@router.post("/handoff/redeem")
def redeem_handoff(body: RedeemBody, db: Session = Depends(get_db)) -> dict:
    """Redeem a handoff token: mint a fresh web session, bind it to the customer
    in the agent checkpoint, and return the session plus the server-authored
    kickoff the frontend fires so Aisha greets first.

    Errors come back as {"ok": false, "reason": ...} with a 200 — the frontend
    just falls back to a normal empty chat, exactly as it would for an unknown
    session. A stale link should degrade to "start fresh", never to an error page.
    """
    session_id = uuid4().hex
    resolved = whatsapp_inbound.redeem(db, token=body.token, session_id=session_id)
    if "error" in resolved:
        return {"ok": False, "reason": resolved["error"]}

    customer_id = resolved["customer_id"]
    mobile = resolved["mobile"]

    # Bind the session -> customer in the checkpoint, so the web agent resumes
    # already identified. Only when we actually matched a customer; an unknown
    # number gets a fresh unbound session and a generic greeting.
    if customer_id and mobile:
        try:
            # Re-run identification against the SAME service the tool wraps, so the
            # envelope (name, consent, memory) is byte-identical to a real one.
            # verify_name is OFF here (the default): this is a TRUSTED bind — the
            # single-use token already proved identity — so the name+number second
            # factor the agent tool applies does not belong on it (it would refuse
            # a named customer bound by mobile alone and break the handoff).
            envelope = customers_service.find_or_create(db, mobile=mobile)
            db.commit()
            call_id = "wa_" + uuid4().hex
            graph = build_graph(new_sqlite_checkpointer(AGENT_CHECKPOINT_PATH))
            cfg = {"configurable": {"thread_id": session_id}}
            graph.update_state(
                cfg,
                {
                    "messages": _synthetic_identify_pair(call_id, mobile, envelope),
                    "customer_id": customer_id,
                },
            )
        except Exception:  # noqa: BLE001 — a bind failure must not lose the handoff
            log.exception("handoff checkpoint bind failed — session=%s", session_id)

    audit.record(
        db,
        session=session_id,
        actor=Actor.customer,  # a person tapped the link — the agent decided nothing
        type=AuditType.intent,
        payload={
            "via": "whatsapp_handoff_redeem",
            "customer_id": customer_id,
            "intent": resolved["intent"],
            "booking_ref": resolved["booking_ref"],
        },
    )
    db.commit()

    kickoff = whatsapp_inbound.compose_greeting_directive(
        intent=resolved["intent"],
        lang=resolved["lang"],
        name=resolved["name"],
        title=resolved["title"],
        booking_ref=resolved["booking_ref"],
    )
    return {
        "ok": True,
        "session_id": session_id,
        "kickoff": kickoff,
        "lang": resolved["lang"],
    }
