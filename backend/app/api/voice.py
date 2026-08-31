"""Voice notes — the DIRECT path (AD-I), no LLM involved.

    POST /api/voice/transcribe        multipart: session_id, audio, duration_ms
    GET  /api/audio/{audio_id}?session_id=<uuid>    the recording, for playback

WHY TRANSCRIPTION IS ITS OWN ROUND TRIP, and not a field on /api/chat. The two
steps answer to different clocks and different failures. Transcription is a
bounded request/response against a US endpoint that either works or does not;
chat is a 30-second streaming agent loop. Folding audio into the chat POST would
mean the customer waits behind a silent vendor call before the first token, with
no way to show them what was heard, and a transcription failure would surface as
a broken CHAT rather than a retryable recording.

Split, the frontend gets the transcript first, paints it under the player, and
only then opens the stream — so the customer sees their own words land while the
agent is still thinking. The chat POST is then completely ordinary: `message` is
a string, exactly as if it had been typed. Nothing downstream knows or cares
that a microphone was involved.

THE AGENT CANNOT REACH EITHER OF THESE. There is no transcription tool and no
playback tool. Speech-to-text is a fact about what was said, and facts belong to
us, never to the model (rule 2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.config import VOICE_MAX_BYTES
from app.db import get_db
from app.services import voice as voice_service

router = APIRouter()


@router.post("/api/voice/transcribe")
def transcribe(
    session_id: str = Form(...),
    audio: UploadFile = File(...),
    duration_ms: int | None = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    """Store one recording and return what was heard in it.

    Returns the SERVICE'S OWN ENVELOPE VERBATIM, failure cases included — the
    same contract the tap intents follow. A caller that gets
    `{"ok": false, "reason": "no_speech_detected", "audio_id": "..."}` has
    everything it needs to draw the player and handle the miss, without a second
    request and without inferring anything from a status code.

    200 ON A FAILED TRANSCRIPTION, deliberately. "We could not make out what you
    said" is a successful round trip with a disappointing answer, not a broken
    request; the recording was stored and is playable. Reserving non-200 for
    genuine transport faults keeps `ok` the single thing the client branches on.

    DEFINED AS `def`, NOT `async def`, so FastAPI runs it in the threadpool. The
    body does blocking work — a DB write and a vendor HTTP call — and on the
    event loop that would stall every other attendee's SSE stream mid-token.
    """
    # Bounded read: one byte past the limit is enough to know it is over, and
    # stops a hostile upload being fully buffered before we reject it. `.file`
    # rather than `await .read()` because this handler is sync (see above).
    data = audio.file.read(VOICE_MAX_BYTES + 1)

    result = voice_service.transcribe_and_store(
        db,
        session_id=session_id,
        audio=data,
        mime=audio.content_type or "audio/webm",
        duration_ms=duration_ms,
    )
    # The route owns the transaction; the service only flushes. Committed on the
    # failure paths too — a stored-but-untranscribed recording is a row we want
    # to keep, both so the player works and so a bad transcription stays
    # investigable.
    db.commit()
    return result


@router.get("/api/audio/{audio_id}")
def get_audio(
    audio_id: str,
    session_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """The recording itself, for the in-bubble player. 404 unless it is yours.

    404 — NOT 403 — ON A MISMATCH, and the same 404 for "no such id", "not your
    session" and "expired". A distinct 403 would confirm that some other
    session's recording exists at that id, which is the one bit an enumeration
    attempt is trying to buy. One response for all three tells a prober nothing.

    It also lands the client in exactly the right place: the player is drawn from
    a ConversationTurn.audio_id anchor, and a 404 there means "drop the player,
    keep the transcript" — the same "never render a shell" rule the vehicle and
    booking cards follow (FR-CX-07). A recording that has aged out simply stops
    being playable, and the words it produced remain in the thread.
    """
    note = voice_service.get(db, audio_id=audio_id, session_id=session_id)
    if note is None:
        raise HTTPException(status_code=404, detail="audio_not_found")

    return Response(
        content=note.audio,
        media_type=note.mime,
        headers={
            # no-store: this is a customer's voice, under a 48h retention and a
            # rule 12 delete obligation. A cached copy in a shared browser at a
            # live demo would outlive both, and would keep playing after "forget
            # me" had supposedly erased it. Re-fetching per play costs nothing
            # at this size and keeps the gate above authoritative every time.
            "Cache-Control": "private, no-store, max-age=0",
            "Content-Disposition": "inline",
        },
    )
