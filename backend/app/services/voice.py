"""Voice notes (input only) — store the recording, transcribe it, own its life.

Added by product decision 2026-07-21; see the scope override in CLAUDE.md. The
customer may record, WhatsApp-style. Aisha still answers in TEXT — there is no
TTS here and no agent-side recording.

WHERE THIS SITS, AND WHY THE AGENT NEVER SEES IT
------------------------------------------------
Transcription is a PRE-PROCESSOR, not a channel. It runs to completion before
/api/chat is called, and what reaches the agent is an ordinary `message` string
indistinguishable from a typed one. So the prompt, the LangGraph loop, every
tool, the SSE contract and the audit choke point are all untouched, and AD-C
(no WebSockets for chat) is untouched with them.

There is deliberately NO transcription tool. The model cannot ask us to
transcribe anything, cannot re-transcribe, and cannot reach the blob — exactly
as it cannot reach the database. Speech-to-text is a fact about what was said,
and facts are ours (rule 2).

THE VOCABULARY PROMPT IS LOAD-BEARING, AND IT COMES FROM THE CATALOGUE
----------------------------------------------------------------------
Measured in the V0 spike (2026-07-21): unprompted, gpt-4o-transcribe heard
"Jetour T2 ... in Deira" as "**Jetter** T2 ... in **Dera**" — it corrupted the
brand and the showroom, i.e. precisely the two entities a booking is made of.
`search_vehicles(name="Jetter T2")` finds nothing, and the customer gets an
honest "we don't have that" about a car we very much do have. Passing the
catalogue as the `prompt` parameter fixed it, and fixed two Arabic verb
corruptions as a bonus.

So `_vocabulary` reads live `VehicleModel` and `Showroom` rows. It is NOT a
hardcoded list, for the same reason nothing else here stores display text: a
list typed by hand drifts from the catalogue it claims to describe, and the
first symptom would be the agent failing to recognise a car we just added.

ERRORS ARE DATA, NEVER EXCEPTIONS
---------------------------------
Same contract the tools use. A vendor outage, a silent recording, an oversized
upload — all of them return a dict with `ok: False` and a `reason`. A live turn
must never die because a US endpoint had a bad minute.

NOTE ON FAILED TRANSCRIPTION: the blob is STILL STORED and the row still exists.
The customer did record something; the player must work even when we could not
make out a word, and the recording is the only evidence of what was actually
said. Deleting on failure would destroy exactly the case worth investigating.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import lang as lang_service
from app.config import (
    OPENAI_API_KEY,
    STT_MODEL,
    VOICE_MAX_BYTES,
    VOICE_MAX_SECONDS,
    VOICE_RETENTION_HOURS,
)
from app.models import Actor, AuditType, Showroom, VehicleModel, VoiceNote
from app.services import audit

log = logging.getLogger("app.services.voice")

# Container -> extension. The vendor infers format from the filename, so this
# has to be right; we transcode nothing. Chrome/Android produce webm/opus,
# Safari (macOS and iOS alike) produces mp4/aac.
_EXT = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}

# Arabic-script place and brand names, which the catalogue holds only in English
# (`Showroom.name` is "Deira", not "ديرة"). Without these the biasing prompt helps
# an English speaker and does nothing for an Arabic one. Short and stable — these
# are proper nouns, not display text: nothing is ever SHOWN from this string, it
# only tells the transcriber which words exist in this world.
_VOCAB_AR = "جيتور، ديرة، القوز، عجمان، العين، أبوظبي، الرابدان، دبي، الشارقة"


def _client():
    """Lazy OpenAI client. Constructed per call, not at import.

    Module-level construction would make importing anything in this package fail
    when OPENAI_API_KEY is unset — which is the normal state for a dev running
    the text-only demo, and for CI. Voice degrades; the app still boots.
    """
    from openai import OpenAI

    return OpenAI(api_key=OPENAI_API_KEY)


def _vocabulary(db: Session) -> str:
    """The biasing prompt, composed from the live catalogue. See module docstring."""
    vehicles = db.scalars(
        select(VehicleModel.name).order_by(VehicleModel.id)
    ).all()
    showrooms = db.scalars(select(Showroom.name).order_by(Showroom.id)).all()
    brands = db.scalars(select(VehicleModel.brand).distinct()).all()

    parts = ["Elite Group test-drive booking, Dubai."]
    if brands:
        parts.append("Brands: " + ", ".join(sorted(b for b in brands if b)) + ".")
    if vehicles:
        parts.append("Models: " + ", ".join(v for v in vehicles if v) + ".")
    if showrooms:
        parts.append("Showrooms: " + ", ".join(s for s in showrooms if s) + ".")
    parts.append(_VOCAB_AR)
    return " ".join(parts)


def pcm16_to_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono in a WAV container (stdlib, no transcoding).

    The call gateway buffers a call utterance as raw PCM frames off the wire;
    `transcribe_and_store` wants a container the vendor can identify by
    filename. WAV is that container with 44 bytes of header — the samples are
    byte-for-byte what the microphone sent, which keeps "we transcode nothing"
    true on the call path too.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # PCM16
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe_and_store(
    db: Session,
    *,
    session_id: str,
    audio: bytes,
    mime: str,
    duration_ms: int | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
    max_seconds: int | None = None,
) -> dict:
    """Store one recording and return what was heard in it. Never raises.

    Returns, on success:
        {"ok": True, "audio_id": str, "text": str, "lang": "en"|"ar",
         "duration_ms": int|None}

    On failure, the same shape minus `text`, plus a `reason` the caller can act
    on. `audio_id` is present whenever the blob was stored — so a failed
    transcription still yields a playable recording, which is the whole point of
    storing before transcribing.

    Rejected outright (nothing stored): empty upload, over VOICE_MAX_BYTES, over
    `max_seconds` — which is the CALLER'S limit, defaulting to VOICE_MAX_SECONDS
    (the voice-note rule). The call gateway passes CALL_UTTERANCE_MAX_SECONDS:
    call speech deliberately runs longer than a composed note, and judging a
    call utterance by the note limit silently discarded every 30–60 s answer on
    the batch path (found 2026-08-03). The browser also caps duration, but that
    is a UI affordance and this is the actual limit.
    """
    now = now or datetime.now()
    limit_seconds = max_seconds if max_seconds is not None else VOICE_MAX_SECONDS

    if not audio:
        return {"ok": False, "reason": "empty_recording", "audio_id": None}
    if len(audio) > VOICE_MAX_BYTES:
        return {
            "ok": False,
            "reason": "recording_too_large",
            "audio_id": None,
            "size_bytes": len(audio),
        }
    if duration_ms is not None and duration_ms > limit_seconds * 1000:
        return {
            "ok": False,
            "reason": "recording_too_long",
            "audio_id": None,
            "duration_ms": duration_ms,
        }

    mime = (mime or "audio/webm").split(";")[0].strip().lower()
    if mime not in _EXT:
        return {"ok": False, "reason": "unsupported_format", "audio_id": None, "mime": mime}

    # Opportunistic retention sweep. No scheduler exists yet (AD-F's APScheduler
    # is the reminder-clock phase), and a demo that records has a demo that
    # purges — hanging it here means expiry works without new machinery. Cheap:
    # one indexed DELETE. Never allowed to break the recording that triggered it.
    try:
        purge_expired(db, now=now)
    except Exception:  # noqa: BLE001 — a failed sweep must not fail a recording
        log.warning("voice retention sweep failed", exc_info=True)

    audio_id = str(uuid.uuid4())
    note = VoiceNote(
        audio_id=audio_id,
        session_id=session_id,
        customer_id=None,  # backfilled by attach_customer on identification
        mime=mime,
        size_bytes=len(audio),
        duration_ms=duration_ms,
        audio=audio,
        transcript="",
        lang=lang_service.detect_enum(""),  # provisional; set from the transcript
        stt_model=STT_MODEL,
        created_at=now,
        expires_at=now + timedelta(hours=VOICE_RETENTION_HOURS),
    )
    db.add(note)
    db.flush()

    if not OPENAI_API_KEY:
        # Not an error state worth a stack trace — it is the ordinary condition
        # of a text-only checkout. Say so plainly so the UI can offer typing.
        _audit_voice(
            db,
            session_id=session_id,
            audio_id=audio_id,
            outcome="unconfigured",
            correlation_id=correlation_id,
        )
        db.flush()
        return {"ok": False, "reason": "stt_not_configured", "audio_id": audio_id}

    filename = f"voice.{_EXT[mime]}"
    try:
        resp = _client().audio.transcriptions.create(
            model=STT_MODEL,
            file=(filename, audio, mime),
            # No `language=`: the customer may speak either, and which one they
            # chose is the thing we are trying to learn (rule 3). Constraining it
            # would defeat the requirement.
            prompt=_vocabulary(db),
        )
        text = (getattr(resp, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — vendor failure is data, not a 500
        log.warning("transcription failed: %s", exc)
        _audit_voice(
            db,
            session_id=session_id,
            audio_id=audio_id,
            outcome="error",
            detail=type(exc).__name__,
            correlation_id=correlation_id,
        )
        db.flush()
        return {"ok": False, "reason": "transcription_failed", "audio_id": audio_id}

    if not text:
        _audit_voice(
            db,
            session_id=session_id,
            audio_id=audio_id,
            outcome="no_speech",
            correlation_id=correlation_id,
        )
        db.flush()
        return {"ok": False, "reason": "no_speech_detected", "audio_id": audio_id}

    note.transcript = text
    note.lang = lang_service.detect_enum(text)
    db.flush()

    _audit_voice(
        db,
        session_id=session_id,
        audio_id=audio_id,
        outcome="ok",
        lang=note.lang.value,
        chars=len(text),
        duration_ms=duration_ms,
        correlation_id=correlation_id,
    )
    db.flush()

    return {
        "ok": True,
        "audio_id": audio_id,
        "text": text,
        "lang": note.lang.value,
        "duration_ms": duration_ms,
    }


def store_call_utterance(
    db: Session,
    *,
    session_id: str,
    audio: bytes,
    mime: str,
    transcript: str,
    stt_model: str,
    duration_ms: int | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Store one call utterance whose transcript the STREAMING relay already
    produced (P3). Same envelope as transcribe_and_store — the gateway treats
    the two paths identically — minus the vendor round-trip: the words came
    back over the relay while the customer was still speaking, and sending the
    audio to the vendor a second time would double the cost and the PDPL
    surface for nothing.

    An empty transcript is the relay's `no_speech_detected`: stored (the
    recording is still the evidence) and reported exactly as the batch path
    reports it, so the voice-failed recovery upstream needs no second branch.
    """
    now = now or datetime.now()

    if not audio:
        return {"ok": False, "reason": "empty_recording", "audio_id": None}
    if len(audio) > VOICE_MAX_BYTES:
        return {
            "ok": False,
            "reason": "recording_too_large",
            "audio_id": None,
            "size_bytes": len(audio),
        }

    mime = (mime or "audio/wav").split(";")[0].strip().lower()
    if mime not in _EXT:
        return {"ok": False, "reason": "unsupported_format", "audio_id": None, "mime": mime}

    try:
        purge_expired(db, now=now)
    except Exception:  # noqa: BLE001 — a failed sweep must not fail a recording
        log.warning("voice retention sweep failed", exc_info=True)

    text = (transcript or "").strip()
    audio_id = str(uuid.uuid4())
    note = VoiceNote(
        audio_id=audio_id,
        session_id=session_id,
        customer_id=None,  # backfilled by attach_customer on identification
        mime=mime,
        size_bytes=len(audio),
        duration_ms=duration_ms,
        audio=audio,
        transcript=text,
        lang=lang_service.detect_enum(text),
        stt_model=stt_model,
        created_at=now,
        expires_at=now + timedelta(hours=VOICE_RETENTION_HOURS),
    )
    db.add(note)
    db.flush()

    if not text:
        _audit_voice(
            db,
            session_id=session_id,
            audio_id=audio_id,
            outcome="no_speech",
            mode="realtime",
            correlation_id=correlation_id,
        )
        db.flush()
        return {"ok": False, "reason": "no_speech_detected", "audio_id": audio_id}

    _audit_voice(
        db,
        session_id=session_id,
        audio_id=audio_id,
        outcome="ok",
        mode="realtime",
        lang=note.lang.value,
        chars=len(text),
        duration_ms=duration_ms,
        correlation_id=correlation_id,
    )
    db.flush()

    return {
        "ok": True,
        "audio_id": audio_id,
        "text": text,
        "lang": note.lang.value,
        "duration_ms": duration_ms,
    }


