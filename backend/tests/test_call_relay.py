"""The streaming STT relay (P3) — scripted VAD/transcript events, offline.

NO LIVE VENDOR CALLS, and doubly guarded here: conftest blanks the relay
module's OPENAI_API_KEY (so the real factory can never dial out), and every
test injects a SCRIPTED transcriber through the same `make_transcriber` seam
the gateway uses in production. What is under test is the GATEWAY'S handling
of the normalised event stream — the vendor dialect itself lives behind the
seam and is explicitly flagged in realtime_stt.py as needing one live smoke
test (it cannot be proven from here).

The properties that matter:

  - server VAD runs the whole turn with NO batch vendor call and NO client
    end_turn — the relay's transcript is stored verbatim on the VoiceNote;
  - the client's end_turn becomes a manual COMMIT while the relay is up, so
    the fallback detector still works rather than double-transcribing;
  - a relay death mid-call hands turn-taking BACK to the client (`vad`
    server:false) and the batch path carries the rest of the call — latency
    degrades, the call does not.
"""

from __future__ import annotations

import functools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import runner as runner_module
from app.api import call as call_module
from app.db import get_db
from app.main import app
from app.models import ConversationTurn, Speaker, VoiceNote
from app.services import speech as speech_service
from app.services import voice as voice_service
from app.services.realtime_stt import resample_pcm16

from tests.test_call_gateway import (
    PCM_HALF_SECOND,
    SESSION,
    SpeakingScriptedModel,
    _FakeSTT,
    _FakeTTS,
    _drain_until,
    _text,
)


class FakeTranscriber:
    """A scripted relay: emits normalised events by rule, never a socket.

    `auto_after_bytes`: emit speech_started on the first audio, then
    speech_stopped + the transcript once that much audio has arrived — the
    server-VAD happy path. None = never auto-fire (the commit tests).
    `die_on_audio`: emit relay_error on the first audio — the mid-call death.
    """

    def __init__(
        self,
        on_event,
        *,
        transcript: str = "I'd like to test drive the Jetour T2",
        auto_after_bytes: int | None = 8_000,
        die_on_audio: bool = False,
    ) -> None:
        self.on_event = on_event
        self.transcript = transcript
        self.auto_after_bytes = auto_after_bytes
        self.die_on_audio = die_on_audio
        self.audio_bytes = 0
        self.started = False
        self.fired = False
        self.committed = False
        self.closed = False

    async def connect(self) -> bool:
        return True

    async def send_audio(self, pcm: bytes) -> None:
        if self.die_on_audio:
            self.die_on_audio = False  # once
            self.on_event({"type": "relay_error", "detail": "scripted death"})
            return
        self.audio_bytes += len(pcm)
        if not self.started and self.audio_bytes > 0:
            self.started = True
            self.on_event({"type": "speech_started"})
        if (
            self.auto_after_bytes is not None
            and not self.fired
            and self.audio_bytes >= self.auto_after_bytes
        ):
            self.fired = True
            self.on_event({"type": "speech_stopped"})
            self.on_event({"type": "transcript", "text": self.transcript})

    async def commit(self) -> None:
        self.committed = True
        if not self.fired:
            self.fired = True
            self.on_event({"type": "transcript", "text": self.transcript})

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def relay_env(db, tmp_path, monkeypatch):
    """The gateway wired for a call, with the relay seam left to each test."""
    engine = db.get_bind()
    factory = lambda: Session(engine)  # noqa: E731

    monkeypatch.setattr(call_module, "SessionLocal", factory)
    monkeypatch.setattr(runner_module, "SessionLocal", factory)
    monkeypatch.setattr(
        runner_module, "AGENT_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db")
    )
    monkeypatch.setattr(call_module, "CALL_ENABLED", True)
    monkeypatch.setattr(call_module, "OPENAI_API_KEY", "test-key")

    # The BATCH vendor stub. In relay tests its call log doubles as the proof
    # that the relay path never paid for a second transcription.
    stt = _FakeSTT("batch transcript — must not be used by the relay path")
    monkeypatch.setattr(voice_service, "_client", lambda: stt)
    monkeypatch.setattr(voice_service, "OPENAI_API_KEY", "test-key")

    tts = _FakeTTS(b"\xffMP3" * 50)
    monkeypatch.setattr(speech_service, "_client", lambda: tts)
    monkeypatch.setattr(speech_service, "OPENAI_API_KEY", "test-key")

    monkeypatch.setattr(
        call_module,
        "run_turn",
        functools.partial(
            runner_module.run_turn,
            generate=SpeakingScriptedModel([_text("Lovely — the T2 it is.")]),
        ),
    )

    holder: dict = {}

    def install(**kwargs) -> None:
        def factory_fn(*, vocabulary, on_event):
            holder["vocabulary"] = vocabulary
            holder["transcriber"] = FakeTranscriber(on_event, **kwargs)
            return holder["transcriber"]

        monkeypatch.setattr(call_module, "make_transcriber", factory_fn)

    holder["install"] = install
    holder["stt"] = stt
    holder["monkeypatch"] = monkeypatch
    return holder


