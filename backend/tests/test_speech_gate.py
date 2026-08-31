"""The Silero speech gate — REAL inference, LOCAL model, zero network.

These tests run the actual ONNX model against local fixtures: the model file
ships in app/assets, the speech fixture is synthesized offline (macOS `say`,
checked in), and onnxruntime executes on CPU. Deterministic and vendor-free —
the one place in the suite where "offline" and "the real model" coexist.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


from app.config import CALL_VAD_SILENCE_MS
from app.services import speech_gate as sg

FIXTURE = Path(__file__).parent / "fixtures" / "speech_24k.wav"


def _speech() -> bytes:
    with wave.open(str(FIXTURE)) as w:
        assert w.getframerate() == 24_000 and w.getnchannels() == 1
        return w.readframes(w.getnframes())


def _feed_all(gate: sg.SpeechGate, pcm: bytes, frame: int = 1536) -> bytes:
    out = b""
    for i in range(0, len(pcm), frame):
        out += gate.feed(pcm[i : i + frame])
    return out


def test_speech_passes_noise_and_silence_do_not():
    speech = _speech()
    forwarded = _feed_all(sg.SpeechGate(), speech)
    # The whole utterance gets through (pre-roll means onset survives too).
    assert len(forwarded) > len(speech) * 0.8

    # A loud 440 Hz tone is sound, not speech — nothing is forwarded, which
    # is the whole point: what the vendor's VAD never hears it cannot commit
    # as a turn or bill as input.
    tone = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * t / 24000)))
        for t in range(24_000 * 2)
    )
    assert _feed_all(sg.SpeechGate(), tone) == b""

    assert _feed_all(sg.SpeechGate(), b"\x00\x00" * 24_000) == b""


def test_onset_rides_the_pre_roll():
    """Detection lags the first phoneme; the blocks just before the trigger
    are flushed WITH it, so the first syllable is never clipped."""
    gate = sg.SpeechGate()
    quiet = gate.feed(b"\x00\x00" * 24_000)  # a silent second first
    assert quiet == b""
    speech = _speech()
    first_forward = b""
    fed = 0
    for i in range(0, len(speech), 1536):
        first_forward = gate.feed(speech[i : i + 1536])
        fed += 1536
        if first_forward:
            break
    # The first non-empty return is bigger than what triggered it: the
    # pre-roll came along.
    assert len(first_forward) > sg._BLOCK_BYTES


def test_a_short_burst_never_opens_the_gate():
    """Min-speech-duration (2026-08-03): a door slam or cough can spike the
    probability for a block or two — only SUSTAINED speech opens the gate.
    Scripted probabilities, so the rule is tested, not the model."""
    gate = sg.SpeechGate(min_speech_ms=200)  # 6 blocks @32 ms
    probs = iter([0.9, 0.95, 0.1] + [0.9] * 20)  # burst, then real speech
    gate._prob = lambda block: next(probs)
    block = b"\x00\x00" * sg._BLOCK_SAMPLES
    # Two high blocks then a drop: the burst is dumped to pre-roll, nothing
    # forwarded.
    assert gate.feed(block * 3) == b""
    assert not gate.speaking
    # Six sustained blocks confirm speech; the flush carries the held onset
    # AND the burst blocks that fell into the pre-roll.
    out = gate.feed(block * 6)
    assert gate.speaking
    assert len(out) >= sg._BLOCK_BYTES * 6


def test_hangover_outlasts_the_vendors_turn_close():
    """The vendor's server_vad needs CALL_VAD_SILENCE_MS of trailing quiet to
    end the customer's turn — a gate that cut forwarding sooner would starve
    it of the very silence that closes a turn, and no turn would ever end."""
    gate = sg.SpeechGate()
    assert gate._hangover_blocks * sg._BLOCK_MS > CALL_VAD_SILENCE_MS


def test_gate_fails_open(monkeypatch):
    """A missing model must cost the GATE, never the call: None means the
    gateway forwards everything, which is the pre-gate behaviour."""
    monkeypatch.setattr(sg, "_MODEL_PATH", Path("/nonexistent/silero.onnx"))
    monkeypatch.setattr(sg, "_session_cache", {})
    assert sg.make_speech_gate() is None


def test_gateway_forwards_speech_and_drops_noise(db, monkeypatch):
    """Integration: with the gate on, _on_bytes forwards the speech fixture to
    the vendor session and drops a pure tone entirely."""
    import asyncio
    from unittest.mock import MagicMock

    from app.api import call_realtime as rt_module
    from tests.test_call_gateway import SESSION

    sent: list[bytes] = []

    class FakeSession:
        async def send_audio(self, pcm: bytes) -> None:
            sent.append(pcm)

    call = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    call.session = FakeSession()
    call.gate = sg.SpeechGate()

    async def scenario() -> None:
        tone = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * t / 24000)))
            for t in range(24_000)
        )
        for i in range(0, len(tone), 1536):
            await call._on_bytes(tone[i : i + 1536])
        assert sent == []  # the room's noise never reached the vendor
        speech = _speech()
        for i in range(0, len(speech), 1536):
            await call._on_bytes(speech[i : i + 1536])
        assert sum(len(b) for b in sent) > len(speech) * 0.8

    asyncio.run(scenario())
