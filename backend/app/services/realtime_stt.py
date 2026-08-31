"""Streaming STT relay — OpenAI Realtime transcription with server VAD (P3).

WHAT THIS BUYS OVER THE BATCH PATH: the vendor hears the audio AS IT IS SPOKEN
and its server-side VAD decides when the customer has finished — so the turn
ends ~CALL_VAD_SILENCE_MS after they stop talking and the transcript is already
in flight, instead of a whole recording being uploaded after a client-side
guess. The gap between "customer stops" and "Aisha starts" drops accordingly.

ONE SESSION PER CALL, RELAY ONLY. This module speaks the vendor's wire protocol
and NOTHING of ours: it forwards PCM in, and surfaces a small set of NORMALISED
events (speech_started / speech_stopped / transcript / relay_error /
relay_closed) through a callback. The gateway owns what those events MEAN —
turn boundaries, storage, the agent. Keeping the vendor dialect in one file is
also what makes the tests honest: the gateway is tested against the normalised
interface, and this module is the only thing a protocol change can break.

DEGRADE, NEVER DEAD-END: every failure here (no key, connect refused, socket
death mid-call) surfaces as an event or a False, and the gateway falls back to
the v0 batch path — client-side silence detection, whole-utterance upload. A
vendor outage costs LATENCY, never the call.

⚠️ THE VENDOR HANDSHAKE IS UNVERIFIED OFFLINE. The shapes below are from the
API reference as of 2026-07-27 (GA `session.update` with `session.type:
"transcription"`, audio.input.format `audio/pcm` @ 24 kHz, server_vad under
audio.input.turn_detection). The offline suite scripts this module's
NORMALISED events and proves the gateway; it cannot prove the vendor accepts
these frames. One live smoke test on a dev box is required before this path is
believed — the same discipline as the 2026-07-21 STT spike, and the same
reason: a hand-written protocol drifts, and the first symptom must not be on a
customer's phone. Until that test runs, treat `server_vad: false` in the
client as the honest default state, not a failure.

AUDIO FORMAT: the vendor accepts 24 kHz PCM16 mono little-endian, full stop.
The client captures at 24 kHz to match; `resample_pcm16` covers any session
that negotiated another rate rather than trusting every browser forever.

RULE 3: no `language` is ever sent — which language the customer chose is the
thing being learned. The vocabulary prompt IS sent, composed by the caller
from the live catalogue (the load-bearing lesson of the V0 spike).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable

from app.config import CALL_STT_MODEL, CALL_VAD_SILENCE_MS, OPENAI_API_KEY

log = logging.getLogger("app.services.realtime_stt")

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"

# The vendor's fixed input rate for audio/pcm. Not configurable — see docstring.
VENDOR_SAMPLE_RATE = 24_000

# How long connect() waits before giving up and letting the gateway fall back.
# 8s, raised from 3 on 2026-07-27: a live call measured the vendor's TLS+WS
# handshake blowing a 3s budget, and the cost of the miss is the WHOLE call
# running on the slow batch path — while the cost of waiting is nothing, since
# the call is already live and answering in batch mode as this connects.
_CONNECT_TIMEOUT_S = 8.0


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of PCM16 mono. Pure Python on purpose:
    audioop left the stdlib in 3.13, numpy is not a dependency, and speech-band
    audio at these sizes (~6k samples per 256 ms chunk) costs microseconds."""
    if src_rate == dst_rate or not pcm:
        return pcm
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    if n == 0:
        return b""
    out_n = max(1, int(n * dst_rate / src_rate))
    out = array.array("h", bytes(2 * out_n))
    ratio = n / out_n
    for i in range(out_n):
        pos = i * ratio
        i0 = int(pos)
        i1 = min(i0 + 1, n - 1)
        frac = pos - i0
        out[i] = int(samples[i0] * (1 - frac) + samples[i1] * frac)
    return out.tobytes()


