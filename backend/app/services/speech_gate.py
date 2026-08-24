"""Silero VAD gate — only human speech is forwarded to the vendor.

WHY THIS EXISTS (2026-07-30). The realtime engine used to stream the raw
microphone to OpenAI nonstop and let their volume-based server VAD decide what
was speech. Echo residue, breaths and room noise kept tripping it: false turns
committed near-silence (the hallucinated-transcript bug), false interruptions
cancelled live replies (48 across two calls), and every false turn billed a
throwaway response that fed the rate-limit saturation. The fix at the source:
a real speech detector — Silero VAD, the neural model the production voice
stacks (LiveKit, Pipecat) standardised on — sits in front of the vendor and
forwards ONLY speech. What the vendor's VAD never hears, it can never misfire
on.

SERVER-SIDE ON PURPOSE, not in the browser: here it is transport-agnostic (the
same gate serves the WS path, the WebRTC path, and the future Twilio leg
unchanged), and here it is offline-testable — the model is a LOCAL file
(app/assets/silero_vad.onnx, vendored from the official snakers4/silero-vad
release), inference is onnxruntime on CPU (~1 ms per 32 ms block), and no
network exists anywhere in this module.

SHAPE OF THE GATE. Audio arrives as PCM16 mono at the vendor rate (24 kHz) and
is processed in 32 ms blocks (768 samples), each decimated to the 512 samples
@16 kHz Silero v5 expects (with its 64-sample rolling context). A block is
forwarded when:
  - speech is live (probability held above `start_prob` for `min_speech_ms` —
    a burst that spikes for a block is noise, a word holds), including a
    PRE-ROLL of the blocks just before onset and the held onset blocks
    themselves — detection lags the first phoneme, and a clipped first
    syllable turns "book" into "ook";
  - or speech ended less than `hangover_ms` ago. The hangover is DELIBERATELY
    LONGER than the vendor's server_vad silence_duration_ms (900): the vendor
    needs to hear the trailing quiet to close the customer's turn, so the gate
    must not starve it of the very silence that ends a turn.
Everything else — the idle room — is dropped, never sent, never billed.

FAIL OPEN. A missing model or a dead onnxruntime must cost the GATE, never the
CALL: make_speech_gate() returns None and the gateway falls back to forwarding
everything, which is exactly yesterday's behaviour.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

log = logging.getLogger("app.services.speech_gate")

_MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "silero_vad.onnx"

IN_RATE = 24_000
_VAD_RATE = 16_000
_VAD_CHUNK = 512  # Silero v5's fixed 16 kHz hop
_BLOCK_SAMPLES = _VAD_CHUNK * IN_RATE // _VAD_RATE  # 768 @24k = one 32 ms block
_BLOCK_BYTES = _BLOCK_SAMPLES * 2
_BLOCK_MS = 1000 * _VAD_CHUNK // _VAD_RATE  # 32
_CONTEXT = 64  # v5 carries the tail of the previous chunk as context

_session_cache: dict[str, object] = {}


def _load_session():
    """One InferenceSession per process — the model is stateless; per-call
    state lives in the SpeechGate instance."""
    if "session" not in _session_cache:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _session_cache["session"] = ort.InferenceSession(
            str(_MODEL_PATH), opts, providers=["CPUExecutionProvider"]
        )
    return _session_cache["session"]


class SpeechGate:
    """Feed PCM16 mono @24 kHz in, get back only the bytes worth sending."""

    def __init__(
        self,
        *,
        start_prob: float = 0.7,
        end_prob: float = 0.5,
        pre_roll_ms: int = 256,
        hangover_ms: int = 1200,
        min_speech_ms: int = 200,
    ) -> None:
        import numpy as np

        self._np = np
        self._ort = _load_session()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._sr = np.array(_VAD_RATE, dtype=np.int64)
        self._start_prob = start_prob
        self._end_prob = end_prob
        self._pre_roll: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // _BLOCK_MS))
        self._hangover_blocks = max(1, hangover_ms // _BLOCK_MS)
        self._quiet_blocks = 0
        # Min-speech-duration (2026-08-03): the gate opens only after this
        # many CONSECUTIVE speech-probability blocks — a door slam or a cough
        # spikes for a block or two, a spoken word holds. Candidate blocks
        # are held (not forwarded) and flushed in full on confirmation, so
        # the wait costs no audio, only certainty.
        self._min_speech_blocks = max(1, min_speech_ms // _BLOCK_MS)
        self._onset: list[bytes] = []
        self.speaking = False
        self._buf = bytearray()

    # ------------------------------------------------------------------ vad
    def _prob(self, block: bytes) -> float:
        np = self._np
        pcm = np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
        # 24k -> 16k by linear interpolation: plenty for a VAD, and it keeps
        # this module dependency-light (no scipy/soxr).
        idx = np.linspace(0, len(pcm) - 1, _VAD_CHUNK, dtype=np.float32)
        chunk = np.interp(idx, np.arange(len(pcm), dtype=np.float32), pcm).astype(
            np.float32
        )
        x = np.concatenate([self._context, chunk])[np.newaxis, :]
        out, self._state = self._ort.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = chunk[-_CONTEXT:]
        return float(out[0][0])

    # ----------------------------------------------------------------- gate
    def feed(self, pcm: bytes) -> bytes:
        """Returns the bytes to forward NOW ('' while the room is idle)."""
        self._buf.extend(pcm)
        out = bytearray()
        while len(self._buf) >= _BLOCK_BYTES:
            block = bytes(self._buf[:_BLOCK_BYTES])
            del self._buf[:_BLOCK_BYTES]
            prob = self._prob(block)
            if self.speaking:
                out.extend(block)
                if prob < self._end_prob:
                    self._quiet_blocks += 1
                    if self._quiet_blocks >= self._hangover_blocks:
                        self.speaking = False
                        self._pre_roll.clear()
                else:
                    self._quiet_blocks = 0
            elif prob >= self._start_prob:
                self._onset.append(block)
                if len(self._onset) >= self._min_speech_blocks:
                    self.speaking = True
                    self._quiet_blocks = 0
                    # The onset the detector missed rides along with the
                    # blocks that confirmed it.
                    for held in self._pre_roll:
                        out.extend(held)
                    self._pre_roll.clear()
                    for held in self._onset:
                        out.extend(held)
                    self._onset.clear()
            else:
                # A candidate onset that fell back below the bar was a burst,
                # not speech — its blocks join the pre-roll so a real start
                # right after keeps its context.
                for held in self._onset:
                    self._pre_roll.append(held)
                self._onset.clear()
                self._pre_roll.append(block)
        return bytes(out)


def make_speech_gate() -> SpeechGate | None:
    """The gateway's seam. None = gate unavailable — forward everything, which
    is the pre-gate behaviour, and say so once in the log. Sensitivity comes
    from config so a noisy room is tuned in .env, never in code."""
    from app.config import (
        CALL_GATE_END_PROB,
        CALL_GATE_MIN_SPEECH_MS,
        CALL_GATE_START_PROB,
    )

    try:
        return SpeechGate(
            start_prob=CALL_GATE_START_PROB,
            end_prob=CALL_GATE_END_PROB,
            min_speech_ms=CALL_GATE_MIN_SPEECH_MS,
        )
    except Exception as exc:  # noqa: BLE001 — the gate must never cost the call
        log.warning("speech gate unavailable (%s) — forwarding ungated", exc)
        return None