def _audit_voice(
    db: Session,
    *,
    session_id: str,
    audio_id: str,
    outcome: str,
    correlation_id: str | None = None,
    **extra: object,
) -> None:
    """Audit one transcription attempt (rule 15).

    THE TRANSCRIPT IS NOT IN THE PAYLOAD, and neither is the audio. What the
    customer said is already the ConversationTurn — copying it here would put a
    second, ungated copy of their words in a table the console renders wholesale
    and rule 12 does not erase. What this records is the MECHANICAL fact: that a
    recording was sent to a vendor, which one, and how it went. That is what the
    PDPL checklist needs to be able to count, and it is all it needs.

    actor=system: neither the customer nor the agent decided this. The customer
    pressed record; the transcription is something WE then did to their audio.
    """
    audit.record(
        db,
        session=session_id,
        actor=Actor.system,
        type=AuditType.voice,
        payload={
            "action": "transcribe",
            "audio_id": audio_id,
            "outcome": outcome,
            "model": STT_MODEL,
            "vendor": "openai",
            **{k: v for k, v in extra.items() if v is not None},
        },
        correlation_id=correlation_id,
    )


def get(db: Session, *, audio_id: str, session_id: str | None) -> VoiceNote | None:
    """One recording, for playback — or None if this viewer may not have it.

    THE GATE IS SESSION OWNERSHIP, and it is stricter than the booking gate on
    purpose. A booking is legitimately reachable by reference alone (FR-RS-01:
    ref OR mobile), so `session_id` there decides only whether the PII fields
    are included. A recording has no such legitimate second route: it is the
    customer's own voice, and the only party with any business replaying it is
    the conversation that produced it. So a mismatch returns None, not a
    redacted variant — there is nothing to redact it down to.

    Expiry is enforced on READ as well as by the sweep. Otherwise a recording
    would stay retrievable for however long it took a purge to run, and "48h
    retention" would be a claim about our scheduling rather than about the data.
    """
    if not audio_id or not session_id:
        return None
    note = db.scalar(select(VoiceNote).where(VoiceNote.audio_id == audio_id))
    if note is None or note.session_id != session_id:
        return None
    if note.expires_at and note.expires_at <= datetime.now():
        return None
    return note