class RealtimeTranscriber:
    """One vendor transcription session, normalised.

    Events delivered to `on_event` (always a dict with `type`):
        {"type": "speech_started"}
        {"type": "speech_stopped"}
        {"type": "transcript", "text": str}        # one utterance, final
        {"type": "relay_error", "detail": str}     # the session is unusable
        {"type": "relay_closed"}                   # vendor closed on us

    `on_event` is invoked ON THE EVENT LOOP the transcriber was connected on
    (the reader is a plain asyncio task), so the gateway may mutate its own
    state in the callback without thread gymnastics.
    """

    def __init__(
        self,
        *,
        vocabulary: str,
        on_event: Callable[[dict], None],
        silence_ms: int | None = None,
        model: str | None = None,
    ) -> None:
        self._vocabulary = vocabulary
        self._on_event = on_event
        self._silence_ms = silence_ms if silence_ms is not None else CALL_VAD_SILENCE_MS
        self._model = model or CALL_STT_MODEL
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def connect(self) -> bool:
        """Open the session and configure it. False = fall back to batch."""
        if not OPENAI_API_KEY:
            return False
        try:
            import websockets

            self._ws = await asyncio.wait_for(
                websockets.connect(
                    REALTIME_URL,
                    additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    max_size=None,
                ),
                timeout=_CONNECT_TIMEOUT_S,
            )
            await self._ws.send(json.dumps(self._session_update()))
        except Exception as exc:  # noqa: BLE001 — a relay failure is a fallback, not a 500
            # repr, not str: TimeoutError stringifies to "" and the first live
            # failure was diagnosed blind because of it.
            log.warning("realtime STT connect failed: %r", exc)
            await self.close()
            return False
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        return True

    def _session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": VENDOR_SAMPLE_RATE},
                        "transcription": {
                            "model": self._model,
                            # The catalogue biasing prompt — load-bearing, from
                            # the live rows, never hardcoded (V0 spike).
                            "prompt": self._vocabulary,
                            # NO "language": rule 3.
                            # NO "delay": tried 2026-07-27 and the vendor
                            # rejected the session with invalid_value, which
                            # silently cost the whole call its streaming path —
                            # the worst possible trade for a latency hint.
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": self._silence_ms,
                            # Defaults are fine for threshold; prefix padding
                            # keeps the utterance's first syllable.
                            "prefix_padding_ms": 300,
                        },
                        "noise_reduction": {"type": "far_field"},
                    }
                },
            },
        }

    async def send_audio(self, pcm: bytes) -> None:
        """Forward one chunk of 24 kHz PCM16 mono. Silently drops when the
        session is gone — the gateway learns that via relay_error/closed, and
        an audio frame must never raise inside the receive loop."""
        ws = self._ws
        if ws is None or self._closed or not pcm:
            return
        try:
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
        except Exception:  # noqa: BLE001
            self._fail("send_failed")

    async def commit(self) -> None:
        """Manual end-of-turn (the client's end_turn while the relay is up) —
        asks the vendor to transcribe what it holds without waiting for VAD."""
        ws = self._ws
        if ws is None or self._closed:
            return
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:  # noqa: BLE001
            self._fail("commit_failed")

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ intern
    def _emit(self, event: dict) -> None:
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 — a bad handler must not kill the reader
            log.exception("relay event handler failed")

    def _fail(self, detail: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit({"type": "relay_error", "detail": detail})

    async def _read_loop(self) -> None:
        """Vendor events -> normalised events. Tolerates BOTH event famillies
        (the beta `transcription_session.*` names and the GA `session.*` ones)
        because accepting one string more costs nothing and a vendor rename
        mid-engagement must not kill calls."""
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                kind = msg.get("type", "")
                if kind == "input_audio_buffer.speech_started":
                    self._emit({"type": "speech_started"})
                elif kind == "input_audio_buffer.speech_stopped":
                    self._emit({"type": "speech_stopped"})
                elif kind.endswith("input_audio_transcription.completed"):
                    self._emit({
                        "type": "transcript",
                        "text": (msg.get("transcript") or "").strip(),
                    })
                elif kind == "error":
                    # The FULL error first (2026-07-27): "invalid_value" alone
                    # cost a debugging round-trip; the message names the field.
                    log.warning("realtime STT vendor error: %s", msg.get("error"))
                    # One benign case: committing an empty buffer. The customer
                    # tapped end with nothing said — not a relay failure.
                    detail = str(
                        (msg.get("error") or {}).get("code")
                        or (msg.get("error") or {}).get("message")
                        or "vendor_error"
                    )
                    if "buffer" in detail and ("small" in detail or "empty" in detail):
                        continue
                    self._fail(detail)
                    return
                # Everything else (session.created/updated, deltas, committed,
                # item.* bookkeeping) is noise to us — deltas deliberately so:
                # the turn runs on the FINAL transcript, and streaming partials
                # into the thread would show words the vendor then revises.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime STT reader died: %s", exc)
            self._fail("reader_died")
            return
        if not self._closed:
            self._closed = True
            self._emit({"type": "relay_closed"})


def make_transcriber(
    *, vocabulary: str, on_event: Callable[[dict], None]
) -> RealtimeTranscriber | None:
    """The gateway's seam, and the tests'. Returns None when streaming STT can
    never work here (no key), so the caller falls straight back to batch —
    tests monkeypatch THIS to inject a scripted transcriber."""
    if not OPENAI_API_KEY:
        return None
    return RealtimeTranscriber(vocabulary=vocabulary, on_event=on_event)
