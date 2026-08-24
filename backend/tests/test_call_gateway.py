"""The WS voice-call gateway — offline, end to end.

NO LIVE VENDOR CALLS, per the suite's standing rule. STT and TTS are
monkeypatched clients (the test_voice/test_speech pattern), and the model is a
scripted `generate` injected through run_turn's seam — the same ScriptedModel
shape test_agent_graph uses, extended to EMIT ITS TEXT AS TOKEN EVENTS the way
the real streaming node does, because the gateway builds Aisha's spoken reply
from exactly those tokens.

What matters here beyond the happy path:

  - the utterance is persisted through the ORDINARY VoiceNote path (audio_id on
    the customer turn, WAV mime, playable later) — the call thread must replay
    like a voice-note thread, and rule 12 must be able to find the recordings;
  - refusals are SPOKEN in the protocol (call_disabled, voice_not_configured,
    session_closed) — an absent control must say why, the mic-button rule;
  - a TTS outage costs the AUDIO, never the call (tts_failed, still listening);
  - the watchdog hangs up on silence and on the cost ceiling, and says which;
  - hangup and close are audited with the right actor (rule 15).
"""

from __future__ import annotations

import functools
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import runner as runner_module
from app.agent.graph import _emit_token
from app.api import call as call_module
from app.db import get_db
from app.main import app
from app.models import (
    AuditEvent,
    AuditType,
    Actor,
    ConversationTurn,
    Speaker,
    VoiceNote,
)
from app.services import conversation as conversation_service
from app.services import speech as speech_service
from app.services import voice as voice_service

SESSION = "call-session"

# 0.5 s of unremarkable 16 kHz PCM16 — enough to clear _MIN_UTTERANCE_BYTES.
PCM_HALF_SECOND = b"\x10\x00" * 16_000


# --------------------------------------------------------------------------- #
# Scripted model that streams like the real one
# --------------------------------------------------------------------------- #
class SpeakingScriptedModel:
    """ScriptedModel + token emission.

    The plain ScriptedModel returns whole messages and never touches the stream
    writer, which is fine for graph tests — but the gateway assembles the reply
    it will SPEAK from token events, so a silent model would synthesise nothing
    and the TTS leg would go untested. Emitting each text block as one token is
    exactly what _default_generate does, just coarser.
    """

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.i = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        msg = self.steps[self.i]
        self.i += 1
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                _emit_token(block["text"])
        return msg


def _text(s: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": s}]}


def _tool_use(tid: str, tool: str, /, **inp) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}],
    }


# --------------------------------------------------------------------------- #
# Fakes — the test_voice / test_speech pattern
# --------------------------------------------------------------------------- #
class _FakeSTT:
    def __init__(self, text: str | None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.calls: list[dict] = []

        outer = self

        class _Transcriptions:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer._raises:
                    raise outer._raises
                return type("R", (), {"text": outer._text})()

        class _Audio:
            transcriptions = _Transcriptions()

        self.audio = _Audio()


class _FakeTTS:
    """Fakes BOTH synthesis shapes: batch `create` (read-aloud style) and the
    call path's `with_streaming_response.create` context manager, whose
    iter_bytes yields the canned data in vendor-sized parts."""

    def __init__(self, data: bytes | None, raises: Exception | None = None) -> None:
        self._data = data
        self._raises = raises
        self.calls: list[dict] = []

        outer = self

        class _StreamCtx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_bytes(self, chunk_size: int = 8192):
                data = outer._data or b""
                for i in range(0, len(data), chunk_size):
                    yield data[i : i + chunk_size]

        class _StreamingSpeech:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer._raises:
                    raise outer._raises
                return _StreamCtx()

        class _Speech:
            with_streaming_response = _StreamingSpeech()

            def create(self, **kw):
                outer.calls.append(kw)
                if outer._raises:
                    raise outer._raises
                return type("R", (), {"content": outer._data})()

        class _Audio:
            speech = _Speech()

        self.audio = _Audio()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def call_env(db, tmp_path, monkeypatch):
    """Everything a live call needs, hermetically.

    One factory of short-lived Sessions over the test's own in-memory engine
    replaces SessionLocal EVERYWHERE the call path opens its own session (the
    gateway, the runner) — the same engine the `db` fixture seeded, so the
    vocabulary prompt reads the real catalogue.
    """
    engine = db.get_bind()
    factory = lambda: Session(engine)  # noqa: E731

    monkeypatch.setattr(call_module, "SessionLocal", factory)
    monkeypatch.setattr(runner_module, "SessionLocal", factory)
    monkeypatch.setattr(
        runner_module, "AGENT_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db")
    )

    monkeypatch.setattr(call_module, "CALL_ENABLED", True)
    monkeypatch.setattr(call_module, "OPENAI_API_KEY", "test-key")

    stt = _FakeSTT("I'd like to test drive the Jetour T2")
    monkeypatch.setattr(voice_service, "_client", lambda: stt)
    monkeypatch.setattr(voice_service, "OPENAI_API_KEY", "test-key")

    tts = _FakeTTS(b"\xffMP3-BYTES" * 100)
    monkeypatch.setattr(speech_service, "_client", lambda: tts)
    monkeypatch.setattr(speech_service, "OPENAI_API_KEY", "test-key")

    # This file tests the BATCH mode; the factory is pinned to None so no call
    # ever tries the streaming relay (conftest also blanks the relay's key —
    # belt and braces). The relay's own tests inject a scripted transcriber
    # through this same seam — see test_call_relay.py.
    monkeypatch.setattr(call_module, "make_transcriber", lambda **kw: None)

    return {"stt": stt, "tts": tts, "monkeypatch": monkeypatch}