def attach_customer(db: Session, *, session_id: str, customer_id: int) -> int:
    """Backfill customer_id onto this session's recordings. Returns the count.

    WHY THIS EXISTS. A customer records before they identify far more often than
    after — "I want to test drive the T2" comes first, the mobile number comes
    several turns later. Those early recordings would carry customer_id=NULL
    forever, and "forget me" searching by customer would miss precisely the ones
    most likely to exist. Called when a session resolves to a customer, so
    erasure can find everything they ever said in it.

    Idempotent: re-running attaches nothing new.
    """
    notes = db.scalars(
        select(VoiceNote).where(
            VoiceNote.session_id == session_id,
            VoiceNote.customer_id.is_(None),
        )
    ).all()
    for note in notes:
        note.customer_id = customer_id
    if notes:
        db.flush()
    return len(notes)


def delete_for_customer(
    db: Session,
    *,
    customer_id: int,
    session_id: str,
    correlation_id: str | None = None,
) -> int:
    """Hard-delete this customer's recordings (rule 12). Returns the count.

    Called from services/memory.forget on a CONFIRMED erasure. Voice is personal
    data and arguably biometric-adjacent, so "forget me" that left the audio
    behind would be the erasure equivalent of a tombstone — and S-08 puts that
    erasure on a screen in front of the client.

    MATCHES ON customer_id OR THIS SESSION. The backfill above normally makes
    customer_id sufficient; the session arm is the belt to its braces, catching
    anything recorded in the live conversation that erasure is happening in
    before identification resolved. Deleting slightly too much of the requester's
    OWN audio is the safe direction to err in for a delete-my-data request.

    Bytes go with the row — that is the argument for the blob living in a column
    rather than on disk (see VoiceNote). No unlink to fail, no orphan to sweep.
    The caller owns the transaction, as everywhere else in this layer.
    """
    notes = db.scalars(
        select(VoiceNote).where(
            or_(
                VoiceNote.customer_id == customer_id,
                VoiceNote.session_id == session_id,
            )
        )
    ).all()
    for note in notes:
        db.delete(note)
    db.flush()

    if notes:
        # AuditType.erasure, not .voice — this belongs with the profile deletion
        # it accompanies, in the trail S-08 walks through. Ids only: what was
        # said is exactly what we are erasing, so recording it here would defeat
        # the request being honoured.
        audit.record(
            db,
            session=session_id,
            actor=Actor.customer,
            type=AuditType.erasure,
            payload={
                "action": "forget_me_voice",
                "customer_id": customer_id,
                "deleted": len(notes),
                "audio_ids": [n.audio_id for n in notes],
            },
            correlation_id=correlation_id,
        )
        db.flush()
    return len(notes)


def purge_expired(db: Session, *, now: datetime | None = None) -> int:
    """Delete recordings past their retention horizon. Returns the count.

    NOT AUDITED, deliberately, and this is the one place that silence is right.
    Retention expiry is a property of the schedule, not an event in anyone's
    conversation — auditing it would append rows to sessions that ended two days
    ago, for something nobody did. The retention POLICY is documented in
    config.VOICE_RETENTION_HOURS and in CLAUDE.md; an erasure someone REQUESTED
    is a different thing and is audited (see delete_for_customer).
    """
    now = now or datetime.now()
    expired = db.scalars(
        select(VoiceNote).where(VoiceNote.expires_at <= now)
    ).all()
    for note in expired:
        db.delete(note)
    if expired:
        db.flush()
        log.info("purged %d expired voice note(s)", len(expired))
    return len(expired)
