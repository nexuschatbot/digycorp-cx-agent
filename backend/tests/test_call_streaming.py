"""P4 — sentence-streamed TTS and barge-in. Offline, like every call test.

The chunker is pinned as a unit (it is pure), then the gateway end-to-end:

  - a two-sentence reply ships as TWO audio segments, the first synthesised
    while the second sentence is still being generated (proven with a gate:
    the scripted model BLOCKS between sentences, and the first segment
    arrives while it is blocked);
  - barge-in mid-reply: remaining speech is abandoned — ONE synthesis, not
    two — while the STORED turn keeps the full text (the text_break rule
    extended to audio: never take back words, spoken or not);
  - the reply after a barge-in still runs (the customer's interrupting
    utterance is not lost).
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
from app.models import ConversationTurn, Speaker
from app.services import speech as speech_service
from app.services import voice as voice_service
from app.services.speech import SentenceChunker, for_speech

from tests.test_call_gateway import (
    PCM_HALF_SECOND,
    SESSION,
    _FakeSTT,
    _FakeTTS,
    _drain_until,
)


# --------------------------------------------------------------------------- #
# The chunker, as a unit
# --------------------------------------------------------------------------- #
def _collect():
    out: list[str] = []
    return out, SentenceChunker(out.append)  # the service's own default


def test_chunker_emits_each_sentence_at_its_boundary():
    out, c = _collect()
    c.feed("Of course, that sounds wonderful. ")
    assert out == ["Of course, that sounds wonderful."]
    c.feed("Give me just a moment to check the diary. ")
    assert out[-1] == "Give me just a moment to check the diary."


def test_chunker_streams_across_token_fragments():
    out, c = _collect()
    for token in ["Of course, that ", "sounds wonderful. Give me just", " a moment. "]:
        c.feed(token)
    assert out == ["Of course, that sounds wonderful.", "Give me just a moment."]


def test_chunker_holds_short_fragments_for_the_next_sentence():
    """The abbreviation trap: 'Perfect, Mr.' must never be spoken alone."""
    out, c = _collect()
    c.feed("Perfect, Mr. Sharma — confirmed for Saturday evening. ")
    assert out == ["Perfect, Mr. Sharma — confirmed for Saturday evening."]


def test_chunker_flush_emits_the_unterminated_tail():
    out, c = _collect()
    c.feed("Anything else I can arrange for you")
    assert out == []
    c.flush()
    assert out == ["Anything else I can arrange for you"]
    c.flush()
    assert out == ["Anything else I can arrange for you"]  # idempotent


def test_first_clause_mode_speaks_the_opening_early():
    """Fast start: with tokens still streaming and no sentence boundary in
    sight, the OPENING clause leaves at its comma — and only the opening;
    later chunks wait for whole sentences."""
    out: list[str] = []
    c = SentenceChunker(out.append, first_clause=True)
    c.feed("Perfect, Mr. Sharma, let me ")
    assert out == ["Perfect, Mr. Sharma,"]
    c.feed("check the diary for Saturday, and I will ")
    assert out == ["Perfect, Mr. Sharma,"]  # second comma does NOT split
    c.feed("hold a slot. ")
    assert out[-1] == "let me check the diary for Saturday, and I will hold a slot."


def test_first_clause_mode_caps_an_unpunctuated_run():
    out: list[str] = []
    c = SentenceChunker(out.append, first_clause=True)
    c.feed("So the thing about the plug in hybrid is that it really does give you the best of both ")
    assert len(out) == 1 and out[0].startswith("So the thing about")


def test_first_clause_mode_off_by_default():
    out, c = _collect()
    c.feed("Perfect, Mr. Sharma, let me ")
    assert out == []  # no clause split unless asked for


def test_chunker_question_marks_break_too():
    out, c = _collect()
    c.feed("Shall I book the seven o'clock slot? I can hold it for you. ")
    assert out == [
        "Shall I book the seven o'clock slot?",
        "I can hold it for you.",
    ]


# --------------------------------------------------------------------------- #
# The gateway, streaming
# --------------------------------------------------------------------------- #
SENTENCE_1 = "Of course, that sounds wonderful. "
SENTENCE_2 = "Give me just a moment to check the diary."


FOLLOW_UP = "Noted, sir."


class GatedTwoSentenceModel:
    """FIRST turn: emits sentence 1, then BLOCKS on `gate` before emitting
    sentence 2 — freezing the generation mid-reply so a test can act while
    Aisha is provably still writing. Any LATER turn: a short, distinct reply,
    so a test that runs a second turn can tell the two apart in the TTS log."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.turns = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        self.turns += 1
        if self.turns > 1:
            _emit_token(FOLLOW_UP)
            return {"role": "assistant", "content": [{"type": "text", "text": FOLLOW_UP}]}
        _emit_token(SENTENCE_1)
        self.gate.wait(timeout=10)
        _emit_token(SENTENCE_2)
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": SENTENCE_1 + SENTENCE_2}],
        }


