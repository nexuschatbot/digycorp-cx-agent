"""WS /ws/takeover/{session_id} — the live channel for a human takeover.

Once an admin takes an escalated session over, the AI is gone (the gate in
runner.run_turn) and BOTH sides talk through this socket instead: the admin
from the Handoffs drawer, the customer from the chat page, which switches its
sends here the moment it hears the takeover started. SSE could not carry this —
the customer only holds a stream open while their own POST is in flight, and a
human reply must land while they are simply waiting.

ONE PROCESS, ONE HUB. The broadcast map below is in-process state, exactly like
the voice gateway's per-call state: the deploy runs a single uvicorn process
(no --workers), so every socket for a session lands in the same dict. If that
ever changes, this needs a real pub/sub — flagged here, not hidden.

WIRE PROTOCOL (JSON text frames, both directions):

    in   {"type": "message", "text": "..."}           # either role
    in   {"type": "close"}                            # admin only: end & close
    out  {"type": "state", ...}                       # on connect: snapshot
    out  {"type": "takeover_started", "admin_name"}   # a person joined
    out  {"type": "message", "speaker", "text",
          "lang", "ts", "turn_id", "admin_name"}      # one persisted turn
    out  {"type": "closed"}                           # conversation over
    out  {"type": "error", "error": "..."}            # refusal, as data

Roles come from the query string (?role=admin&name=... | ?role=customer).
There is no auth on either role — the same deliberate D1 demo relaxation as the
transcript GET and the whole console, logged on the same hardening checklist:
in production the admin role must be bound to an authenticated operator and the
customer role to the session's owner.

DB WORK OFF THE LOOP. The service layer is sync SQLAlchemy, so every read and
write runs through asyncio.to_thread with a short-lived SessionLocal it commits
and closes itself — the per-operation pattern the call gateway already uses.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db import SessionLocal
from app.models import Actor, Speaker
from app.services import conversation as conversation_service
from app.services import takeover as takeover_service

log = logging.getLogger("app.api.takeover")
router = APIRouter()

# A message longer than this is not a chat message. Refused as data, like every
# other refusal on this socket.
MAX_MESSAGE_CHARS = 4000

# session_id -> the sockets listening to it (admin and customer alike).
_hub: dict[str, set[WebSocket]] = {}


async def _broadcast(session_id: str, frame: dict) -> None:
    """Send one frame to every socket on the session, dropping the dead."""
    for ws in list(_hub.get(session_id, ())):
        try:
            await ws.send_json(frame)
        except Exception:
            _hub.get(session_id, set()).discard(ws)


def _start_sync(session_id: str, admin_name: str) -> dict:
    db = SessionLocal()
    try:
        result = takeover_service.start(db, session_id=session_id, admin_name=admin_name)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _state_sync(session_id: str) -> dict:
    db = SessionLocal()
    try:
        return {
            **takeover_service.state(db, session_id=session_id),
            "escalated": takeover_service.is_escalated(db, session_id=session_id),
            "closed": conversation_service.is_closed(db, session_id=session_id),
        }
    finally:
        db.close()


def _append_sync(session_id: str, speaker: Speaker, text: str) -> dict:
    db = SessionLocal()
    try:
        result = takeover_service.append_turn(
            db, session_id=session_id, speaker=speaker, text=text
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _close_sync(session_id: str) -> dict:
    db = SessionLocal()
    try:
        # Same service as the customer's "New chat" and the agent's tool, so all
        # three exits behave identically (held slots released, audit row left).
        # Actor.admin: a person in the back office ended it, and the trail must
        # say so (rule 15/16).
        result = conversation_service.close(
            db, session_id=session_id, actor=Actor.admin
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.websocket("/ws/takeover/{session_id}")
async def takeover_ws(ws: WebSocket, session_id: str) -> None:
    role = ws.query_params.get("role", "customer")
    name = (ws.query_params.get("name") or "").strip()
    await ws.accept()

    if role == "admin":
        result = await asyncio.to_thread(_start_sync, session_id, name)
        if "error" in result:
            # not_escalated / conversation_closed / admin_name_required — the
            # drawer shows why and the socket has nothing more to say.
            await ws.send_json({"type": "error", "error": result["error"]})
            await ws.close()
            return
        if result.get("already") and result.get("admin_name") != name:
            # First admin won. Tell the second who holds it, then hang up.
            await ws.send_json(
                {"type": "error", "error": "already_taken",
                 "admin_name": result.get("admin_name")}
            )
            await ws.close()
            return
        holder = result.get("admin_name")
    elif role == "customer":
        # No validation, deliberately: the customer client opens this listener
        # the moment the escalation tool fires, possibly before that turn's
        # transaction has committed, and refusing the race would orphan them.
        # D1 covers what an unauthenticated listener may see (nothing beyond
        # what GET /api/conversation already serves).
        holder = None
    else:
        await ws.send_json({"type": "error", "error": "invalid_role"})
        await ws.close()
        return

    _hub.setdefault(session_id, set()).add(ws)
    try:
        # Snapshot first, so a reconnecting client renders the right mode
        # before the first live frame arrives.
        snapshot = await asyncio.to_thread(_state_sync, session_id)
        await ws.send_json({"type": "state", **snapshot})
        if role == "admin" and not snapshot.get("closed"):
            # Fresh start or rejoin — either way the customer side may not have
            # heard it yet (their listener can predate the takeover).
            await _broadcast(
                session_id, {"type": "takeover_started", "admin_name": holder}
            )

        while True:
            try:
                frame = await ws.receive_json()
            except (ValueError, KeyError):
                await ws.send_json({"type": "error", "error": "invalid_frame"})
                continue
            kind = frame.get("type")

            if kind == "message":
                text = (frame.get("text") or "").strip()
                if not text:
                    await ws.send_json({"type": "error", "error": "empty_message"})
                    continue
                if len(text) > MAX_MESSAGE_CHARS:
                    await ws.send_json({"type": "error", "error": "message_too_long"})
                    continue
                state = await asyncio.to_thread(_state_sync, session_id)
                if state.get("closed"):
                    await ws.send_json({"type": "error", "error": "conversation_closed"})
                    continue
                if role == "customer" and not state.get("taken_over"):
                    # Before a person joins, the customer's words belong to the
                    # ordinary chat POST (and the AI). This socket only carries
                    # the human half of the conversation.
                    await ws.send_json({"type": "error", "error": "not_taken_over"})
                    continue
                speaker = Speaker.staff if role == "admin" else Speaker.customer
                turn = await asyncio.to_thread(_append_sync, session_id, speaker, text)
                if "error" in turn:
                    await ws.send_json({"type": "error", "error": turn["error"]})
                    continue
                await _broadcast(
                    session_id,
                    {
                        "type": "message",
                        **turn,
                        # The holder's name rides on every staff frame so the
                        # customer bubble can be labelled without a second
                        # lookup. Null on customer frames.
                        "admin_name": holder if role == "admin" else None,
                    },
                )

            elif kind == "close" and role == "admin":
                await asyncio.to_thread(_close_sync, session_id)
                await _broadcast(session_id, {"type": "closed"})

            else:
                await ws.send_json({"type": "error", "error": "unknown_type"})

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("takeover socket failed session=%s role=%s", session_id, role)
    finally:
        sockets = _hub.get(session_id)
        if sockets is not None:
            sockets.discard(ws)
            if not sockets:
                _hub.pop(session_id, None)