@pytest.fixture()
def client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_server_vad_runs_the_turn_with_no_batch_call(db, client, relay_env):
    relay_env["install"](auto_after_bytes=8_000)

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json()["type"] == "call_started"
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        assert ws.receive_json() == {"type": "vad", "server": True}

        ws.send_json({"type": "start", "sample_rate": 16000})
        # Speak — and never send end_turn: the VENDOR's VAD ends the turn.
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_bytes(PCM_HALF_SECOND)

        utterance, seen = _drain_until(ws, "utterance")
        assert utterance["text"] == "I'd like to test drive the Jetour T2"
        assert utterance["failed"] is False
        # speech_stopped showed thinking BEFORE the transcript landed.
        assert {"type": "state", "value": "thinking"} in seen

        done, _ = _drain_until(ws, "done")
        assert done["turn_id"]
        ws.send_json({"type": "hangup"})

    # The whole point: the relay's words, no second vendor round-trip.
    assert relay_env["stt"].calls == []
    db.expire_all()
    note = db.scalar(select(VoiceNote).where(VoiceNote.audio_id == utterance["audio_id"]))
    assert note is not None
    assert note.transcript == "I'd like to test drive the Jetour T2"
    assert note.mime == "audio/wav"
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.session_id == SESSION)
        .order_by(ConversationTurn.id)
    ).all()
    assert [t.speaker for t in turns] == [Speaker.customer, Speaker.agent]
    assert turns[0].text == "I'd like to test drive the Jetour T2"

    # The vocabulary reached the relay from the live catalogue.
    assert "Jetour" in relay_env["vocabulary"]
    # The vendor stream got the FIRST frame resampled (16k -> 24k, 1.5x). The
    # second frame arrived after the scripted VAD had already ended the turn,
    # and was correctly DROPPED — no barge-in until P4, and audio Aisha will
    # not hear must not be silently buffered.
    assert relay_env["transcriber"].audio_bytes == len(
        resample_pcm16(PCM_HALF_SECOND, 16_000, 24_000)
    )


def test_client_end_turn_becomes_a_commit_while_the_relay_is_up(db, client, relay_env):
    """The fallback detector still works in relay mode — as a manual commit,
    never a second transcription of audio the vendor already holds."""
    relay_env["install"](auto_after_bytes=None)  # VAD never fires on its own

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})

        utterance, _ = _drain_until(ws, "utterance")
        assert utterance["text"] == "I'd like to test drive the Jetour T2"
        _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    assert relay_env["transcriber"].committed is True
    assert relay_env["stt"].calls == []


def test_relay_death_hands_turn_taking_back_to_the_client(db, client, relay_env):
    """Mid-call vendor failure: the client is told (`vad` server:false) and the
    BATCH path finishes the call — latency degrades, the call does not."""
    relay_env["install"](die_on_audio=True)

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        first_vad, _ = _drain_until(ws, "vad")
        assert first_vad == {"type": "vad", "server": True}

        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.send_bytes(PCM_HALF_SECOND)  # scripted death on this frame
        second_vad, _ = _drain_until(ws, "vad")
        assert second_vad == {"type": "vad", "server": False}

        # The customer keeps talking; the CLIENT's detector now ends the turn.
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})
        utterance, _ = _drain_until(ws, "utterance")
        # Batch transcript this time — the batch stub's text, one vendor call.
        assert utterance["text"] == "batch transcript — must not be used by the relay path"
        _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    assert len(relay_env["stt"].calls) == 1
    assert relay_env["transcriber"].closed is True


def test_the_relay_is_closed_on_hangup(db, client, relay_env):
    relay_env["install"](auto_after_bytes=None)
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        ws.send_json({"type": "hangup"})
        _drain_until(ws, "call_ended")
    assert relay_env["transcriber"].closed is True
