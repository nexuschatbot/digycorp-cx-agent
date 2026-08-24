"""WS /api/call/{session_id} — the voice-call gateway (voice phase).

THE SAME AGENT, WITH EARS AND A MOUTH. A call is a hands-free loop around the
ordinary turn: mic audio up this WebSocket, STT, run_turn() — the SAME LangGraph
graph, tools, checkpoint and audit choke point as /api/chat — then TTS of the
reply back down. No second model, no second Aisha: everything spoken in her
voice is a persisted agent turn's text, and every fact still comes from a tool.

AD-C AMENDMENT, NOT VIOLATION. AD-C deferred WebSockets "to the voice phase"
because only bidirectional audio justifies them. This is that phase. WS exists
for THIS CHANNEL ONLY — /api/chat stays SSE, untouched, and the two share one
turn implementation (app/agent/runner.run_turn) so they cannot drift.

WIRE PROTOCOL
-------------
Uplink:   binary frames = PCM16 mono at `sample_rate` (little-endian)
          JSON text frames:
            {"type": "start", "sample_rate": 24000}     # first message
            {"type": "end_turn"}                        # client fallback VAD;
                                                        #   with the relay up it
                                                        #   becomes a manual
                                                        #   vendor commit
            {"type": "barge_in"}                        # P4: the customer spoke
                                                        #   over Aisha. Client-
                                                        #   detected (it owns
                                                        #   playback); remaining
                                                        #   speech is abandoned,
                                                        #   the TEXT never is.
            {"type": "mute"} / {"type": "unmute"}
            {"type": "hangup"}
Downlink: JSON text frames:
            {"type": "call_started", "max_minutes": n}
            {"type": "vad", "server": bool}             # P3: who owns turn-
                                                        #   taking. true = the
                                                        #   vendor's server VAD;
                                                        #   false = the client's
                                                        #   silence detector.
                                                        #   Can flip to false
                                                        #   mid-call if the
                                                        #   relay dies.
            {"type": "state", "value": "listening|thinking|speaking"}
            {"type": "utterance", "audio_id", "text", "lang", "failed": bool}
            {"type": "utterance_failed", "reason"}      # nothing to act on —
                                                        #   client returns to
                                                        #   listening
            {"type": "token"|"tool_event"|"booking"|"text_break"|"closed"
                    |"done"|"error", ...}                # run_turn events,
                                                        #   payloads exactly as
                                                        #   the SSE contract
            {"type": "audio_start", "mime"}             # then binary TTS frames
            {"type": "audio_end"}                       # P4: one segment PER
                                                        #   SENTENCE — several
                                                        #   start/end pairs per
                                                        #   reply, sent while
                                                        #   later sentences are
                                                        #   still generating.
                                                        #   The client queues
                                                        #   and plays them
                                                        #   gaplessly in order.
            {"type": "tts_failed", "reason"}            # client may fall back
                                                        #   to on-device voice
            {"type": "call_ended", "reason"}            # then the socket closes
          binary frames = the reply's synthesised audio, between
          audio_start/audio_end.

call_ended reasons: hangup | max_minutes | silence | closed | call_disabled |
voice_not_configured | session_closed | error.

STATE MACHINE: listening -> (end_turn) -> thinking -> speaking -> listening.
Audio received outside `listening` is DISCARDED in v0 — barge-in lands in P4,
and silently buffering speech Aisha will not hear would be worse than dropping
it (the customer would think she heard them).

ABORT (AD-E, unchanged): one threading.Event per call, shared with every
run_turn on it. Client disconnect or hangup sets it — generation stops within a
token, tools stop before dispatch, the partial transcript rolls back.

SILENCE HANGUP: measured from COMPLETED ACTIVITY (call start, an utterance, a
finished reply) — not from received frames, because in v0 the client streams
continuously and silence still arrives as bytes. The call-channel sibling of
rule 19's idle ladder; CALL_MAX_MINUTES is the unconditional cost ceiling.

PERSISTENCE: each utterance is stored through the ordinary VoiceNote path
(WAV of the buffered PCM) — session gate, 48h expiry, rule-12 erasure and
customer backfill all inherited — and stamped as audio_id on the customer's
ConversationTurn, so a call thread replays exactly like a voice-note thread.
Nothing of Aisha's call audio is stored (see synthesize_for_call).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.runner import run_turn
from app.config import (
    CALL_ENABLED,
    CALL_MAX_MINUTES,
    CALL_MODE,
    CALL_SILENCE_HANGUP_SECONDS,
    CALL_UTTERANCE_MAX_SECONDS,
    OPENAI_API_KEY,
    STT_MODEL,
    VOICE_MAX_BYTES,
)
from app.db import SessionLocal
from app.models import Actor, AuditType, Language
from app.services import audit as audit_service
from app.services import conversation as conversation_service
from app.services import speech as speech_service
from app.services import voice as voice_service
from app.services.realtime_stt import VENDOR_SAMPLE_RATE, make_transcriber, resample_pcm16

log = logging.getLogger("app.api.call")
router = APIRouter()

# An "utterance" shorter than this is a click or a breath, not speech — discard
# rather than spend a vendor round-trip learning nothing. ~0.15 s at 16 kHz.
_MIN_UTTERANCE_BYTES = 4800

_DEFAULT_SAMPLE_RATE = 16_000
_SAMPLE_RATES = {8_000, 16_000, 24_000, 48_000}


def _audit_call(db, session_id: str, action: str, *, actor: Actor, **extra: object) -> None:
    """One call-lifecycle fact in the trail (rule 15). Never breaks the call."""
    try:
        audit_service.record(
            db,
            session=session_id,
            actor=actor,
            type=AuditType.voice,
            payload={"action": action, **extra},
        )
        db.commit()
    except Exception:  # noqa: BLE001 — telemetry must never cost the call
        log.warning("call audit failed — session=%s", session_id, exc_info=True)
        db.rollback()


@router.websocket("/api/call/{session_id}")
async def call_ws(ws: WebSocket, session_id: str) -> None:
    await ws.accept()

    # Refusals are spoken in the protocol, not as bare close codes: the client
    # shows a stated reason (an absent/refused control must say why — the same
    # rule the mic button follows on plain HTTP).
    if not CALL_ENABLED:
        await ws.send_json({"type": "call_ended", "reason": "call_disabled"})
        await ws.close(code=4403)
        return
    if not OPENAI_API_KEY:
        # Without STT the call cannot hear and without TTS it cannot speak —
        # unlike voice notes, there is no degraded mode worth offering.
        await ws.send_json({"type": "call_ended", "reason": "voice_not_configured"})
        await ws.close(code=4503)
        return

    db = SessionLocal()
    try:
        closed = conversation_service.is_closed(db, session_id=session_id)
    finally:
        db.close()
    if closed:
        # Rule 19: a closed session is ended. A new message starts a fresh
        # conversation — and so must a new call, on a fresh session_id.
        await ws.send_json({"type": "call_ended", "reason": "session_closed"})
        await ws.close(code=4409)
        return

    # Engine switch (2026-07-27, developer decision): "realtime" = OpenAI
    # speech-to-speech speaking as Aisha with OUR tools (sub-second, GPT's
    # judgement); "pipeline" = STT -> the Claude agent -> TTS (one brain,
    # ~4 s). Same client protocol either way; see call_realtime.py.
    if CALL_MODE == "realtime":
        from app.api.call_realtime import RealtimeCallSession

        await RealtimeCallSession(ws, session_id).run()
        return
    await _CallSession(ws, session_id).run()


class _CallSession:
    """One live call: receive loop, ordered sender, watchdog, turn pipeline."""

    def __init__(self, ws: WebSocket, session_id: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self.loop: asyncio.AbstractEventLoop | None = None
        # ONE ordered outbound queue for JSON and binary alike — a reply's
        # audio_start / frames / audio_end must never interleave wrongly with
        # its own token events.
        self.out: asyncio.Queue = asyncio.Queue()
        self.cancel = threading.Event()  # AD-E abort, shared with run_turn
        self.state = "listening"
        self.muted = False
        self.sample_rate = _DEFAULT_SAMPLE_RATE
        self.pcm = bytearray()
        # The streaming STT relay (P3), or None = the v0 batch path. None is a
        # legitimate steady state (no key, connect failed, relay died mid-call)
        # — the client is told which mode is live via the `vad` event and runs
        # its own silence detection when it is on the hook for turn-taking.
        self.transcriber = None
        self.turn_task: asyncio.Task | None = None
        # P4 barge-in. tts_abort silences the CURRENT turn's remaining speech
        # (synthesis is skipped, nothing more is sent); barged widens the mic
        # so the interrupting utterance is captured while the interrupted turn
        # drains; the two pendings hold the next turn's trigger until it can
        # start. All four reset in the pipeline's finally.
        self.tts_abort = False
        self.barged = False
        self.pending_transcript: str | None = None
        self.pending_end_turn = False
        self.started_mono = time.monotonic()
        # COMPLETED activity (see module docstring), not received frames.
        self.last_activity_mono = self.started_mono
        self.ended = False
        self.end_reason: str | None = None
        self._t: dict[str, float] = {}  # temporary latency instrumentation

    # ---------------------------------------------------------------- sending
    # ---- temporary latency instrumentation (2026-07-27 debugging) ----------
    def _mark(self, label: str, since: str | None = None) -> None:
        now = time.monotonic()
        self._t[label] = now
        if since and since in self._t:
            log.info(
                "call timing %-16s +%6.0f ms  (session=%s)",
                label, (now - self._t[since]) * 1000, self.session_id[:8],
            )
        else:
            log.info("call timing %-16s  (session=%s)", label, self.session_id[:8])

    def _put(self, item: tuple) -> None:
        self.out.put_nowait(item)

    def _put_threadsafe(self, item: tuple) -> None:
        """From run_turn's worker thread (same bridge shape as chat.py)."""
        assert self.loop is not None
        try:
            self.loop.call_soon_threadsafe(self.out.put_nowait, item)
        except RuntimeError:  # loop gone — client long gone
            pass

    def _send_json(self, payload: dict) -> None:
        self._put(("json", payload))

    def _set_state(self, value: str) -> None:
        self.state = value
        self._send_json({"type": "state", "value": value})

    async def _sender(self) -> None:
        """Drain the ordered queue onto the socket. The ONLY writer after
        run() starts, so ordering is the queue's ordering."""
        while True:
            kind, payload = await self.out.get()
            try:
                if kind == "json":
                    await self.ws.send_json(payload)
                elif kind == "bytes":
                    await self.ws.send_bytes(payload)
                elif kind == "close":
                    await self.ws.close()
                    return
            except Exception:  # noqa: BLE001 — socket already dead
                return

    # ----------------------------------------------------------------- ending
    def _end(self, reason: str) -> None:
        """Idempotent. Speaks call_ended, then closes — in queue order."""
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        self.cancel.set()
        self._send_json({"type": "call_ended", "reason": reason})
        self._put(("close", None))

    # --------------------------------------------------------------- watchdog
    async def _watchdog(self) -> None:
        while not self.ended:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - self.started_mono > CALL_MAX_MINUTES * 60:
                self._end("max_minutes")
                return
            if (
                self.state == "listening"
                and self.turn_task is None
                and now - self.last_activity_mono > CALL_SILENCE_HANGUP_SECONDS
            ):
                self._end("silence")
                return

    # ------------------------------------------------------------ turn thread
    def _transcribe_sync(self, wav: bytes, duration_ms: int) -> dict:
        """Own session, own commit — the voice route's exact shape."""
        db = SessionLocal()
        try:
            result = voice_service.transcribe_and_store(
                db,
                session_id=self.session_id,
                audio=wav,
                mime="audio/wav",
                duration_ms=duration_ms,
                # The CALL's limit, not the voice-note one: _on_bytes lets an
                # utterance run to CALL_UTTERANCE_MAX_SECONDS, and judging it
                # by VOICE_MAX_SECONDS silently discarded every 30–60 s answer
                # on this batch path (found 2026-08-03).
                max_seconds=CALL_UTTERANCE_MAX_SECONDS,
            )
            db.commit()
            return result
        except Exception:  # noqa: BLE001 — errors are data on this path
            log.exception("call transcription failed — session=%s", self.session_id)
            db.rollback()
            return {"ok": False, "reason": "transcription_failed", "audio_id": None}
        finally:
            db.close()

    def _store_sync(self, wav: bytes, duration_ms: int, transcript: str) -> dict:
        """The relay path's storage: transcript already in hand, no vendor."""
        db = SessionLocal()
        try:
            result = voice_service.store_call_utterance(
                db,
                session_id=self.session_id,
                audio=wav,
                mime="audio/wav",
                transcript=transcript,
                stt_model=STT_MODEL,
                duration_ms=duration_ms,
            )
            db.commit()
            return result
        except Exception:  # noqa: BLE001
            log.exception("call utterance store failed — session=%s", self.session_id)
            db.rollback()
            return {"ok": False, "reason": "transcription_failed", "audio_id": None}
        finally:
            db.close()

    def _synth_stream_to(self, text: str, buf: asyncio.Queue) -> dict:
        """Streaming synthesis into a per-segment buffer queue (2026-07-27):
        bytes land in `buf` AS THE VENDOR PRODUCES THEM, and a None sentinel
        closes it — ALWAYS, success or failure, or the consumer waits forever.
        The buffer (not the wire) is the target so the speaker can PREFETCH:
        segment N+1 synthesises into its buffer while N still streams/plays,
        which is what removed the ~1.5 s of silence between spoken sentences
        measured on the first instrumented call. One voice per call
        (TTS_VOICE_EN via Language.en — see the speaker)."""

        def on_chunk(part: bytes) -> None:
            if self.ended or self.tts_abort:
                raise speech_service.SynthAborted()
            if "first_audio" not in self._t or self._t["first_audio"] < self._t.get("synth_start", 0):
                self._mark("first_audio", since="synth_start")
            assert self.loop is not None
            self.loop.call_soon_threadsafe(buf.put_nowait, part)

        db = SessionLocal()
        try:
            result = speech_service.stream_for_call(
                db,
                session_id=self.session_id,
                text=text,
                lang=Language.en,
                on_chunk=on_chunk,
            )
            db.commit()
            return result
        except Exception:  # noqa: BLE001
            log.exception("call synthesis failed — session=%s", self.session_id)
            db.rollback()
            return {"ok": False, "reason": "synthesis_failed"}
        finally:
            db.close()
            assert self.loop is not None
            self.loop.call_soon_threadsafe(buf.put_nowait, None)

    async def _run_pipeline(self, pcm: bytes, transcript: str | None = None) -> None:
        """One utterance end-to-end: STT -> agent turn -> TTS. Runs as a task;
        the receive loop keeps draining the socket meanwhile (hangup must work
        mid-turn).

        `transcript` present = the streaming relay already heard the words
        (P3): storage only, no second vendor round-trip. None = the v0 batch
        path transcribes the buffer now. Everything downstream is one code
        path — the envelopes are identical by design."""
        try:
            self._mark("turn_start", since="transcript")
            duration_ms = int(len(pcm) / 2 / self.sample_rate * 1000)
            wav = voice_service.pcm16_to_wav(bytes(pcm), sample_rate=self.sample_rate)
            if transcript is None:
                stt = await asyncio.to_thread(self._transcribe_sync, wav, duration_ms)
            else:
                stt = await asyncio.to_thread(self._store_sync, wav, duration_ms, transcript)
            self._mark("stored", since="turn_start")
            if self.ended:
                return

            if stt.get("ok"):
                message, voice_failed = stt["text"], False
                self._send_json({
                    "type": "utterance",
                    "audio_id": stt.get("audio_id"),
                    "text": stt.get("text", ""),
                    "lang": stt.get("lang"),
                    "duration_ms": duration_ms,
                    "failed": False,
                })
            elif stt.get("audio_id"):
                # Stored but unheard — the voice-note recovery: an empty
                # customer turn with the player, and Aisha asks them to repeat
                # (never guesses what they said, rule 2).
                message, voice_failed = "", True
                self._send_json({
                    "type": "utterance",
                    "audio_id": stt.get("audio_id"),
                    "text": "",
                    "lang": None,
                    "duration_ms": duration_ms,
                    "failed": True,
                })
            else:
                # Nothing stored, nothing to act on (empty, oversized…) —
                # tell the client so it returns to listening.
                self._send_json({
                    "type": "utterance_failed",
                    "reason": stt.get("reason", "transcription_failed"),
                })
                return

            # P4: SPEAK WHILE GENERATING. The chunker watches the token stream
            # and emits complete sentences; the speaker task synthesises and
            # ships each one while later sentences are still being written —
            # Aisha's first words land seconds before her reply finishes. A
            # text_break flushes (the bubble sealed, so the sentence is as
            # finished as it will ever be), and the events still reach the
            # client verbatim — the thread renders exactly as before.
            loop = asyncio.get_running_loop()
            tts_queue: asyncio.Queue = asyncio.Queue()
            first_flags = {"token": False, "chunk": False}

            def _push_chunk(s: str) -> None:
                if not first_flags["chunk"]:
                    first_flags["chunk"] = True
                    self._mark("first_chunk", since="first_token")
                loop.call_soon_threadsafe(tts_queue.put_nowait, s)

            chunker = speech_service.SentenceChunker(
                push=_push_chunk,
                # Fast start: the opening clause is spoken before its sentence
                # finishes while the rest is still being written.
                first_clause=True,
                # After the opening, ~two sentences per synthesis: one prosodic
                # arc instead of disconnected lines (the "she is reading" fix);
                # prefetch hides the bigger chunks' latency entirely.
                group_chars=100,
            )

            def on_event(event: str, data: dict) -> None:
                if event == "token":
                    if not first_flags["token"]:
                        first_flags["token"] = True
                        self._mark("first_token", since="stored")
                    chunker.feed(data.get("text", ""))
                elif event == "text_break":
                    chunker.flush()
                self._put_threadsafe(("json", {"type": event, **data}))

            speaker = loop.create_task(self._speaker(tts_queue))
            try:
                result = await asyncio.to_thread(
                    run_turn,
                    self.session_id,
                    message,
                    on_event=on_event,
                    cancel=self.cancel,
                    audio_id=stt.get("audio_id"),
                    voice_failed=voice_failed,
                    # The voice anchor: this reply will be SPOKEN, so the model
                    # is told to answer at phone length (register, not rules).
                    voice_call=True,
                )
                if result.status == "done":
                    chunker.flush()
                else:
                    # Aborted or errored: nothing further deserves a voice.
                    self.tts_abort = True
            finally:
                # The sentinel takes the SAME call_soon path as the chunks:
                # put directly it would overtake a final flush still sitting
                # in the loop's callback queue, and the last sentence would
                # silently never be spoken (caught by test_call_streaming).
                loop.call_soon_threadsafe(tts_queue.put_nowait, None)
                await speaker

            if self.ended or result.status == "aborted":
                return
            if result.closed:
                # Aisha signed off (rule 19) — the call ends with the chat,
                # and only after the speaker finished saying the farewell.
                self._end("closed")
                return
        except Exception:  # noqa: BLE001 — one bad utterance must not kill the call
            log.exception("call pipeline failed — session=%s", self.session_id)
            self._send_json({"type": "error", "message": "Something went wrong on our side. Please try again."})
        finally:
            self.turn_task = None
            self.tts_abort = False
            pending_transcript, self.pending_transcript = self.pending_transcript, None
            pending_end, self.pending_end_turn = self.pending_end_turn, False
            self.barged = False
            if not self.ended:
                self.last_activity_mono = time.monotonic()
                # A barge-in may have parked the NEXT turn while this one
                # drained — start it now rather than going quiet on a customer
                # who already spoke.
                if pending_transcript is not None:
                    self._begin_relay_turn(pending_transcript)
                elif pending_end:
                    await self._start_batch_turn()
                else:
                    self._set_state("listening")

    async def _speaker(self, queue: asyncio.Queue) -> None:
        """Speak sentence chunks as they arrive (P4), PREFETCHED (2026-07-27).

        Two stages, both ordered: a producer spawns synthesis for each chunk
        the moment it exists (at most two in flight — the one playing and the
        next), each into its own buffer queue; the consumer walks segments in
        order, streaming each buffer to the client between its audio_start /
        audio_end. Segment N+1's ~1.5 s time-to-first-byte therefore happens
        WHILE N is still being heard — measured, that gap was the single
        biggest reason a reply sounded like sentences being read off a list.

        tts_abort (barge-in, aborted turn, dead vendor) stops spawning and
        skips delivery; in-flight synthesis kills itself at its next chunk
        (SynthAborted closes the vendor stream). ONE VOICE PER CALL — see
        _synth_stream_to."""
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(2)  # the playing segment + the prefetching one
        segments: asyncio.Queue = asyncio.Queue()

        async def synth(chunk: str, buf: asyncio.Queue) -> dict:
            async with sem:
                if self.ended or self.tts_abort:
                    buf.put_nowait(None)
                    return {"ok": False, "reason": "aborted"}
                return await asyncio.to_thread(self._synth_stream_to, chunk, buf)

        async def producer() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await segments.put(None)
                    return
                if self.ended or self.tts_abort:
                    continue  # asked for quiet — do not even start
                self._mark("synth_start", since="first_chunk")
                buf: asyncio.Queue = asyncio.Queue()
                await segments.put((buf, loop.create_task(synth(chunk, buf))))

        producer_task = loop.create_task(producer())
        tts_dead = False
        try:
            while True:
                item = await segments.get()
                if item is None:
                    return
                buf, task = item
                skip = self.ended or self.tts_abort or tts_dead
                if not skip:
                    if self.state != "speaking":
                        self._set_state("speaking")
                    self._send_json({"type": "audio_start", "mime": "audio/mpeg"})
                while True:
                    part = await buf.get()
                    if part is None:
                        break
                    if not skip:
                        self._put(("bytes", part))
                tts = await task
                if skip:
                    continue
                self._send_json({"type": "audio_end"})
                if not tts.get("ok") and tts.get("reason") != "aborted":
                    # One failure silences the REST of the reply (retrying
                    # every sentence against a dead vendor buys latency, not
                    # audio) — the text already streamed, and the client may
                    # speak it on-device (the read-aloud degrade rule).
                    tts_dead = True
                    self._send_json({
                        "type": "tts_failed",
                        "reason": tts.get("reason", "synthesis_failed"),
                    })
        finally:
            producer_task.cancel()

    # ------------------------------------------------------- streaming relay
    async def _start_relay(self) -> None:
        """Try to bring up server-side VAD (P3); tell the client which side owns
        turn-taking either way. Batch mode is a WORKING mode, not an error."""
        db = SessionLocal()
        try:
            vocabulary = voice_service._vocabulary(db)
        except Exception:  # noqa: BLE001 — a vocab miss must not cost the call
            vocabulary = ""
        finally:
            db.close()
        transcriber = make_transcriber(vocabulary=vocabulary, on_event=self._on_relay_event)
        if transcriber is not None and await transcriber.connect():
            self.transcriber = transcriber
        else:
            self.transcriber = None
        self._send_json({"type": "vad", "server": self.transcriber is not None})

    def _drop_relay(self, event: dict | None = None) -> None:
        """The relay died mid-call: hand turn-taking back to the client and
        carry on in batch mode. Latency degrades; the call does not."""
        log.warning(
            "relay dropped (%s) — batch mode from here — session=%s",
            (event or {}).get("detail", (event or {}).get("type", "?")),
            self.session_id[:8],
        )
        transcriber, self.transcriber = self.transcriber, None
        if transcriber is not None:
            asyncio.get_running_loop().create_task(transcriber.close())
        if not self.ended:
            self._send_json({"type": "vad", "server": False})

    def _on_relay_event(self, event: dict) -> None:
        """Normalised relay events, delivered on this loop (see realtime_stt)."""
        if self.ended:
            return
        kind = event.get("type")
        if kind == "speech_started":
            # Speech in progress IS activity: without this stamp the silence
            # watchdog could hang up under a slow, long sentence.
            self.last_activity_mono = time.monotonic()
            self._mark("speech_started")
        elif kind == "speech_stopped":
            self._mark("speech_stopped", since="speech_started")
            # The vendor heard the turn end ~CALL_VAD_SILENCE_MS ago; the
            # transcript is already being finalised. Show thinking now so the
            # client stops streaming while it arrives.
            if self.state == "listening" and self.turn_task is None:
                self._set_state("thinking")
        elif kind == "transcript":
            self._mark("transcript", since="speech_stopped")
            self._begin_relay_turn(event.get("text") or "")
        elif kind in ("relay_error", "relay_closed"):
            self._drop_relay(event)

    def _begin_relay_turn(self, text: str) -> None:
        if self.turn_task is not None:
            if self.barged:
                # The interrupting utterance finished before the interrupted
                # turn drained — hold it; the pipeline's finally starts it.
                self.pending_transcript = text
            return
        pcm, self.pcm = bytes(self.pcm), bytearray()
        # No words AND no meaningful audio: a VAD misfire, not an utterance.
        if not text and len(pcm) < _MIN_UTTERANCE_BYTES:
            if self.state != "listening":
                self._set_state("listening")
            return
        self.last_activity_mono = time.monotonic()
        if self.state != "thinking":
            self._set_state("thinking")
        self.turn_task = asyncio.get_running_loop().create_task(
            self._run_pipeline(pcm, transcript=text)
        )

    # ------------------------------------------------------------ receive loop
    async def _on_end_turn(self) -> None:
        if self.turn_task is not None:
            if self.barged and self.transcriber is None:
                # Batch mode, mid-barge: the interrupting utterance ended
                # before the interrupted turn drained. Hold the signal; the
                # pipeline's finally processes the buffered audio.
                self.pending_end_turn = True
            return
        if self.state != "listening":
            return
        # Relay mode: the client's end_turn (its fallback detector, or the
        # utterance cap) becomes a manual commit — the vendor transcribes what
        # it holds and the transcript event drives the turn as usual.
        if self.transcriber is not None:
            if len(self.pcm) < _MIN_UTTERANCE_BYTES:
                return
            self._set_state("thinking")
            await self.transcriber.commit()
            return
        await self._start_batch_turn()

    async def _start_batch_turn(self) -> None:
        """Batch mode's turn entry — from a live end_turn, or from a pipeline's
        finally when a barged utterance was held pending."""
        pcm, self.pcm = bytes(self.pcm), bytearray()
        if len(pcm) < _MIN_UTTERANCE_BYTES:
            self._send_json({"type": "utterance_failed", "reason": "empty_recording"})
            if self.state != "listening":
                self._set_state("listening")
            return
        self.last_activity_mono = time.monotonic()
        self._set_state("thinking")
        self.turn_task = asyncio.get_running_loop().create_task(self._run_pipeline(pcm))

    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return  # garbage on the wire is ignored, never fatal
        kind = msg.get("type")
        if kind == "start":
            rate = msg.get("sample_rate")
            if isinstance(rate, int) and rate in _SAMPLE_RATES:
                self.sample_rate = rate
        elif kind == "end_turn":
            await self._on_end_turn()
        elif kind == "mute":
            self.muted = True
        elif kind == "unmute":
            self.muted = False
        elif kind == "barge_in":
            # P4: the customer started talking over Aisha. The CLIENT detected
            # it (it owns playback) and has already stopped the audio; here the
            # reply's remaining speech is abandoned and the mic stays open even
            # though the turn is still draining. THE TEXT IS NEVER TAKEN BACK —
            # the turn persists in full (the text_break rule, extended to
            # audio): interruption is playback-level, and the transcript keeps
            # what she wrote whether or not it was heard out loud.
            if self.turn_task is not None:
                self.tts_abort = True
                self.barged = True
            self.last_activity_mono = time.monotonic()
        elif kind == "hangup":
            self._end("hangup")

    async def _on_bytes(self, data: bytes) -> None:
        accepting = (self.state == "listening" and self.turn_task is None) or (
            # P4: after a barge-in the customer is ALREADY talking while the
            # interrupted turn drains — their speech must not fall on the
            # floor while the machinery catches up.
            self.barged and self.turn_task is not None
        )
        if not accepting or self.muted:
            return
        self.pcm.extend(data)
        if self.transcriber is not None:
            await self.transcriber.send_audio(
                resample_pcm16(data, self.sample_rate, VENDOR_SAMPLE_RATE)
            )
        # Caps force an end of turn rather than an error — the recording is
        # kept and processed, mirroring the voice-note auto-stop.
        max_bytes = min(VOICE_MAX_BYTES, CALL_UTTERANCE_MAX_SECONDS * self.sample_rate * 2)
        if len(self.pcm) >= max_bytes:
            await self._on_end_turn()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        sender = self.loop.create_task(self._sender())
        watchdog = self.loop.create_task(self._watchdog())

        db = SessionLocal()
        try:
            # actor=customer: a person pressed Call — the agent decided nothing.
            _audit_call(db, self.session_id, "call_started", actor=Actor.customer)
        finally:
            db.close()

        self._send_json({"type": "call_started", "max_minutes": CALL_MAX_MINUTES, "engine": "pipeline"})
        self._set_state("listening")
        # Server VAD (P3): brought up AFTER the call is answered, so its ~3s
        # worst-case connect happens while the customer is already hearing a
        # live call, and its failure quietly leaves the client's own detector
        # in charge (the `vad` event says which).
        await self._start_relay()

        try:
            while not self.ended:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await self._on_bytes(message["bytes"])
                elif message.get("text") is not None:
                    await self._on_text(message["text"])
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # receive() after the socket closed under us (e.g. _end mid-loop).
            pass
        finally:
            # Disconnect OR normal end: stop generation (AD-E), let the
            # in-flight pipeline finish its rollback, then tear down.
            self.cancel.set()
            if not self.ended:
                self.ended = True
                self.end_reason = self.end_reason or "hangup"
            if self.transcriber is not None:
                transcriber, self.transcriber = self.transcriber, None
                try:
                    await transcriber.close()
                except Exception:  # noqa: BLE001 — teardown must not raise
                    pass
            if self.turn_task is not None:
                try:
                    await self.turn_task
                except asyncio.CancelledError:
                    pass
            watchdog.cancel()
            # Give the sender a close sentinel in case _end never queued one
            # (disconnect path); a second close on a dead socket is swallowed.
            self._put(("close", None))
            try:
                await asyncio.wait_for(sender, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sender.cancel()

            db = SessionLocal()
            try:
                actor = Actor.customer if self.end_reason == "hangup" else Actor.system
                _audit_call(
                    db, self.session_id, "call_ended",
                    actor=actor, reason=self.end_reason or "disconnect",
                    duration_s=int(time.monotonic() - self.started_mono),
                )
            finally:
                db.close()