@pytest.fixture()
def client(db) -> TestClient:
    # NOT a context manager — the lifespan (prewarm, dispatcher) must not run.
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _use_script(monkeypatch, steps: list[dict]) -> None:
    """Route the gateway's run_turn through a scripted generate."""
    monkeypatch.setattr(
        call_module,
        "run_turn",
        functools.partial(runner_module.run_turn, generate=SpeakingScriptedModel(steps)),
    )


def _drain_until(ws, wanted_type: str, *, limit: int = 200) -> tuple[dict, list]:
    """Receive JSON frames until `wanted_type` arrives; returns (frame, seen)."""
    seen: list = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(frame)
        if frame.get("type") == wanted_type:
            return frame, seen
    raise AssertionError(f"never saw {wanted_type!r}; saw {[f.get('type') for f in seen]}")


# --------------------------------------------------------------------------- #
# Refusals — spoken, then closed
# --------------------------------------------------------------------------- #
def test_refuses_when_the_flag_is_off(db, client, monkeypatch):
    monkeypatch.setattr(call_module, "CALL_ENABLED", False)
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json() == {"type": "call_ended", "reason": "call_disabled"}


def test_refuses_without_the_vendor_key(db, client, call_env):
    """Unlike voice notes there is no degraded call: it can neither hear nor
    speak, so the honest move is a stated refusal, not a dead line."""
    call_env["monkeypatch"].setattr(call_module, "OPENAI_API_KEY", "")
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json() == {"type": "call_ended", "reason": "voice_not_configured"}


def test_refuses_a_closed_session(db, client, call_env):
    """Rule 19: a closed session is ended. A new call needs a fresh session."""
    conversation_service.close(db, session_id=SESSION, actor=Actor.customer)
    db.commit()
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json() == {"type": "call_ended", "reason": "session_closed"}


# --------------------------------------------------------------------------- #
# The v0 turn loop, end to end
# --------------------------------------------------------------------------- #
def test_full_turn_loop_stt_agent_tts(db, client, call_env):
    _use_script(call_env["monkeypatch"], [_text("Happy to help with the T2.")])

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json()["type"] == "call_started"
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        # P3: who owns turn-taking. Batch mode here, so the client's detector.
        assert ws.receive_json() == {"type": "vad", "server": False}

        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})

        assert ws.receive_json() == {"type": "state", "value": "thinking"}

        utterance, _ = _drain_until(ws, "utterance")
        assert utterance["failed"] is False
        assert utterance["text"] == "I'd like to test drive the Jetour T2"
        assert utterance["lang"] == "en"
        assert utterance["audio_id"]

        done, seen = _drain_until(ws, "done")
        token_text = "".join(f.get("text", "") for f in seen if f.get("type") == "token")
        assert token_text == "Happy to help with the T2."
        assert done["turn_id"]

        speaking, _ = _drain_until(ws, "state")
        assert speaking == {"type": "state", "value": "speaking"}
        assert ws.receive_json()["type"] == "audio_start"
        audio = b""
        while True:
            frame = ws.receive()
            if frame.get("bytes") is not None:
                audio += frame["bytes"]
                continue
            import json as _json

            assert _json.loads(frame["text"]) == {"type": "audio_end"}
            break
        assert audio == b"\xffMP3-BYTES" * 100
        assert ws.receive_json() == {"type": "state", "value": "listening"}

        ws.send_json({"type": "hangup"})
        assert ws.receive_json() == {"type": "call_ended", "reason": "hangup"}

    # Persistence: the utterance went through the ORDINARY VoiceNote path…
    db.expire_all()
    note = db.scalar(select(VoiceNote).where(VoiceNote.audio_id == utterance["audio_id"]))
    assert note is not None
    assert note.mime == "audio/wav"
    assert note.transcript == "I'd like to test drive the Jetour T2"

    # …and the thread reads exactly like a voice-note thread (rule 18/replay).
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.session_id == SESSION)
        .order_by(ConversationTurn.id)
    ).all()
    assert [t.speaker for t in turns] == [Speaker.customer, Speaker.agent]
    assert turns[0].audio_id == utterance["audio_id"]
    assert turns[0].text == "I'd like to test drive the Jetour T2"
    assert turns[1].text == "Happy to help with the T2."
    assert turns[1].id == done["turn_id"]

    # The vendor was briefed from the live catalogue, not a typed list.
    stt_call = call_env["stt"].calls[0]
    assert "Jetour" in stt_call["prompt"]
    # And the reply was normalised for speech before synthesis.
    assert call_env["tts"].calls, "TTS was never reached"