@pytest.fixture()
def stream_env(db, tmp_path, monkeypatch):
    engine = db.get_bind()
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(call_module, "SessionLocal", factory)
    monkeypatch.setattr(runner_module, "SessionLocal", factory)
    monkeypatch.setattr(
        runner_module, "AGENT_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db")
    )
    monkeypatch.setattr(call_module, "CALL_ENABLED", True)
    monkeypatch.setattr(call_module, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(call_module, "make_transcriber", lambda **kw: None)

    stt = _FakeSTT("I'd like to test drive the Jetour T2")
    monkeypatch.setattr(voice_service, "_client", lambda: stt)
    monkeypatch.setattr(voice_service, "OPENAI_API_KEY", "test-key")
    tts = _FakeTTS(b"\xffMP3" * 40)
    monkeypatch.setattr(speech_service, "_client", lambda: tts)
    monkeypatch.setattr(speech_service, "OPENAI_API_KEY", "test-key")

    gate = threading.Event()
    monkeypatch.setattr(
        call_module,
        "run_turn",
        functools.partial(runner_module.run_turn, generate=GatedTwoSentenceModel(gate)),
    )
    return {"stt": stt, "tts": tts, "gate": gate, "monkeypatch": monkeypatch}


@pytest.fixture()
def client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _speak_one_utterance(ws) -> None:
    ws.send_json({"type": "start", "sample_rate": 16000})
    ws.send_bytes(PCM_HALF_SECOND)
    ws.send_json({"type": "end_turn"})


def _read_segment(ws) -> bytes:
    """Skip JSON frames to the next audio_start, then collect the segment.

    Skipping is deliberate: the token event and the speaker's state change
    cross the event loop as separate callbacks, so their relative order is
    scheduling luck — a reader that assumed one would be flaky by design."""
    import json as _json

    for _ in range(200):
        frame = ws.receive()
        assert frame.get("text") is not None, "binary frame before audio_start"
        if _json.loads(frame["text"]).get("type") == "audio_start":
            break
    else:
        raise AssertionError("never saw audio_start")
    audio = b""
    for _ in range(400):
        raw = ws.receive()
        if raw.get("bytes") is not None:
            audio += raw["bytes"]
            continue
        # Since streamed synthesis, OTHER json (tokens, tool_events) can land
        # between a segment's bytes — the reply is still generating while its
        # opening is already playing. Only audio_end closes the segment.
        if _json.loads(raw["text"]).get("type") == "audio_end":
            return audio
    raise AssertionError("segment never closed with audio_end")


def test_voice_anchor_only_on_call_turns():
    """The register instruction rides ONLY the call channel: a call turn's
    system prompt carries it, a text turn's prompt is byte-identical to
    before it existed."""
    from app.agent.prompt import system_prompt

    assert "LIVE VOICE CALL" in system_prompt(voice_call=True)
    assert "LIVE VOICE CALL" not in system_prompt()


def test_the_gateway_arms_the_voice_anchor(db, client, stream_env):
    """End to end: the model called for a spoken turn actually SEES the anchor
    (trusted context through build_config, set by the gateway alone)."""
    seen_systems: list[str] = []

    class CapturingModel:
        def __call__(self, messages, *, model, system, tools, config=None):
            seen_systems.append(system)
            _emit_token("Right away.")
            return {
                "role": "assistant",
                "content": [{"type": "text", "text": "Right away."}],
            }

    stream_env["monkeypatch"].setattr(
        call_module,
        "run_turn",
        functools.partial(runner_module.run_turn, generate=CapturingModel()),
    )
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        _speak_one_utterance(ws)
        _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    # The cache split hands the model (stable, volatile) rather than one
    # string; the anchor is volatile by design. Join before searching, so this
    # keeps asserting WHAT the model sees, not how the prompt is packaged.
    assert seen_systems
    first = seen_systems[0]
    blob = first if isinstance(first, str) else "".join(first)
    assert "LIVE VOICE CALL" in blob


def test_first_sentence_is_spoken_while_the_second_still_generates(db, client, stream_env):
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        _speak_one_utterance(ws)
        _drain_until(ws, "utterance")

        # Aisha starts SPEAKING while the model is gated — proof the speech
        # did not wait for the reply to finish. (The token and the state
        # change race benignly; only the audio's timing is the claim here.)
        speaking, _ = _drain_until(ws, "state")
        assert speaking == {"type": "state", "value": "speaking"}
        segment_1 = _read_segment(ws)
        assert segment_1  # first sentence's audio, generation still frozen
        assert len(stream_env["tts"].calls) == 1
        assert stream_env["tts"].calls[0]["input"] == for_speech(SENTENCE_1.strip())

        # Unfreeze: sentence 2 streams, is synthesised, and the turn completes.
        stream_env["gate"].set()
        done, seen = _drain_until(ws, "done")
        segment_2 = _read_segment(ws)
        assert segment_2
        assert len(stream_env["tts"].calls) == 2
        assert stream_env["tts"].calls[1]["input"] == for_speech(SENTENCE_2)
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        ws.send_json({"type": "hangup"})


def test_barge_in_silences_the_rest_but_never_the_transcript(db, client, stream_env):
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        _speak_one_utterance(ws)
        _drain_until(ws, "utterance")
        _drain_until(ws, "state")  # speaking
        _read_segment(ws)  # sentence 1's audio is out — Aisha is mid-reply

        # The customer talks over her. The client stops playback and says so.
        ws.send_json({"type": "barge_in"})
        # Their interrupting speech keeps flowing while the turn drains…
        ws.send_bytes(PCM_HALF_SECOND)
        ws.send_json({"type": "end_turn"})

        # …then the gated model finishes writing sentence 2.
        stream_env["gate"].set()

        # Sentence 2 is NEVER synthesised, and the customer's held utterance
        # starts the NEXT turn (state thinking) instead of the call going
        # quiet. No audio may arrive between the barge and that turn.
        seen = []
        for _ in range(200):
            frame = ws.receive_json()
            seen.append(frame)
            if frame == {"type": "state", "value": "thinking"}:
                break
        else:
            raise AssertionError(f"second turn never started: {seen}")
        assert not any(f.get("type") == "audio_start" for f in seen)
        _drain_until(ws, "done")  # the follow-up turn completes
        ws.send_json({"type": "hangup"})

    # Sentence 1 was spoken; sentence 2 was interrupted away and NEVER
    # synthesised (the follow-up turn's audio is its own business).
    spoken = [c["input"] for c in stream_env["tts"].calls]
    assert for_speech(SENTENCE_1.strip()) in spoken
    assert for_speech(SENTENCE_2) not in spoken

    # THE FULL TEXT SURVIVED THE INTERRUPTION — both sentences, stored.
    db.expire_all()
    agent_turns = db.scalars(
        select(ConversationTurn)
        .where(
            ConversationTurn.session_id == SESSION,
            ConversationTurn.speaker == Speaker.agent,
        )
        .order_by(ConversationTurn.id)
    ).all()
    assert (SENTENCE_1 + SENTENCE_2) == agent_turns[0].text
