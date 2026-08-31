"""Read one of Aisha's replies aloud (read-aloud, 2026-07-21).

PLAYBACK, NOT A SPOKEN AGENT. Aisha still WRITES. Nothing is ever synthesised
unless a customer taps the speaker on a specific message; no reply is automatic,
and a voice note still gets a text reply (modality mirroring stays out of scope).

THE SERVER READS THE MESSAGE, THE CLIENT DOES NOT SEND IT
--------------------------------------------------------
`synthesize_turn` takes a TURN ID, not text. The client asks "read turn 41 of my
session" and this module looks the words up itself. That matters twice over:

  GROUNDING. Everything spoken in Aisha's voice is something she actually said,
  and is in the transcript to prove it. Accepting client text would let a caller
  put arbitrary words in her mouth — the audio equivalent of the invention rule 2
  exists to prevent, and worse, because a recording sounds authoritative.

  ABUSE. A public endpoint that synthesises submitted text is a free TTS proxy
  billed to us. Keyed on turns, the worst a caller can do is re-read their own
  conversation, which is exactly the feature.

WHY THE VENDOR AT ALL — this replaced an on-device engine that cost nothing and
sent nothing anywhere. The browser's voices are the DEVICE'S, and on the demo
hardware the English ones were 1990s novelty voices (Boing, Zarvox, Fred) while
Arabic had exactly one, formal and MSA. Server synthesis is the only way to get
the same warm Gulf register on every attendee's phone.

⚠️ ITS PDPL EXPOSURE IS HEAVIER THAN THE STT PATH'S AND MUST NOT BE FILED WITH
IT. Transcription sent the customer's own audio. This sends AISHA'S REPLIES,
which quote the customer back to themselves — "confirmed for Saturday, we'll text
+971 50 555 0103" is a name, a mobile and a booking reference leaving the UAE.
Own D1 entry. Mitigated by per-session scoping, the 48h expiry, and hard deletion
on "forget me" (rule 12) — never by pretending it is the same exposure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import (
    CALL_TTS_INSTRUCTIONS,
    OPENAI_API_KEY,
    TTS_INSTRUCTIONS,
    TTS_MAX_CHARS,
    TTS_MODEL,
    TTS_VOICE_AR,
    TTS_VOICE_EN,
    VOICE_RETENTION_HOURS,
)
from app.models import (
    Actor,
    AuditType,
    ConversationTurn,
    Language,
    Speaker,
    SpeechClip,
)
from app.services import audit

log = logging.getLogger("app.services.speech")


# --------------------------------------------------------------------------- #
# Normalisation — rewrite for SPEECH ONLY, never for display
# --------------------------------------------------------------------------- #
# A TWIN OF frontend/src/speech-text.ts, and the duplication is deliberate but
# not free. The browser fallback needs this in TypeScript; the server needs it in
# Python because the SERVER holds the text (see the module docstring). Both are
# pinned by the same cases — scripts/speech_harness.ts and tests/test_speech.py —
# so a rule added to one and not the other fails a test rather than drifting
# quietly. If they ever must diverge, that is a bug, not a decision.
#
# SEPARATORS, NOT WORDS. Every rule inserts spaces, commas and digits — never
# English or Arabic prose. A comma is a pause in both languages, so one
# implementation serves both and there is no translated string to maintain. It
# also means this can never put words in Aisha's mouth that she did not write.

_REF = re.compile(r"\bTD-(\d{4})-([A-Z0-9]{4})\b")
_MOBILE = re.compile(r"\+971[\s-]?(\d{2})[\s-]?(\d{3})[\s-]?(\d{4})\b")
# NOTE THE LOOKAHEAD RATHER THAN \b. Python's \b is Unicode-aware and WOULD work
# here, unlike JavaScript's — but the two implementations are kept structurally
# identical on purpose, so a reader comparing them sees one rule, not two
# dialects of one rule. The JS side needs the lookahead (its \b is ASCII-only and
# silently skipped every Arabic time); matching it here keeps them auditable
# side by side.
_TIME = re.compile(r"\b(\d{1,2}):00(\s*)(am|pm|AM|PM|ص|م)(?![A-Za-z؀-ۿ])")
_DECOR = re.compile(r"[*_`#]")


def for_speech(text: str) -> str:
    """Prepare one message for the synthesiser. Pure; safe on any input.

    The bubble's text is NEVER altered — this produces a throwaway string, so
    what is read and what is on screen stay the same message.
    """
    out = _REF.sub(lambda m: f"T D, {m.group(1)}, {' '.join(m.group(2))}", text)
    out = _MOBILE.sub(
        lambda m: f"+971, {m.group(1)}, {m.group(2)}, {m.group(3)[:2]} {m.group(3)[2:]}",
        out,
    )
    out = _TIME.sub(r"\1\2\3", out)
    out = _DECOR.sub(" ", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def _voice_for(lang: Language) -> str:
    return TTS_VOICE_AR if lang is Language.ar else TTS_VOICE_EN


def _cache_key(spoken: str, lang: Language, voice: str, model: str) -> str:
    """Hash every input that changes the AUDIO.

    Voice and model are in the key so switching either misses the cache instead
    of serving audio that no longer matches the configured persona. The SPOKEN
    text is hashed, not the raw text, so a normalisation change also invalidates
    correctly — the words that were actually said are what the clip represents.
    """
    raw = f"{spoken}\x00{lang.value}\x00{voice}\x00{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _client():
    """Lazy OpenAI client — constructed per call, never at import, so the app
    boots and the text demo runs with OPENAI_API_KEY unset."""
    from openai import OpenAI

    return OpenAI(api_key=OPENAI_API_KEY)


def synthesize_turn(
    db: Session,
    *,
    session_id: str,
    turn_id: int,
    now: datetime | None = None,
) -> dict:
    """Audio for one of Aisha's turns. Never raises.

    Returns {"ok": True, "clip": SpeechClip} or {"ok": False, "reason": str}.
    Reasons: turn_not_found | not_an_agent_turn | empty_turn | text_too_long |
             tts_not_configured | synthesis_failed

    ERRORS ARE DATA, as everywhere in this layer. A vendor outage must degrade to
    the browser's on-device voice, not break the page — which is why the caller
    gets a reason it can act on rather than an exception.
    """
    now = now or datetime.now()

    turn = db.get(ConversationTurn, turn_id)
    # THE SESSION CHECK IS PART OF THE LOOKUP, not a separate gate: a turn from
    # someone else's conversation is simply not found, so a caller guessing turn
    # ids learns nothing and can never have another customer's reply read aloud.
    if turn is None or turn.session_id != session_id:
        return {"ok": False, "reason": "turn_not_found"}
    if turn.speaker is not Speaker.agent:
        # Only Aisha is read aloud. The customer knows what they said, and their
        # own voice note is already playable where they left it.
        return {"ok": False, "reason": "not_an_agent_turn"}

    spoken = for_speech(turn.text or "")
    if not spoken:
        return {"ok": False, "reason": "empty_turn"}
    if len(spoken) > TTS_MAX_CHARS:
        return {"ok": False, "reason": "text_too_long", "chars": len(spoken)}

    voice = _voice_for(turn.lang)
    key = _cache_key(spoken, turn.lang, voice, TTS_MODEL)

    cached = db.scalar(
        select(SpeechClip).where(
            SpeechClip.session_id == session_id,
            SpeechClip.cache_key == key,
        )
    )
    if cached is not None and cached.expires_at > now:
        # Deliberately NOT audited: a replay is not a new disclosure. The text
        # went to the vendor once, and that once is already in the trail; logging
        # every tap would bury the fact that matters under UI noise.
        return {"ok": True, "clip": cached, "cached": True}

    if not OPENAI_API_KEY:
        return {"ok": False, "reason": "tts_not_configured"}

    try:
        resp = _client().audio.speech.create(
            model=TTS_MODEL,
            voice=voice,
            input=spoken,
            instructions=TTS_INSTRUCTIONS,
        )
        data = resp.content
    except Exception as exc:  # noqa: BLE001 — vendor failure is data, not a 500
        log.warning("tts failed (turn=%s): %s", turn_id, exc)
        _audit(db, session_id, turn_id, "error", detail=type(exc).__name__)
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    if not data:
        _audit(db, session_id, turn_id, "empty")
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    if cached is not None:
        # Expired row for this exact key: refresh it in place rather than insert,
        # or the unique (session, key) constraint would reject the new clip.
        cached.audio = data
        cached.size_bytes = len(data)
        cached.created_at = now
        cached.expires_at = now + timedelta(hours=VOICE_RETENTION_HOURS)
        clip = cached
    else:
        clip = SpeechClip(
            cache_key=key,
            session_id=session_id,
            turn_id=turn_id,
            lang=turn.lang,
            voice=voice,
            model=TTS_MODEL,
            mime="audio/mpeg",
            size_bytes=len(data),
            audio=data,
            created_at=now,
            expires_at=now + timedelta(hours=VOICE_RETENTION_HOURS),
        )
        db.add(clip)
    db.flush()

    _audit(db, session_id, turn_id, "ok", lang=turn.lang.value, voice=voice, chars=len(spoken))
    db.flush()
    return {"ok": True, "clip": clip, "cached": False}


class SentenceChunker:
    """Split a streamed reply into speakable sentences as tokens arrive (P4).

    Feeding is incremental: `feed` receives raw token text in stream order,
    and `push` is called with each COMPLETE sentence the moment its boundary
    passes — which is what lets the first sentence reach the synthesiser while
    the rest of the reply is still generating. `flush` force-emits whatever is
    held (end of turn, or a text_break: the bubble sealed, so the sentence is
    as finished as it will ever be).

    MIN-LENGTH HOLD: a boundary only emits once the held text is at least
    `min_chars` (16 — long enough to swallow "Perfect, Mr." and "Dr.", short
    enough that a genuine terse sentence still gets its own breath), so the
    abbreviation trap rides along with the sentence that follows instead of
    being synthesised as a clipped half-thought. English-first tuning (product decision 2026-07-27:
    the call is English-only for now); the boundary set still includes the
    Arabic question mark so an Arabic reply degrades to coarser chunks, never
    to none.

    Pure accumulator — no I/O, no thread opinions. The caller decides what
    `push` does (the gateway posts to the speaker task's queue).
    """

    _BOUNDARY = re.compile(r"[.!?؟…]+[)\"'»]*\s")
    # First-clause fast start (call-only, 2026-07-27): a comma/colon/dash also
    # ends the VERY FIRST chunk, and a run with no punctuation at all is cut at
    # the last space past the cap — the customer hears "Perfect, Mr Sharma,"
    # while the rest of the sentence is still being written. Only the opening:
    # later chunks keep whole sentences for prosody.
    _CLAUSE = re.compile(r"[,;:—–]\s")
    _FIRST_CAP = 60

    def __init__(
        self,
        push: Callable[[str], None],
        *,
        min_chars: int = 16,
        first_clause: bool = False,
        group_chars: int | None = None,
    ) -> None:
        self._push = push
        self._min_chars = min_chars
        self._first_clause = first_clause
        # After the opening, hold until this many characters before a boundary
        # emits (None = every sentence on its own). Bigger later chunks read
        # with a connected prosodic arc — one synthesis breathes across two
        # sentences the way a person does — and cost nothing, because the
        # speaker prefetches them while the opening is still being heard.
        self._group_chars = group_chars
        self._spoke_any = False
        self._held = ""

    def _emit(self, piece: str) -> None:
        self._spoke_any = True
        self._push(piece)

    def feed(self, text: str) -> None:
        if not text:
            return
        self._held += text
        emitted_upto = 0
        piece_start = 0
        pieces: list[str] = []
        min_now = (
            self._group_chars
            if (self._spoke_any and self._group_chars is not None)
            else self._min_chars
        )
        for match in self._BOUNDARY.finditer(self._held):
            piece = self._held[piece_start : match.end()]
            # Short segments (an abbreviation's dot, a terse "Done.") merge
            # into the piece that follows rather than being spoken clipped.
            if len(piece.strip()) >= min_now:
                pieces.append(piece.strip())
                piece_start = match.end()
                emitted_upto = match.end()
        if pieces:
            self._held = self._held[emitted_upto:]
            for piece in pieces:
                self._emit(piece)
            return
        # Nothing sentence-shaped yet. In fast-start mode the OPENING may leave
        # early anyway: at the first roomy clause boundary, or hard-capped at
        # the last space once the run is long enough that waiting buys nothing.
        if not self._first_clause or self._spoke_any:
            return
        # The first clause boundary with ROOM before it — "Perfect," alone is
        # too clipped to speak, "Perfect, Mr. Sharma," is an opening.
        for clause in self._CLAUSE.finditer(self._held):
            if len(self._held[: clause.end()].strip()) >= self._min_chars:
                piece, self._held = self._held[: clause.end()], self._held[clause.end() :]
                self._emit(piece.strip())
                return
        if len(self._held) >= self._FIRST_CAP:
            cut = self._held.rfind(" ", self._min_chars, self._FIRST_CAP)
            if cut > 0:
                piece, self._held = self._held[:cut], self._held[cut + 1 :]
                self._emit(piece.strip())

    def flush(self) -> None:
        spoken = self._held.strip()
        self._held = ""
        if spoken:
            self._emit(spoken)


def synthesize_for_call(
    db: Session,
    *,
    session_id: str,
    turn_id: int | None,
    text: str,
    lang: Language,
) -> dict:
    """Audio for (part of) one of Aisha's call replies. Never raises.

    Returns {"ok": True, "audio": bytes, "mime": "audio/mpeg"} or
    {"ok": False, "reason": str}. Reasons: empty_text | text_too_long |
    tts_not_configured | synthesis_failed.

    THE CALL PATH, DELIBERATELY UNCACHED AND UNSTORED — the mirror image of
    synthesize_turn above, and both differences are decisions:

    NO SpeechClip. Its cache keys on the FULL turn text, which is useless once
    P4 streams sentence-sized chunks — and a call reply is spoken exactly once,
    at generation time, so there is no replay to serve. Replaying a call turn
    later IS read-aloud, which has its own cached path. Not storing also keeps
    the PDPL surface flat: nothing of Aisha's call audio persists anywhere.

    TEXT IS PASSED IN, NOT LOOKED UP — but only ever by the gateway, from a
    TurnResult the runner just persisted, so the grounding property holds by
    construction: everything spoken is a stored turn's text (P4 will pass
    sentence chunks OF that text). This function is not reachable from any
    public endpoint with client-supplied text; keep it that way.

    Accepts a chunk or a whole reply — the P4 sentence streamer calls it per
    sentence with the same turn_id, which is why normalisation happens here
    (per chunk) and why the audit rides on every call: each vendor disclosure
    is its own fact in the trail (rule 15).
    """
    spoken = for_speech(text or "")
    if not spoken:
        return {"ok": False, "reason": "empty_text"}
    if len(spoken) > TTS_MAX_CHARS:
        return {"ok": False, "reason": "text_too_long", "chars": len(spoken)}
    if not OPENAI_API_KEY:
        return {"ok": False, "reason": "tts_not_configured"}

    voice = _voice_for(lang)
    try:
        resp = _client().audio.speech.create(
            model=TTS_MODEL,
            voice=voice,
            input=spoken,
            instructions=TTS_INSTRUCTIONS,
        )
        data = resp.content
    except Exception as exc:  # noqa: BLE001 — vendor failure is data, not a 500
        log.warning("call tts failed (turn=%s): %s", turn_id, exc)
        _audit(db, session_id, turn_id, "error", channel="call", detail=type(exc).__name__)
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    if not data:
        _audit(db, session_id, turn_id, "empty", channel="call")
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    _audit(
        db, session_id, turn_id, "ok",
        channel="call", lang=lang.value, voice=voice, chars=len(spoken),
    )
    db.flush()
    return {"ok": True, "audio": data, "mime": "audio/mpeg"}


class SynthAborted(Exception):
    """Raised by a stream_for_call chunk callback to stop synthesis mid-clip —
    the barge-in path. Exiting the vendor's `with` closes the HTTP stream, so
    the model stops generating audio server-side too. Never propagated."""


def stream_for_call(
    db: Session,
    *,
    session_id: str,
    text: str,
    lang: Language,
    on_chunk: Callable[[bytes], None],
    turn_id: int | None = None,
) -> dict:
    """synthesize_for_call's STREAMING twin (call-only, 2026-07-27): audio
    bytes go to `on_chunk` AS THE VENDOR PRODUCES THEM instead of after the
    clip completes — the client starts playing while the tail is still being
    synthesised, which shaves the whole synthesis wait off first-word latency.

    Same guards, same audit, same nothing-stored posture as the batch twin.
    `on_chunk` may raise SynthAborted (barge-in): the vendor stream closes,
    the reason comes back as "aborted", and the caller stays quiet about it —
    the customer asked for silence, not an error.

    Returns {"ok": True, "mime": "audio/mpeg", "bytes": n} or
    {"ok": False, "reason": empty_text | text_too_long | tts_not_configured |
    aborted | synthesis_failed}.
    """
    spoken = for_speech(text or "")
    if not spoken:
        return {"ok": False, "reason": "empty_text"}
    if len(spoken) > TTS_MAX_CHARS:
        return {"ok": False, "reason": "text_too_long", "chars": len(spoken)}
    if not OPENAI_API_KEY:
        return {"ok": False, "reason": "tts_not_configured"}

    voice = _voice_for(lang)
    total = 0
    try:
        with _client().audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=voice,
            input=spoken,
            # The phone-conversation tone, not the read-aloud narrator's —
            # this function is only ever the live call speaking.
            instructions=CALL_TTS_INSTRUCTIONS,
        ) as resp:
            for part in resp.iter_bytes(8192):
                if part:
                    total += len(part)
                    on_chunk(part)
    except SynthAborted:
        _audit(db, session_id, turn_id, "aborted", channel="call", chars=len(spoken))
        db.flush()
        return {"ok": False, "reason": "aborted"}
    except Exception as exc:  # noqa: BLE001 — vendor failure is data, not a 500
        log.warning("call tts stream failed: %s", exc)
        _audit(db, session_id, turn_id, "error", channel="call", detail=type(exc).__name__)
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    if total == 0:
        _audit(db, session_id, turn_id, "empty", channel="call")
        db.flush()
        return {"ok": False, "reason": "synthesis_failed"}

    _audit(
        db, session_id, turn_id, "ok",
        channel="call", lang=lang.value, voice=voice, chars=len(spoken),
    )
    db.flush()
    return {"ok": True, "mime": "audio/mpeg", "bytes": total}


def _audit(
    db: Session, session_id: str, turn_id: int | None, outcome: str, **extra: object
) -> None:
    """Audit one SYNTHESIS (rule 15) — a disclosure, not a button press.

    THE SPOKEN TEXT IS NOT IN THE PAYLOAD. It is already the ConversationTurn,
    and copying it here would put a second copy of a reply containing the
    customer's name and mobile into a table the console renders wholesale and
    rule 12 does not erase. What is recorded is the fact the D1 checklist needs
    to be able to count: this turn's text went to this vendor, in this language.

    actor=system: the customer asked to hear it, but sending their data to a US
    endpoint is something WE did — the same reasoning as transcription.
    """
    audit.record(
        db,
        session=session_id,
        actor=Actor.system,
        type=AuditType.voice,
        payload={
            "action": "synthesize",
            "turn_id": turn_id,
            "outcome": outcome,
            "model": TTS_MODEL,
            "vendor": "openai",
            **{k: v for k, v in extra.items() if v is not None},
        },
    )


def delete_for_session(
    db: Session, *, session_id: str, correlation_id: str | None = None
) -> int:
    """Hard-delete this session's clips (rule 12). Returns the count.

    Called from memory.forget alongside the voice notes. These clips are audio of
    the customer's own name, mobile and booking reference being read out — an
    erasure that kept them would leave the most quotable form of the data behind.

    BY SESSION, NOT BY CUSTOMER, because that is how the rows are keyed: a clip
    belongs to a conversation, not to an identified person (identification may
    never have happened). The session is the widest honest scope, and it is the
    conversation the erasure is being requested in.
    """
    clips = db.scalars(
        select(SpeechClip).where(SpeechClip.session_id == session_id)
    ).all()
    for clip in clips:
        db.delete(clip)
    db.flush()

    if clips:
        audit.record(
            db,
            session=session_id,
            actor=Actor.customer,
            type=AuditType.erasure,
            payload={
                "action": "forget_me_speech",
                "deleted": len(clips),
                "turn_ids": sorted({c.turn_id for c in clips}),
            },
            correlation_id=correlation_id,
        )
        db.flush()
    return len(clips)


def purge_expired(db: Session, *, now: datetime | None = None) -> int:
    """Delete clips past their retention horizon. Returns the count.

    Not audited, for the same reason VoiceNote's sweep is not: expiry is a
    property of the schedule, not an event in anyone's conversation.
    """
    now = now or datetime.now()
    expired = db.scalars(
        select(SpeechClip).where(SpeechClip.expires_at <= now)
    ).all()
    for clip in expired:
        db.delete(clip)
    if expired:
        db.flush()
        log.info("purged %d expired speech clip(s)", len(expired))
    return len(expired)