def test_a_long_call_answer_is_not_judged_by_the_voice_note_limit(db, client, call_env):
    """The 30–60 s gap (2026-08-03): _on_bytes lets a call utterance run to
    CALL_UTTERANCE_MAX_SECONDS (60), but the batch path stored it through the
    voice-note rule (VOICE_MAX_SECONDS, 30) — so a 45-second answer was
    silently discarded as recording_too_long: nothing stored, a bare
    utterance_failed, and Aisha never heard a word the customer said. The
    gateway now passes the CALL's own limit; the voice-note route keeps its
    own (test_voice pins that side)."""
    _use_script(call_env["monkeypatch"], [_text("Noted, thank you.")])

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json()["type"] == "call_started"
        _drain_until(ws, "vad")
        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.send_bytes(b"\x01\x02" * (45 * 16_000))  # 45 s @ 16 kHz — over the NOTE limit
        ws.send_json({"type": "end_turn"})

        utterance, _ = _drain_until(ws, "utterance")
        assert utterance["failed"] is False
        assert utterance["duration_ms"] == 45_000
        assert utterance["audio_id"]
        _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    db.expire_all()
    note = db.scalar(select(VoiceNote).where(VoiceNote.audio_id == utterance["audio_id"]))
    assert note is not None and note.duration_ms == 45_000


def test_a_breath_is_not_an_utterance(db, client, call_env):
    """Below the minimum, nothing is stored and no vendor is called — the
    client is told to go back to listening."""
    _use_script(call_env["monkeypatch"], [_text("never reached")])
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "state")
        ws.send_bytes(b"\x00\x00" * 100)
        ws.send_json({"type": "end_turn"})
        frame, _ = _drain_until(ws, "utterance_failed")
        assert frame["reason"] == "empty_recording"
        ws.send_json({"type": "hangup"})

    assert call_env["stt"].calls == []
    assert db.scalars(select(VoiceNote)).all() == []


def test_tts_outage_costs_the_audio_never_the_call(db, client, call_env):
    """The read-aloud degrade rule, applied to calls: the text streamed, so a
    vendor failure downgrades to a silent (client-spoken) reply — the call goes
    on listening."""
    call_env["monkeypatch"].setattr(
        speech_service, "_client", lambda: _FakeTTS(None, raises=RuntimeError("boom"))
    )
    _use_script(call_env["monkeypatch"], [_text("Still here, in text.")])

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "state")
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})
        frame, _ = _drain_until(ws, "tts_failed")
        assert frame["reason"] == "synthesis_failed"
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        ws.send_json({"type": "hangup"})
        assert ws.receive_json() == {"type": "call_ended", "reason": "hangup"}


def test_failed_transcription_still_reaches_the_agent(db, client, call_env):
    """The voice-note recovery, on the call path: stored but unheard means an
    empty customer turn with the player, and Aisha asks them to repeat — the
    model never invents what might have been said (rule 2)."""
    call_env["monkeypatch"].setattr(
        voice_service, "_client", lambda: _FakeSTT(None, raises=RuntimeError("vendor down"))
    )
    _use_script(call_env["monkeypatch"], [_text("Sorry — could you say that again?")])

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "state")
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})
        utterance, _ = _drain_until(ws, "utterance")
        assert utterance["failed"] is True
        assert utterance["text"] == ""
        assert utterance["audio_id"]  # stored: the player must still work
        done, _ = _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    db.expire_all()
    customer_turn = db.scalar(
        select(ConversationTurn).where(
            ConversationTurn.session_id == SESSION,
            ConversationTurn.speaker == Speaker.customer,
        )
    )
    assert customer_turn.text == ""  # the honest record — no invented words
    assert customer_turn.audio_id == utterance["audio_id"]


# --------------------------------------------------------------------------- #
# Watchdog + lifecycle
# --------------------------------------------------------------------------- #
def test_silence_hangs_up_and_says_so(db, client, call_env):
    call_env["monkeypatch"].setattr(call_module, "CALL_SILENCE_HANGUP_SECONDS", 0)
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        frame, _ = _drain_until(ws, "call_ended")
        assert frame["reason"] == "silence"


def test_the_cost_ceiling_hangs_up(db, client, call_env):
    call_env["monkeypatch"].setattr(call_module, "CALL_MAX_MINUTES", 0)
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        frame, _ = _drain_until(ws, "call_ended")
        assert frame["reason"] == "max_minutes"


def test_call_lifecycle_is_audited_with_the_right_actor(db, client, call_env):
    """rule 15: a person pressed Call and a person hung up — the trail must not
    read as though the agent or the scheduler decided either."""
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "state")
        ws.send_json({"type": "hangup"})
        _drain_until(ws, "call_ended")

    db.expire_all()
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.session == SESSION, AuditEvent.type == AuditType.voice)
        .order_by(AuditEvent.id)
    ).all()
    actions = [(r.payload.get("action"), r.actor) for r in rows]
    assert ("call_started", Actor.customer) in actions
    assert ("call_ended", Actor.customer) in actions
