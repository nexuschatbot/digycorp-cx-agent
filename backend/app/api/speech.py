"""Read-aloud — the DIRECT path (AD-I), no LLM involved.

    GET /api/speech/{turn_id}?session_id=<uuid>   -> audio/mpeg

A GET RETURNING BYTES, not a POST returning a url. The client sets it as an
<audio> src and the browser streams it, so nothing has to be buffered in JS and
a replay is one request. It is shaped like GET /api/audio/{audio_id}
deliberately: both are "give me the sound for this thing I already have a
pointer to", and they should not be two different-looking endpoints.

It does generate on a cache miss, which makes this a GET with a side effect —
knowingly. The side effect is idempotent (the same turn always yields the same
clip) and invisible to the caller, and the alternative (POST to create, GET to
fetch) doubles the round trips on the one path where latency is already the
weak point.

TURN ID, NOT TEXT. The service reads Aisha's words from the transcript itself —
so everything spoken in her voice is something she actually said and can be
proved to have said, and no caller can use this as a free text-to-speech proxy.
See services/speech for the full reasoning.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import speech as speech_service

router = APIRouter()

# Reasons that mean "this will never work, stop asking" versus "it might work
# later". The client falls back to the browser's on-device voice either way, but
# only the second kind is worth retrying, and a 404 vs 503 is how it tells them
# apart without parsing a body it never receives (the success path is audio).
_NOT_FOUND = {"turn_not_found", "not_an_agent_turn", "empty_turn", "text_too_long"}


@router.get("/api/speech/{turn_id}")
def get_speech(
    turn_id: int,
    session_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Audio for one of Aisha's turns, generated on first request and cached.

    503 when the vendor is unreachable or unconfigured, 404 when the turn is not
    ours to read. BOTH are recoverable at the client: read-aloud falls back to
    the browser's own voice, so a vendor outage costs quality, never the feature.

    DEFINED AS `def`, NOT `async def`, so FastAPI runs it in the threadpool — the
    body does a blocking vendor call, and on the event loop that would stall
    every other attendee's SSE stream mid-token.
    """
    if not session_id:
        # No viewer, no clip. Same shape as the audio endpoint: an absent session
        # identifies nobody, and nobody may hear a conversation.
        raise HTTPException(status_code=404, detail="turn_not_found")

    result = speech_service.synthesize_turn(
        db, session_id=session_id, turn_id=turn_id
    )
    # The route owns the transaction; the service only flushes. Committed on the
    # failure paths too, so the audit row for a vendor error survives — a
    # transcription that failed in front of an audience should be findable
    # afterwards, not rolled back with the request.
    db.commit()

    if not result.get("ok"):
        reason = result.get("reason", "synthesis_failed")
        raise HTTPException(
            status_code=404 if reason in _NOT_FOUND else 503, detail=reason
        )

    clip = result["clip"]
    return Response(
        content=clip.audio,
        media_type=clip.mime,
        headers={
            # PRIVATE BUT CACHEABLE, unlike the customer's own recordings. This
            # is Aisha's reply read back — still PII-bearing, so never a shared
            # cache, but re-fetching it on every replay of the same message
            # wastes the demo's bandwidth for nothing. The server-side clip
            # expires at 48h; a browser copy living an hour changes no exposure
            # that the on-screen text does not already have.
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": "inline",
        },
    )
