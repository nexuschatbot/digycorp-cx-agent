"""POST /api/chat — the SSE chat endpoint (AD-A/AD-B/AD-D/AD-E).

Transport (AD-B): a `text/event-stream` from a FastAPI StreamingResponse. The
event contract is exactly CLAUDE.md's:

    event: token       data: {"text": "<chunk>"}          # append to live bubble
    event: tool_event  data: {"tool","input","output"}    # the agent working
    event: booking     data: {"booking_ref": "TD-..."}    # freeze the timer
    event: text_break  data: {}                           # rule 17: seal the bubble,
                                                          #   next tokens start a new
                                                          #   one. Deletes NOTHING.
    event: cascade_start data: {"briefs": N}              # specialists starting
                                                          #   (multi-agent path only)
    event: closed      data: {}                           # rule 19: chat ended
    event: done        data: {"lang": "en|ar", "turn_id": N}  # stream complete

    event: error       data: {"message": "..."}           # graceful UI recovery
    ": ping"  heartbeat comment every ~15s on queue silence (clients ignore it)

TRANSPORT ONLY. The turn itself — transcript writes, language, the graph, card
anchors, abort semantics — lives in app/agent/runner.run_turn(), shared with
the voice-call WebSocket gateway so the two channels cannot drift. This module
owns what is SSE-shaped: the worker thread, the asyncio.Queue bridge, the 15s
heartbeat, and the disconnect→cancel wiring.

ARCHITECTURE — worker thread + async queue bridge. The graph is SYNC (raw
Anthropic SDK per AD-A, sync SQLAlchemy service layer), so run_turn runs on a
worker thread; its events are bridged to this async generator through an
asyncio.Queue (call_soon_threadsafe). The async side owns the 15s heartbeat and
the abort — neither of which a blocking sync generator could do.

ABORT (AD-E): a per-request threading.Event. When the client drops, Starlette
closes this async generator → its finally sets the event → the agent node stops
within one token and the tool node stops BEFORE any dispatch. The graph stops;
it does not run on to the 60s ceiling burning tokens, and no booking is written
for a departed customer.

RESUME: session_id IS the checkpointer thread_id, so a second turn on the same
session_id resumes with the full tool_use/tool_result history — no re-identifying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import lang as lang_service
from app.services import recognition
from app.agent.runner import _card_anchor, run_turn  # noqa: F401 — _card_anchor
# re-exported: tests/test_conversation_resume.py pins its behaviour under this
# module path (its natural home moved to the runner with the rest of the turn).

log = logging.getLogger("app.api.chat")
router = APIRouter()


def _detect_lang(text: str) -> str:
    """Per-turn language detection (LOC-01). Any Arabic-script char -> ar.

    Kept as a name and signature; the rule itself moved to app/lang.py when
    services/voice.py came to need the identical answer (a service must not
    import the API layer, and two copies of this would drift).
    """
    return lang_service.detect(text)


def _sse(event: str, data: dict) -> str:
    # ensure_ascii=False so Arabic streams as native script, not \uXXXX escapes.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # Optional (Option B): the exact slot_id from a tapped slot chip. When present
    # the tool node PINS lock_slot to it (executor-injected trusted context) so the
    # customer's human prose is never re-parsed for the id. Absent for typed turns —
    # a pure, backward-compatible addition.
    slot_id: int | None = None
    # Optional (voice notes, 2026-07-21): the recording this turn was SPOKEN as,
    # already stored and transcribed by POST /api/voice/transcribe. `message` is
    # then the transcript — an ordinary string, handled identically to a typed
    # one, which is exactly why voice needed no change to the agent.
    #
    # A POINTER FOR REPLAY ONLY. It is stamped onto the customer's
    # ConversationTurn so a reload can rebuild the player, and it is not passed
    # to the graph: the model has no use for it and no way to reach the audio.
    audio_id: str | None = None
    # The customer recorded, but we could not make out a word (empty audio, a
    # vendor outage, noise). `message` is then EMPTY — see _VOICE_FAILED_NOTE
    # in app/agent/runner.py.
    voice_failed: bool = False
    # WhatsApp handoff kickoff (2026-07-22): `message` is a SERVER-AUTHORED
    # greeting directive (from POST /api/whatsapp/handoff/redeem), not the
    # customer's words, so Aisha speaks FIRST when they open the link. When true
    # we store NO customer ConversationTurn — there is no customer bubble to show,
    # only her greeting — and the directive is fed to the graph exactly as
    # _VOICE_FAILED_NOTE is: context the model acts on, never renders. Everything
    # else (streaming, the agent turn it produces, the checkpoint) is unchanged.
    kickoff: bool = False


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    cancel = threading.Event()  # AD-E abort flag, shared with the graph via config

    # DEVICE RECOGNITION (C10, 2026-08-14). A long-lived cookie, DELIBERATELY NOT
    # the session id: session_id is per-tab and per-conversation (it is the
    # checkpointer thread), so reusing it here would either make recognition die
    # with the tab or make one conversation's history follow the browser forever.
    # Two identifiers with two lifetimes, kept apart.
    #
    # Minted on the FIRST message rather than on page load: a visitor who never
    # types is not somebody we need to remember, and the cookie is set on this
    # response either way — headers go out before the first token, so setting it
    # on a StreamingResponse is not a race.
    #
    # HttpOnly: it is a bearer credential and no page script has business reading
    # it. SameSite=Lax: the chat is same-origin, so nothing legitimate needs it on
    # a cross-site request. Secure ONLY over HTTPS — hardcoding it would silently
    # drop the cookie on a local http dev server and make C10 look broken.
    device_token = request.cookies.get(recognition.COOKIE_NAME)
    set_device_cookie = device_token is None
    if set_device_cookie:
        device_token = recognition.mint_token()

    def push(item: tuple) -> None:
        """Hand an item to the async side from the worker thread, safely."""
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # event loop already closed (client long gone)
            pass

    def worker() -> None:
        try:
            run_turn(
                req.session_id,
                req.message,
                on_event=lambda event, data: push(("event", event, data)),
                cancel=cancel,
                slot_id=req.slot_id,
                audio_id=req.audio_id,
                voice_failed=req.voice_failed,
                kickoff=req.kickoff,
                device_token=device_token,
            )
        finally:
            push(("close", None, None))

    threading.Thread(target=worker, daemon=True, name=f"chat-{req.session_id[:8]}").start()

    async def sse_gen():
        try:
            yield ": stream open\n\n"  # flush headers to the client immediately
            while True:
                try:
                    kind, event, data = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # AD-D heartbeat — transport keepalive on silence
                    continue
                if kind == "close":
                    break
                yield _sse(event, data)
        finally:
            # Client disconnect (Starlette closes this generator) OR normal end:
            # signal the graph to stop. On disconnect this is what actually aborts
            # generation server-side (AD-E); on normal end it is a harmless no-op.
            cancel.set()

    response = StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flush live
        },
    )
    if set_device_cookie:
        response.set_cookie(
            recognition.COOKIE_NAME,
            device_token,
            max_age=recognition.COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
    return response
