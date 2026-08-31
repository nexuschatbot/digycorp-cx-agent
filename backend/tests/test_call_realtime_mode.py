"""CALL_MODE=realtime — the speech-to-speech engine, offline.

The vendor session is scripted through the make_agent_session seam (the
realtime_stt pattern); what is under test is the GATEWAY's contract:

  - the same client protocol as pipeline mode, plus PCM segments;
  - THE CALL IS EPHEMERAL (2026-07-29): nothing is stored mid-call — no
    VoiceNote, no per-turn rows — and hangup leaves exactly ONE
    Speaker.system summary row ("call:<seconds>") behind;
  - a function call runs through THE SAME dispatch choke point — audited
    tool_call row, trusted-context injection — and its result returns to the
    vendor session;
  - mode selection is config, and pipeline mode is untouched by it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import call as call_module
from app.api import call_realtime as rt_module
from app.db import get_db
from app.main import app
from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    ConversationTurn,
    Speaker,
    VoiceNote,
)
from app.services import voice as voice_service

from tests.test_call_gateway import PCM_HALF_SECOND, SESSION


def _drain_until(ws, wanted_type: str, *, limit: int = 300):
    """Like the gateway tests' helper, but BINARY-TOLERANT: in realtime mode
    audio frames interleave freely with JSON events, so non-JSON frames are
    skipped rather than crashing the reader."""
    import json as _json

    seen = []
    for _ in range(limit):
        frame = ws.receive()
        if frame.get("bytes") is not None:
            continue
        if frame.get("text") is None:  # socket closed under us
            raise AssertionError(
                f"socket closed waiting for {wanted_type!r}; saw {[m.get('type') for m in seen]}"
            )
        msg = _json.loads(frame["text"])
        seen.append(msg)
        if msg.get("type") == wanted_type:
            return msg, seen
    raise AssertionError(f"never saw {wanted_type!r}; saw {[m.get('type') for m in seen]}")


class FakeAgentSession:
    """Scripted speech-to-speech session. `script` maps the Nth send_audio
    call (1-based) to a list of normalised events to emit; send_tool_result
    triggers `after_tool` events. Emission happens inside awaited methods, so
    events land on the gateway's loop exactly as the real reader would."""

    def __init__(self, on_event, script: dict, after_tool: list | None = None) -> None:
        self.on_event = on_event
        self.script = script
        self.after_tool = after_tool or []
        self.audio_calls = 0
        self.tool_results: list[tuple[str, dict]] = []
        self.seeded: list[tuple[str, str]] = []
        self.cancelled = False
        self.closed = False
        self.greeted = False

    async def connect(self) -> bool:
        return True

    async def greet(self) -> None:
        self.greeted = True

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_calls += 1
        for event in self.script.get(self.audio_calls, []):
            self.on_event(event)

    async def send_tool_result(self, call_id: str, result: dict) -> None:
        self.tool_results.append((call_id, result))
        for event in self.after_tool:
            self.on_event(event)

    async def cancel_response(self) -> None:
        self.cancelled = True

    async def request_response(self, lang=None, *, note=None) -> None:
        self.requested = getattr(self, "requested", 0) + 1

    async def seed_history(self, turns) -> None:
        self.seeded = list(turns)

    async def close(self) -> None:
        self.closed = True


REPLY_EVENTS = [
    {"type": "assistant_delta", "text": "Hello! How can I help?"},
    {"type": "audio", "data": b"\x01\x02" * 600},
    {"type": "audio", "data": b"\x03\x04" * 600},
    {"type": "response_done"},
]

HEARD_EVENTS = [
    {"type": "user_speech_started"},
]
STOPPED_EVENTS = [
    {"type": "user_speech_stopped"},
    {"type": "user_transcript", "text": "Hi Aisha, how are you?"},
]


@pytest.fixture()
def rt_env(db, monkeypatch, tmp_path):
    engine = db.get_bind()
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(call_module, "SessionLocal", factory)
    monkeypatch.setattr(rt_module, "SessionLocal", factory)
    monkeypatch.setattr(call_module, "CALL_ENABLED", True)
    monkeypatch.setattr(call_module, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(call_module, "CALL_MODE", "realtime")
    # The split-memory narrowing writes call outcomes into the LangGraph
    # checkpoint — isolate it per test, never the repo-root file.
    monkeypatch.setattr(
        rt_module, "AGENT_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db")
    )

    holder: dict = {}

    def install(script: dict, after_tool: list | None = None) -> None:
        def factory_fn(*, instructions, vocabulary, on_event):
            holder["instructions"] = instructions
            holder["vocabulary"] = vocabulary
            holder["session"] = FakeAgentSession(on_event, script, after_tool)
            return holder["session"]

        monkeypatch.setattr(rt_module, "make_agent_session", factory_fn)

    holder["install"] = install
    holder["monkeypatch"] = monkeypatch
    return holder


@pytest.fixture()
def client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_full_realtime_turn_speaks_and_records_both_sides(db, client, rt_env):
    rt_env["install"]({
        1: HEARD_EVENTS,
        2: STOPPED_EVENTS + REPLY_EVENTS,
    })
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json()["type"] == "call_started"
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        # The vendor's VAD owns turn-taking in this mode, always.
        assert ws.receive_json() == {"type": "vad", "server": True}

        ws.send_json({"type": "start", "sample_rate": 24000})
        ws.send_bytes(PCM_HALF_SECOND)  # -> speech_started (capturing on)
        ws.send_bytes(PCM_HALF_SECOND)  # -> transcript + the spoken reply

        # COLLECT-THEN-ASSERT: audio frames are pushed synchronously and the
        # interleaving of events around them is scheduling, not contract.
        # `done` is the turn's terminal event.
        import json as _json

        audio = b""
        events: list[dict] = []
        for _ in range(300):
            frame = ws.receive()
            if frame.get("bytes") is not None:
                audio += frame["bytes"]
                continue
            assert frame.get("text") is not None, "socket closed mid-turn"
            msg = _json.loads(frame["text"])
            events.append(msg)
            if msg.get("type") == "done":
                break
        by_type = {m["type"]: m for m in events}
        utterance = by_type["utterance"]
        assert utterance["text"] == "Hi Aisha, how are you?"
        # Ephemeral call: no recording is stored, so there is nothing to point
        # a player at — and no turn to point read-aloud at either.
        assert utterance["audio_id"] is None
        assert by_type["pcm_start"]["rate"] == 24_000
        assert "pcm_end" in by_type
        assert audio == b"\x01\x02" * 600 + b"\x03\x04" * 600
        assert by_type["done"]["turn_id"] is None
        ws.send_json({"type": "hangup"})
        _drain_until(ws, "call_ended")

    db.expire_all()
    # WHAT AISHA SAID IS NOW RECORDED, and this assertion was reversed on
    # 2026-08-14 to say so. It used to read "what was SAID is nowhere; what
    # HAPPENED is one system marker row", which was the contract when a call
    # stored only its closing summary.
    #
    # That changed when call transcripts were fixed (C4): every agent reply is
    # stored as a turn, not just the farewell, because a transcript showing only
    # the customer's half cannot be reviewed or scored — which is the entire
    # reason Elite Group asked for call transcripts. The system marker still
    # follows, carrying the call duration.
    #
    # BOTH SIDES, PLUS THE MARKER — agent, customer, system, in that order. The
    # agent row lands first because a reply is persisted as it is spoken, while
    # the customer's utterance is written once their turn completes.
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.session_id == SESSION)
        .order_by(ConversationTurn.id)
    ).all()
    # BY SPEAKER, NOT BY POSITION — and that is a finding, not a convenience.
    # Successive runs of this test produced [agent, customer, system] and
    # [customer, agent, system]: the reply and the utterance are written close
    # enough together that their row ids do not order reliably. Asserting a fixed
    # order would make this test flap, and asserting it would also be asserting
    # something the code does not actually guarantee.
    #
    # ⚠️ The transcript view orders by id, so a REAL call could in principle
    # render Aisha's answer above the question it answered. Not reproduced
    # outside this harness — the scripted events arrive with no human pause
    # between them — but it is the reason this is written down rather than
    # papered over.
    by_speaker = {t.speaker: t for t in turns}
    assert set(by_speaker) == {Speaker.agent, Speaker.customer, Speaker.system}
    assert by_speaker[Speaker.agent].text == "Hello! How can I help?"
    assert by_speaker[Speaker.customer].text == "Hi Aisha, how are you?"
    summary = by_speaker[Speaker.system]
    assert summary.text.startswith("call:")  # the marker carries the duration
    assert summary.cards == []  # no booking made on this call
    assert db.scalars(select(VoiceNote)).all() == []
    # She answered the phone — the greeting fired before any customer audio.
    assert rt_env["session"].greeted is True
    # The persona and the catalogue reached the session.
    assert "live voice call" in rt_env["instructions"].lower()
    # The COMPACT persona, not the 20k master prompt — the full prompt burned
    # the vendor's per-minute token limit in two replies (live, 2026-07-27).
    assert len(rt_env["instructions"]) < 6000
    assert "Jetour" in rt_env["vocabulary"]


def test_function_calls_run_through_the_audited_choke_point(db, client, rt_env):
    rt_env["install"](
        {
            1: [
                {"type": "function_call", "name": "search_vehicles",
                 "call_id": "call_123", "arguments": {"name": "T2"}},
            ],
        },
        after_tool=REPLY_EVENTS,
    )
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        ws.send_bytes(PCM_HALF_SECOND)

        tool_event, _ = _drain_until(ws, "tool_event")
        assert tool_event["tool"] == "search_vehicles"
        assert tool_event["output"].get("matches"), "the real tool ran"
        _drain_until(ws, "done")
        ws.send_json({"type": "hangup"})

    session = rt_env["session"]
    assert session.tool_results and session.tool_results[0][0] == "call_123"
    assert session.tool_results[0][1].get("matches")

    db.expire_all()
    row = db.scalar(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.tool_call
        )
    )
    assert row is not None
    assert row.actor is Actor.agent
    assert row.payload["tool"] == "search_vehicles"
    assert row.payload["engine"] == "realtime"
    assert row.payload["correlation_id"] == "call_123"  # stamped into payload (see audit.record)


def test_vendor_vad_may_end_turns_but_never_interrupt(monkeypatch):
    """The session config pins interrupt_response FALSE in both VAD modes: 48
    replies died to `turn_detected` blips across two live calls (2026-07-30).
    The vendor's VAD decides when the CUSTOMER's turn ends — interruption of
    Aisha is ours alone. CALL_TURN_DETECTION flips the end-of-turn detector
    without touching that."""
    from app.services import realtime_agent as ra

    session = ra.RealtimeAgentSession(
        instructions="x", vocabulary="", on_event=lambda e: None
    )
    td = session._session_update()["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["interrupt_response"] is False
    # …and never AUTO-ANSWER either (2026-07-30): the vendor VAD is a
    # segmenter; every reply is created by the gateway's turn engine at
    # transcript time, so sounds that transcribe to nothing draw no reply.
    assert td["create_response"] is False

    monkeypatch.setattr(ra, "CALL_TURN_DETECTION", "semantic_vad")
    monkeypatch.setattr(ra, "CALL_VAD_EAGERNESS", "low")
    td = session._session_update()["session"]["audio"]["input"]["turn_detection"]
    assert td == {
        "type": "semantic_vad",
        "eagerness": "low",
        "interrupt_response": False,
        "create_response": False,
    }


def test_language_pin_lives_at_session_level_and_creates_are_bare():
    """The prompt-speaking fix (2026-08-01): a 6k-char instruction payload
    replacing the persona on EVERY reply is what the model sometimes read
    ALOUD. The pin now lives in the SESSION instructions, refreshed only
    when it CHANGES — so ordinary replies (tool follow-ups included) are
    BARE creates with nothing attached to read. The pin still survives the
    whole turn: it is standing session state."""
    import asyncio as _asyncio

    from app.services import realtime_agent as ra

    session = ra.RealtimeAgentSession(
        instructions="PERSONA", vocabulary="", on_event=lambda e: None
    )
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    session._send = fake_send  # type: ignore[method-assign]

    # Never pinned: a bare create — the session persona applies.
    _asyncio.run(session.request_response(None))
    assert sent == [{"type": "response.create"}]

    # A NEW pin refreshes the session instructions, then a bare create.
    sent.clear()
    _asyncio.run(session.request_response("ar"))
    assert sent[0]["type"] == "session.update"
    assert sent[0]["session"]["instructions"].startswith("PERSONA")
    assert "Arabic ONLY" in sent[0]["session"]["instructions"]
    assert sent[1] == {"type": "response.create"}

    # The tool follow-up rides the standing session pin — bare.
    sent.clear()
    _asyncio.run(session.send_tool_result("call_1", {"ok": True}))
    assert sent[0]["type"] == "conversation.item.create"
    assert sent[1] == {"type": "response.create"}

    # Same pin again: NO refresh, just the bare create.
    sent.clear()
    _asyncio.run(session.request_response("ar"))
    assert sent == [{"type": "response.create"}]

    # A pin CHANGE refreshes once more.
    sent.clear()
    _asyncio.run(session.request_response("en"))
    assert sent[0]["type"] == "session.update"
    assert "English ONLY" in sent[0]["session"]["instructions"]
    assert sent[1] == {"type": "response.create"}

    # set_language (the call-start pin) refreshes without a reply.
    sent.clear()
    session.set_language("en")  # unchanged: no traffic at all
    assert sent == []


def test_language_guard_redoes_a_disobedient_reply(db, rt_env):
    """C of the language design: a reply that BEGINS in the wrong language is
    cancelled within its first words and redone under the same pin — once.
    A matching reply passes untouched; a second mismatch plays out (an
    accent beats a loop). The redo carries a no-narration note (live
    2026-08-03: the model, seeing its own cancelled opening, told the
    customer 'I'll keep going in English' — remarking on the very switch
    the persona forbids remarking on)."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []
    notes: list[str | None] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")
            notes.append(note)

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()
    session.reply_lang_pin = "ar"

    async def scenario() -> None:
        # The reply streams in ENGLISH against an Arabic pin.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta", "text": "The Jetour T2 starts at ninety"}
        )
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create:ar"]  # killed and redone, same pin
        # The redo is told to just answer — never to narrate the language.
        assert notes and notes[-1] is not None
        assert "Do NOT mention language" in notes[-1]
        assert {"type": "stop_audio"} in sent
        assert session.discard_stale_audio is True
        assert session.reply_text == ""  # the false start never accumulates

        # The redo comes back in Arabic: passes, nothing else fires.
        calls.clear()
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta", "text": "سعر الجيتور تي تو يبدأ من تسعين ألف درهم"}
        )
        await _asyncio.sleep(0)
        assert calls == []

        # A second mismatch in the same turn is let through — one retry only.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta", "text": "The price starts at ninety thousand"}
        )
        await _asyncio.sleep(0)
        assert calls == []

    _asyncio.run(scenario())


def test_both_call_personas_forbid_reading_agreement_into_a_non_answer():
    """Live 2026-08-03: offered Al Quoz, Deira or Abu Dhabi, she took "Okay,
    have a look." as choosing Al Quoz; asked "shall I lock that in?", she took
    the customer's Arabic GREETING as a yes and held the slot. The chat prompt
    has carried this discipline since the compound-question hardening; the
    call personas are compressed briefs and carried none of it. Both now do —
    the natural persona is the active one, so a rule only in the classic
    would not have run on that call."""
    from app.agent.call_persona import natural_instructions
    from app.agent.prompt import realtime_instructions

    for brief in (natural_instructions(), realtime_instructions()):
        assert "ANSWER ANSWERS THE QUESTION YOU ASKED" in brief
        assert "not a choice and not a yes" in brief
        # And the concrete failure it was written for: a slot is held only
        # when the customer NAMES one.
        assert "lock_slot only" in brief


def test_the_same_hello_transcribed_twice_is_answered_once(db, rt_env):
    """THE TWO-VOICES BUG (live 2026-08-03, dev VM). One spoken hello came
    back as two transcripts 2.4s apart — 'Hey, hello, Aitha.' then 'Hi, hello
    Aisha.' — and each drew its own reply, so two of her voices played at
    once. The words are NOT identical (the transcriber spells the same audio
    differently each pass), which is why the guard tolerates one character
    per word. The duplicate is still DELIVERED as a turn — the customer did
    say it — but it is not answered a second time."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        session._decide_turn(9600, "Hey, hello, Aitha.")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        calls.clear()
        sent.clear()

        # The same hello again, while the first is still being answered:
        # no second reply, but the turn IS delivered.
        session._decide_turn(9600, "Hi, hello Aisha.")
        await _asyncio.sleep(0)
        assert calls == []
        assert any(m.get("type") == "utterance" for m in sent)

        # Once she has FINISHED answering, a repeat means she was not heard —
        # that must be answered afresh, never swallowed.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "response_done"})
        session._answered_at -= rt_module._DUPLICATE_WINDOW_S + 1
        session._decide_turn(9600, "Hi, hello Aisha.")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]

    _asyncio.run(scenario())


def test_near_repeat_tolerates_respelling_but_not_a_new_sentence():
    near = rt_module._near_repeat
    first = rt_module._normalize_words("Hey, hello, Aitha.")
    assert near(rt_module._normalize_words("Hi, hello Aisha."), first)
    # A genuinely different sentence is not a duplicate, however soon it comes.
    assert not near(rt_module._normalize_words("I want to book a test drive"), first)
    # One word is never enough to call something a repeat — a lone "yes"
    # legitimately follows a lone "yes".
    assert not near(["yes"], ["yes"])


def test_a_new_turns_reply_silences_audio_still_playing(db, rt_env):
    """Belt to the duplicate guard's braces: whatever else lets a second
    reply begin, the customer must never hear two voices. A reply to a NEW
    customer turn clears the peer connection's queue and tells the client to
    stop — but a TOOL FOLLOW-UP does not, because it continues the same
    answer and clipping it would cut 'one moment, let me check' mid-word."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    class Fake:
        async def cancel_response(self) -> None:
            pass

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            pass

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        session._decide_turn(9600, "I want to book a test drive")
        await _asyncio.sleep(0)
        sent.clear()
        session._on_agent_event({"type": "response_started"})
        assert {"type": "stop_audio"} in sent

        # A tool follow-up's reply begins without a new customer turn: her
        # own sentence is left to finish.
        sent.clear()
        session._on_agent_event({"type": "response_done"})
        session._on_agent_event({"type": "response_started"})
        assert {"type": "stop_audio"} not in sent

    _asyncio.run(scenario())


def test_a_reply_that_never_starts_is_re_asked_then_released(db, rt_env):
    """THE GO-SILENT BUG (live 2026-08-03, four dev-VM calls): the vendor
    accepted the supersede's response.create and produced NOTHING — no
    response.created, no error, no rejection — so the call sat in 'thinking'
    for good. Nothing could rescue it: the idle ladder only runs while
    LISTENING, so the nudge and the silence hangup were both unreachable and
    the customer repeated themselves into a dead line. One bounded re-ask,
    then the call is handed back to the ladder."""
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        now = _time.monotonic()
        session._decide_turn(9600, "I want to book a test drive")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        assert session._awaiting_reply_since is not None
        calls.clear()

        # Well inside the window: nothing is re-asked (a slow vendor is not a
        # dead one).
        session._watchdog_tick(now + 1)
        await _asyncio.sleep(0)
        assert calls == []

        # Past it, with no response ever started: exactly one re-ask.
        session._watchdog_tick(now + rt_module._REPLY_START_MAX_S + 1)
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        calls.clear()

        # Still nothing: the call stops waiting, drops any embargo the dead
        # reply raised, and returns to listening so the idle ladder can run.
        session.discard_stale_audio = True
        session._watchdog_tick(now + 2 * rt_module._REPLY_START_MAX_S + 2)
        await _asyncio.sleep(0)
        assert calls == []  # never an endless re-ask
        assert session._awaiting_reply_since is None
        assert session.discard_stale_audio is False
        assert session.state == "listening"

        # A reply that DOES start disarms the rescue outright.
        session._decide_turn(9600, "are you there?")
        await _asyncio.sleep(0)
        calls.clear()
        session._on_agent_event({"type": "response_started"})
        assert session._awaiting_reply_since is None
        session._watchdog_tick(now + 60)
        await _asyncio.sleep(0)
        assert calls == []

    _asyncio.run(scenario())


def test_language_pin_is_absolute_script_decides_not_the_model(db, rt_env):
    """THE LANGUAGE POLICY (one language per call, 2026-08-03, superseding
    the per-transcript rule of 2026-08-01): the pin exists from the first
    breath ('en'), rides every response, and the model's insistence never
    flips it — the adoption ladder is gone (it let the model out-vote the
    rule, which is how a muted English call got an Arabic nudge). A
    persisting mismatch plays with a warning, but the pin stands and the
    next request still carries it. The ONLY thing that can move the pin is
    the lock itself: an unlocked call's first clear utterance."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

        def set_language(self, lang) -> None:
            calls.append(f"set:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()

    ARABIC = "أهلاً وسهلاً، تشرفنا فيك، كيف أقدر أساعدك اليوم؟"

    # The pin is never None — English from the first breath.
    assert session.reply_lang_pin == "en"

    async def scenario() -> None:
        # A Latin transcript keeps the en pin, and the request carries it.
        session._respond(supersede=False)
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        calls.clear()

        # Her reply streams Arabic against the en pin: strike one — redone
        # under the SAME pin.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "assistant_delta", "text": ARABIC})
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create:en"]
        calls.clear()

        # The redo defies the order too: it PLAYS (a wrong-language reply
        # beats a dead line) — but the pin does NOT flip, the session is
        # never re-pinned, and nothing was taken back.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "assistant_delta", "text": ARABIC})
        await _asyncio.sleep(0)
        assert calls == []  # no adoption, no set:, no kill
        assert session.reply_lang_pin == "en"
        assert session.discard_stale_audio is False
        assert session.reply_text != ""

        # The next Latin transcript still pins English — the model's
        # insistence changed nothing.
        session._on_agent_event({"type": "response_done"})
        session._respond(supersede=False)
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        calls.clear()

        # The only pin movement left in the design: an UNLOCKED call is
        # locked by the customer's first clear Arabic utterance — the one
        # language the whole call now keeps.
        session._on_agent_event({"type": "response_done"})
        session._decide_turn(9600, ARABIC)
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]
        assert session.reply_lang_pin == "ar"
        assert session.lang_locked is True
        calls.clear()

        # And an Arabic reply now passes in silence.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "assistant_delta", "text": ARABIC})
        await _asyncio.sleep(0)
        assert calls == []

    _asyncio.run(scenario())


def test_short_answers_survive_on_their_words(db, rt_env):
    """'Yes, correct.' — a customer confirming a cancellation — was dropped
    as sub-perceptual for measuring 1536 bytes (live 2026-08-01), and Aisha
    went silent at the worst possible moment. Words win: a transcript with
    real words is believed regardless of the byte count; a wordless one is
    still dropped."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # A short confirmation with a tiny byte count still gets a reply —
        # and still counts as a customer turn (the consent guard's number).
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 1536
        session._on_agent_event({"type": "user_speech_stopped"})
        session._on_agent_event({"type": "user_transcript", "text": "Yes, correct."})
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        assert session.customer_turns == 1

        # Wordless noise is still nothing.
        calls.clear()
        session._on_agent_event({"type": "response_done"})
        session._on_agent_event({"type": "user_transcript", "text": " ... "})
        await _asyncio.sleep(0)
        assert calls == []

    _asyncio.run(scenario())


def test_transcript_deltas_answer_when_completed_never_arrives(db, rt_env, monkeypatch):
    """The no-reply bug's second face (live 2026-08-01, twice): the vendor
    streams transcription DELTAS but the completed event never lands — the
    turn engine waited forever and the customer got nothing. Deltas are now
    accumulated; a delta also ARMS the fallback (transcription only runs on
    committed audio, so a delta proves an utterance ended); and when the
    timer fires with delta text in hand, it answers from those words as an
    ordinary turn — Arabic script and all."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    monkeypatch.setattr(rt_module, "_TRANSCRIPT_FALLBACK_S", 0.01)
    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # No speech events, no completed transcript — deltas only.
        session._on_agent_event({"type": "user_transcript_delta", "text": "مرحبا "})
        session._on_agent_event({"type": "user_transcript_delta", "text": "أبغى أجرب السيارة"})
        await _asyncio.sleep(0.05)
        assert calls == ["create:ar"]  # answered, in Arabic, from the deltas

        # A completed transcript arriving normally CLEARS the pending text —
        # the fallback must never answer the same words twice.
        calls.clear()
        session._on_agent_event({"type": "response_done"})
        session._on_agent_event({"type": "user_speech_started", "at_ms": 0})
        session._on_agent_event({"type": "user_transcript_delta", "text": "hello"})
        session._on_agent_event({"type": "user_speech_stopped", "at_ms": 2000})
        session._on_agent_event({"type": "user_transcript", "text": "hello there"})
        await _asyncio.sleep(0.05)
        # Exactly one reply, from the completed text — still under the Arabic
        # pin: the call locked on its first clear utterance (2026-08-03).
        assert calls == ["create:ar"]

    _asyncio.run(scenario())


def test_vendor_vad_duration_rescues_an_undercounted_utterance(db, rt_env):
    """The no-reply bug (live 2026-08-01): our byte counter starts at the
    vendor's speech_started event, so audio forwarded during that round
    trip is missed — a real Arabic utterance counted near zero and its
    transcript was SILENTLY dropped as hallucination. The vendor's own VAD
    timestamps (audio_start_ms/audio_end_ms) are first-hand evidence and
    outrank the count; a genuine blip is still dropped."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # 2.4s of speech by the VENDOR's clock; zero by our counter.
        session._on_agent_event({"type": "user_speech_started", "at_ms": 1000})
        session._on_agent_event({"type": "user_speech_stopped", "at_ms": 3400})
        session._on_agent_event({
            "type": "user_transcript", "text": "مرحبا أبغى أجرب السيارة",
        })
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]  # the reply exists — and in Arabic
        calls.clear()

        # A genuine blip (60ms, no bytes) is still dropped.
        session._on_agent_event({"type": "response_done"})
        session._on_agent_event({"type": "user_speech_started", "at_ms": 9000})
        session._on_agent_event({"type": "user_speech_stopped", "at_ms": 9060})
        await _asyncio.sleep(0)
        assert calls == []

        # Timestamps absent (relay shapes): the old byte rule stands.
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 50_000
        session._on_agent_event({"type": "user_speech_stopped"})
        session._on_agent_event({"type": "user_transcript", "text": "hello there"})
        await _asyncio.sleep(0)
        # Still the Arabic pin: the call locked on its first clear utterance
        # (2026-08-03) and an English turn no longer re-pins it.
        assert calls == ["create:ar"]

    _asyncio.run(scenario())


def test_pin_lang_is_majority_script_not_any_char():
    """The mixed-reply bug (2026-07-31): _detect_lang's any-Arabic-char rule
    pinned 'Arabic ONLY' off one Arabic word in a code-switched transcript,
    and the model split the difference and spoke both languages in one
    reply. The PIN detector is majority-script: a sentence spoken entirely
    in Arabic (Latin brand names tolerated) pins Arabic; anything mixed or
    Latin pins English; no letters at all keeps the previous pin (None)."""
    pin = rt_module._pin_lang
    assert pin("I wanted to know about the Jetour T2") == "en"
    assert pin("هلا مسهلا") == "ar"
    # A real Arabic sentence keeps its verdict despite Latin brand names.
    assert pin("أبغى أجرب الـ Jetour T2") == "ar"
    # A code-switched, mostly-English sentence is English — the user's rule.
    assert pin("yes خلاص book it for Saturday please") == "en"
    # Eastern-digit numbers are NOT Arabic prose: a phone number dictated
    # mid-English-booking must not flip the conversation to Arabic.
    assert pin("٠٥٠١٢٣٤٥٦٧") is None
    assert pin("0501234567") is None
    assert pin("") is None
    # The rendering rule is untouched: one Arabic word still makes the
    # bubble RTL even though it no longer re-pins the reply.
    assert rt_module._detect_lang("yes خلاص book it") == "ar"
    # The LOCK detector is stricter still: one word — however clearly
    # scripted — is never enough to commit a whole call to a language.
    lock = rt_module._lock_verdict
    assert lock("آه") is None
    assert lock("hello") is None
    assert lock("٠٥٠١٢٣٤٥٦٧ ٠٥٠") is None  # words of digits, no letters
    assert lock("أهلاً وسهلاً") == "ar"
    assert lock("hello there") == "en"


def test_first_clear_utterance_locks_the_call_for_good(db, rt_env):
    """One language per call (2026-08-03): the first CLEAR utterance locks
    the call, and after that no transcript — code-switched, fully in the
    other language, or digits-only — ever changes what she speaks. This
    replaces the per-transcript re-pinning test: the middle utterance here
    used to flip the pin to en, which is exactly the behaviour removed."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # A clear Arabic opening locks the call to Arabic.
        session._decide_turn(9600, "يعطيك العافية، أبغى أحجز تجربة قيادة")
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]
        assert session.reply_lang_pin == "ar"
        assert session.lang_locked is True
        calls.clear()

        # A code-switched, mostly-English turn no longer flips anything —
        # she understands it and answers in the call's language.
        session._on_agent_event({"type": "response_done"})
        session._decide_turn(9600, "خلاص let's book the Jetour T2 for Saturday")
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]
        assert session.reply_lang_pin == "ar"
        calls.clear()

        # A dictated number carries no language: the pin stands — and is
        # STILL passed explicitly (no response goes out unpinned).
        session._on_agent_event({"type": "response_done"})
        session._decide_turn(9600, "٠٥٠١٢٣٤٥٦٧")
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]
        assert session.reply_lang_pin == "ar"

    _asyncio.run(scenario())


def test_a_locked_english_call_answers_arabic_in_english(db, rt_env):
    """The other direction of the lock: a call locked to English by its
    first clear utterance keeps replying in English when the customer
    switches wholly to Arabic mid-call — the reply is to WHAT they said,
    in the call's language, and the switch is never remarked on (that half
    lives in the persona)."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        session._decide_turn(9600, "hi, I'd like to try the Jetour T2")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        assert session.lang_locked is True
        calls.clear()

        session._on_agent_event({"type": "response_done"})
        session._decide_turn(9600, "أبغى السيارة يوم السبت بعد المغرب")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        assert session.reply_lang_pin == "en"

    _asyncio.run(scenario())


def test_a_garbled_one_word_transcript_cannot_lock_the_call(db, rt_env):
    """The lock's threshold (_lock_verdict): the transcriber hallucinates
    one-word foreign-script 'utterances' out of background noise ('آه' seen
    live), and a call must never be committed to a language by a sound
    nobody made. One word answers in the unlocked default and locks
    nothing; the next clear sentence still gets to decide."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # A hallucinated one-word Arabic transcript: answered under the
        # English default, and the call stays UNLOCKED.
        session._decide_turn(9600, "آه")
        await _asyncio.sleep(0)
        assert calls == ["create:en"]
        assert session.reply_lang_pin == "en"
        assert session.lang_locked is False
        calls.clear()

        # The first real sentence still owns the decision.
        session._on_agent_event({"type": "response_done"})
        session._decide_turn(9600, "أهلاً، أبغى أجرب سيارة جيتور")
        await _asyncio.sleep(0)
        assert calls == ["create:ar"]
        assert session.reply_lang_pin == "ar"
        assert session.lang_locked is True

    _asyncio.run(scenario())


def test_language_guard_ignores_eastern_digits_in_a_reply(db, rt_env):
    """A number read back in Eastern digits must not tip the reply's script
    judgment: counting the whole Arabic BLOCK (which holds ٠-٩) once made an
    English read-back look majority-Arabic against an en pin — killing a
    correct reply. Letters only, both sides of the comparison."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        session._respond(supersede=False)
        await _asyncio.sleep(0)
        calls.clear()
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({
            "type": "assistant_delta",
            "text": "Let me read that back: ٠٥٠١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧٨٩ — is that right?",
        })
        await _asyncio.sleep(0)
        assert calls == []  # judged English (or abstained) — never killed

    _asyncio.run(scenario())


def test_seed_pin_takes_the_threads_language_for_the_greeting():
    """The seeded thread's last judgeable CUSTOMER turn — the language the
    chat is in when the customer presses Call — decides the call's language
    deterministically, and since 2026-08-03 that verdict LOCKS the whole
    call, greeting included. No seed, agent-only seed, or nothing judgeable
    leaves the call unlocked on the English default."""
    seed_pin = rt_module._seed_pin
    assert seed_pin([("customer", "هلا، أبغى أجرب السيارة"), ("agent", "أهلاً!")]) == "ar"
    assert seed_pin([("customer", "hello there"), ("agent", "hi!")]) == "en"
    # The LAST customer turn wins.
    assert seed_pin([
        ("customer", "هلا"),
        ("agent", "أهلاً"),
        ("customer", "ok great, thanks, that works"),
    ]) == "en"
    # A letterless last turn (a typed number) falls back to the one before.
    assert seed_pin([
        ("customer", "هلا أبغى أحجز"),
        ("customer", "٠٥٠١٢٣٤٥٦٧"),
    ]) == "ar"
    assert seed_pin([]) is None
    assert seed_pin([("agent", "Hello! How can I help?")]) is None


def test_language_pin_forbids_mixing(rt_env):
    """The pin's own wording carries the anti-mix order — the enforcement
    half of the mixed-reply fix that the guard cannot provide (it judges
    only the reply's opening letters; mixing later must be forbidden at
    the instruction). The pin's home is the SESSION instructions
    (2026-08-01); a note-carrying response repeats the full stack because
    response instructions REPLACE the session's."""
    from app.services.realtime_agent import RealtimeAgentSession

    s = RealtimeAgentSession(
        instructions="PERSONA", vocabulary="", on_event=lambda _e: None
    )
    s._pinned_lang = "ar"
    ar = s._instructions_update()["session"]["instructions"]
    assert "Arabic ONLY" in ar and "Never mix English" in ar
    s._pinned_lang = "en"
    en = s._instructions_update()["session"]["instructions"]
    assert "English ONLY" in en and "Never mix Arabic" in en
    # A noted response carries persona + pin line again — it must, since it
    # replaces the session instructions for that one reply.
    noted = s._response_payload("note")["response"]["instructions"]
    assert noted.startswith("PERSONA") and "English ONLY" in noted

    # tools_off (the silence nudge): the response carries an EMPTY tool set
    # AND tool_choice none (`tools: []` alone was measured live 2026-08-01
    # being ignored) — a check-in structurally cannot call a tool.
    noted_off = s._response_payload("note", tools_off=True)["response"]
    assert noted_off["tools"] == [] and noted_off["tool_choice"] == "none"
    s._pinned_lang = None
    off = s._response_payload(None, tools_off=True)["response"]
    assert off["tools"] == [] and off["tool_choice"] == "none"
    assert "PERSONA" in off["instructions"]


def test_reference_guard_kills_an_invented_booking_ref(db, rt_env):
    """Rule 2's reference clause, structural (2026-07-31): the model once
    'confirmed' a booking that never existed and then 'cancelled'
    TD-2026-0307, a ref composed from a slot id. A spoken TD- reference no
    tool result ever contained is cancelled mid-breath and redone with a
    corrective note; a reference a tool DID return passes untouched; one
    retry, then it plays (the language guard's budget)."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}:{'note' if note else 'bare'}")
            if note:
                assert "TD-2026-QQQQ" in note  # the correction names the fake

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()
    session.reply_lang_pin = "en"
    # A real booking happened through the choke point: its ref is a fact.
    session._note_outcome(
        "create_booking", {"booking": {"booking_ref": "TD-2026-GJNV"}}
    )
    assert "TD-2026-GJNV" in session.known_refs

    async def scenario() -> None:
        # She speaks a reference NO tool ever returned — killed and redone,
        # same pin, with the corrective note riding the redo.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta",
             "text": "Perfect, your booking TD-2026-QQQQ is confirmed for Saturday"}
        )
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create:en:note"]
        assert {"type": "stop_audio"} in sent
        assert session.discard_stale_audio is True
        assert session.reply_text == ""  # the invention never accumulates

        # The redo speaks the REAL reference: passes, nothing fires.
        calls.clear()
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta",
             "text": "Your booking TD-2026-GJNV is all set for Saturday"}
        )
        await _asyncio.sleep(0)
        assert calls == []

        # A second invention in the same turn plays out — one retry only.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event(
            {"type": "assistant_delta", "text": "Reference TD-2026-ZZZZ, all done"}
        )
        await _asyncio.sleep(0)
        assert calls == []

        # A new customer turn refreshes the retry budget.
        session._respond(supersede=False)
        await _asyncio.sleep(0)
        assert session._ref_retry_done is False

    _asyncio.run(scenario())


def test_reference_harvesting_sees_tool_results_and_seeded_thread():
    """The whitelist is built from facts only: tool RESULTS (any shape, case
    normalised) and the typed thread the call resumed — never from the
    model's own arguments, which is where the invented ref arrived."""
    assert rt_module._harvest_refs(
        "we moved td-2026-gjnv to Sunday"
    ) == {"TD-2026-GJNV"}
    assert rt_module._harvest_refs("slot 307 at seven") == set()

    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    # A nested result shape (get_booking's list) harvests too.
    session._note_outcome(
        "get_booking",
        {"bookings": [{"booking_ref": "TD-2026-AB12"}, {"booking_ref": "TD-2026-CD34"}]},
    )
    assert {"TD-2026-AB12", "TD-2026-CD34"} <= session.known_refs


def test_a_reference_the_customer_spoke_is_one_she_may_read_back():
    """2026-08-05, the reference-identity change. The manage-a-booking journey
    now identifies by TD reference, so the customer says one BEFORE any tool
    result contains it — and reading it back to confirm is the right thing to do
    with a code you just heard. Without this the guard saw an unknown ref in her
    reply, called it invented, and cancelled her mid-word.

    Safe for the same reason heard_digits is: the model does not write the
    customer's transcript. Whitelisting a MIS-heard reference costs nothing
    either — this governs what she may SAY, never what exists; the tool still
    decides whether the booking is real.
    """
    from unittest.mock import MagicMock

    # Nobody says the dashes, so the spoken harvester reads the loose forms —
    # and stores them CANONICALLY, because her reply writes the dashes back in.
    assert rt_module._harvest_spoken_refs("it's TD 2026 GJNV") == {"TD-2026-GJNV"}
    assert rt_module._harvest_spoken_refs("td2026gjnv") == {"TD-2026-GJNV"}
    assert rt_module._harvest_spoken_refs("my number is 0501234567") == set()

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda payload: None  # type: ignore[method-assign]
    session._finish_utterance(4800, "yes, TD 2026 GJNV please")

    assert "TD-2026-GJNV" in session.known_refs


def test_lock_result_says_hold_not_booking(slots):
    """A of the phantom-confirmation fix: the lock RESULT itself states it is
    not a booking — schema descriptions fade over a long call; a result is
    read at the moment it matters."""
    from datetime import datetime

    from app.models import Slot, SlotStatus
    from app.services import slots as slots_service

    slot = (
        slots.query(Slot)
        .filter(Slot.status == SlotStatus.open, Slot.start > datetime.now())
        .first()
    )
    result = slots_service.lock(slots, session_id=SESSION, slot_id=slot.id)
    assert result["locked"] is True
    assert "NOT booked" in result["note"]
    # And the idempotent already-held path carries it too.
    again = slots_service.lock(slots, session_id=SESSION, slot_id=slot.id)
    assert again["already_held"] is True
    assert "NOT booked" in again["note"]


def test_spoken_digits_survive_every_form():
    """The transcriber writes numbers as digits, digit words, doubles, or
    Eastern Arabic numerals depending on its mood — evidence must survive
    all of them (EN + AR, the two call languages)."""
    sd = rt_module._spoken_digits
    assert sd("my number is 050 987 6543") == "0509876543"
    assert sd("zero five zero, nine eight seven, six five four three") == "0509876543"
    assert sd("oh five oh double nine eight") == "050998"
    assert sd("plus nine seven one five zero") == "97150"
    assert sd("صفر خمسة صفر تسعة ثمانية سبعة ستة خمسة أربعة ثلاثة") == "0509876543"
    assert sd("٠٥٠٩٨٧٦٥٤٣") == "0509876543"
    assert sd("we'll text +971 50 555 0103") == "9715055501" + "03"
    assert sd("no digits here at all") == ""


def test_mobile_evidence_guard_refuses_invented_numbers(db, rt_env):
    """The invented-identity incident (2026-07-31): the model passed
    find_or_create_customer a name and mobile the customer never said — one
    second after the licence step — then consent and a real booking followed.
    Identity is the chokepoint: a mobile no customer utterance ever contained
    is refused BEFORE dispatch, with the attempt kept in the audit trail; the
    same number, once actually spoken (in any form), passes."""
    from unittest.mock import MagicMock

    from app.models import Customer

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]

    # Fabricated: nobody said this number. Refused, never dispatched.
    refusal = session._run_tool_sync(
        "find_or_create_customer",
        {"name": "Mr. Nasser", "mobile": "+971509876543"},
        "call_fab",
    )
    assert refusal["error"] == "mobile_not_heard"
    assert db.query(Customer).filter_by(mobile="+971509876543").first() is None
    # The refused ATTEMPT is in the trail — the evidence of fabrication.
    trail = db.query(AuditEvent).filter(AuditEvent.session == SESSION).all()
    assert any(e.payload.get("tool") == "find_or_create_customer" for e in trail)

    # The customer actually says it — as WORDS — and the same call passes.
    session._finish_utterance(
        9600, "sure, it's zero five zero nine eight seven six five four three"
    )
    result = session._run_tool_sync(
        "find_or_create_customer",
        {"name": "Mr. Nasser", "mobile": "+971509876543"},
        "call_real",
    )
    assert "error" not in result
    assert db.query(Customer).filter_by(mobile="+971509876543").first() is not None

    # Lookup tools carry the same rule: a cancel by a number never heard in
    # THIS call is refused too (the number is someone's identity).
    other = session._run_tool_sync(
        "cancel_booking", {"mobile": "+971501112222"}, "call_other"
    )
    assert other["error"] == "mobile_not_heard"


def test_one_transcriber_slip_is_forgiven():
    """The model hears the audio natively; the evidence is the written
    transcript — and live (2026-07-31) they disagreed by ONE digit on a
    number the customer really said, refusing an honest customer. One
    dropped, added or misheard digit passes; a different number still
    fails by a mile."""
    heard = rt_module._spoken_digits("it's 0340 929 758")  # transcriber DROPPED a 7
    assert rt_module._tail_heard("923409297758", heard)    # the real number passes
    assert rt_module._tail_heard("03409297758", heard)     # local form too
    assert not rt_module._tail_heard("971509876543", heard)  # invention still dies
    assert not rt_module._tail_heard("0509876543", "")       # silence proves nothing
    # Substituted digit (misheard "six" for "seven") also forgiven.
    assert rt_module._tail_heard("0501234567", rt_module._spoken_digits("050 123 4568"))


def test_consent_grant_needs_an_answer(db, rt_env):
    """Rule 9 enforced on calls, both halves: a consent GRANT is refused
    until (a) the customer has spoken again since identification — an
    answer exists — AND (b) her own completed previous reply actually asked
    about WhatsApp — the question exists. Half (b) is the chat's 2026-07-28
    live defect kept out of the call: a yes to the recap can never be filed
    as messaging consent. A withdrawal is never blocked."""
    from unittest.mock import MagicMock

    from app.models import Customer

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]

    session._finish_utterance(9600, "my number is 050 111 2233, name is Omar")
    result = session._run_tool_sync(
        "find_or_create_customer",
        {"name": "Omar", "mobile": "+971501112233"},
        "call_id1",
    )
    assert "error" not in result
    session._note_outcome("find_or_create_customer", result)
    assert session.customer_id is not None

    # No utterance since identification: the grant has no answer behind it.
    refused = session._run_tool_sync(
        "record_consent", {"messaging": True}, "call_c1"
    )
    assert refused["error"] == "consent_not_heard"
    customer = db.get(Customer, session.customer_id)
    assert customer.whatsapp_consent is False

    # The customer spoke — but her last reply was the RECAP, not a consent
    # question. That yes is about the details; the grant is still refused.
    session.last_reply_text = "So that's the T2 at Deira, Saturday at seven — all correct?"
    session._finish_utterance(9600, "yes that's fine")
    recap_yes = session._run_tool_sync(
        "record_consent", {"messaging": True}, "call_c2"
    )
    assert recap_yes["error"] == "consent_question_not_asked"
    db.expire_all()
    assert db.get(Customer, session.customer_id).whatsapp_consent is False

    # She asks the consent question alone; the next yes records.
    session.last_reply_text = "Shall I send your confirmation on WhatsApp?"
    session._finish_utterance(9600, "yes please")
    granted = session._run_tool_sync(
        "record_consent", {"messaging": True}, "call_c3"
    )
    assert "error" not in granted
    db.expire_all()
    assert db.get(Customer, session.customer_id).whatsapp_consent is True

    # The Arabic question counts the same way.
    session.last_reply_text = "أرسل لك التأكيد على الواتساب؟"
    session._finish_utterance(9600, "تمام")
    assert "error" not in session._run_tool_sync(
        "record_consent", {"messaging": True}, "call_c4"
    )

    # A withdrawal is NEVER gated — refusing a customer's "no" is the
    # anti-customer direction.
    session.last_reply_text = ""
    withdrawn = session._run_tool_sync(
        "record_consent", {"messaging": False}, "call_c5"
    )
    assert "error" not in withdrawn


def test_silence_nudge_speaks_once_and_never_postpones_the_hangup(db, rt_env):
    """Rule 19's first rung, spoken (2026-08-01): after CALL_NUDGE_SECONDS of
    silence Aisha asks once whether the caller is still there. Exactly once
    per silence stretch, never while a goodbye is pending — and the nudge's
    own reply must NOT stamp last_activity, or it would postpone the silence
    hangup it precedes. The customer speaking re-arms the rung."""
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import MagicMock

    notes: list = []

    class Fake:
        async def cancel_response(self) -> None:
            pass

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            notes.append((note, tools_off))

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # Quiet past the rung: the nudge speaks, once — with TOOLS OFF, so
        # it cannot "let me check" its way back into the conversation (live
        # 2026-08-01: the nudge ran get_showrooms and answered a stale
        # question instead of asking if anyone was there).
        session.state = "listening"
        anchor = _time.monotonic() - (rt_module.CALL_NUDGE_SECONDS + 1)
        session.last_activity_mono = anchor
        session.speaking_until_mono = anchor  # she is not talking either
        session._nudge()
        await _asyncio.sleep(0)
        assert len(notes) == 1 and "still" in notes[0][0]
        assert notes[0][1] is True  # tools_off
        assert session._nudge_done is True

        # A muted caller gets the honest variant: we cannot hear you.
        muted = rt_module.RealtimeCallSession(MagicMock(), SESSION)
        muted._send_json = lambda _m: None  # type: ignore[method-assign]
        muted.session = Fake()
        muted.muted = True
        muted._nudge()
        await _asyncio.sleep(0)
        assert "hear" in notes[-1][0] and notes[-1][1] is True

        # Its reply finishing does NOT move the silence anchor.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "assistant_delta", "text": "Are you still with me?"})
        session._on_agent_event({"type": "response_done"})
        assert session.last_activity_mono == anchor

        # A bare VAD trigger (echo residue, a chair) does NOT re-arm the
        # rung — that once played the nudge twice in one silent stretch
        # (live 2026-08-01). Only a REAL turn re-arms it.
        session._on_agent_event({"type": "user_speech_started"})
        assert session._nudge_done is True
        assert session.last_activity_mono > anchor
        session._decide_turn(9600, "sorry, I'm here — yes the Deira showroom")
        await _asyncio.sleep(0)
        assert session._nudge_done is False  # real words re-arm
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "response_done"})
        assert session.last_activity_mono > anchor

    _asyncio.run(scenario())

    # The watchdog's gate: never while a goodbye is pending. (The nudge
    # condition in _watchdog_tick checks pending_close — asserted
    # structurally here since the loop itself sleeps in 1s ticks.)
    src = __import__("inspect").getsource(rt_module.RealtimeCallSession._watchdog_tick)
    assert "pending_close" in src and "_nudge_done" in src


def test_silence_hangup_says_goodbye_first(db, rt_env):
    """The 60s silence hangup speaks before it hangs up (2026-08-01): one
    warm goodbye — tools off, no question — then the drain ends the call as
    'silence'. The customer speaking mid-goodbye disarms it; a goodbye that
    never finishes is hard-stopped ~25s later."""
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import MagicMock

    requests: list = []

    class Fake:
        async def cancel_response(self) -> None:
            pass

        async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
            requests.append((note, tools_off))

    def fresh() -> object:
        s = rt_module.RealtimeCallSession(MagicMock(), SESSION)
        s._send_json = lambda _m: None  # type: ignore[method-assign]
        s.session = Fake()
        s.state = "listening"
        return s

    async def scenario() -> None:
        now = _time.monotonic()

        # Past the hangup rung: the goodbye is requested, once, tools off.
        session = fresh()
        session.last_activity_mono = now - (rt_module.CALL_SILENCE_HANGUP_SECONDS + 1)
        # Her voice finished then too: the ladder counts from whoever
        # spoke last, so a stale last_activity alone is not silence.
        session.speaking_until_mono = session.last_activity_mono
        session._watchdog_tick(now)
        await _asyncio.sleep(0)
        assert len(requests) == 1
        note, tools_off = requests[0]
        # An EXACT line (2026-08-01): briefs got improvised on, twice.
        assert "Say EXACTLY this" in note and "Goodbye" in note and tools_off is True
        assert session.pending_close is True and session.ended is False
        # A later tick does not re-request while the goodbye is in flight.
        session._watchdog_tick(now + 5)
        await _asyncio.sleep(0)
        assert len(requests) == 1

        # The goodbye finishes -> the drain ends the call as 'silence'.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "assistant_delta", "text": "Thank you for calling!"})
        session._on_agent_event({"type": "response_done"})
        for _ in range(20):
            await _asyncio.sleep(0.05)
            if session.ended:
                break
        assert session.ended is True and session.end_reason == "silence"

        # Speaking over the goodbye disarms it — the call continues.
        requests.clear()
        talker = fresh()
        talker.last_activity_mono = now - (rt_module.CALL_SILENCE_HANGUP_SECONDS + 1)
        # Her voice finished then too: the ladder counts from whoever
        # spoke last, so a stale last_activity alone is not silence.
        talker.speaking_until_mono = talker.last_activity_mono
        talker._watchdog_tick(now)
        await _asyncio.sleep(0)
        assert talker.pending_close is True
        talker._decide_turn(9600, "sorry — I'm still here, about that T2")
        await _asyncio.sleep(0)
        assert talker.pending_close is False
        assert talker._silence_farewell_at is None and talker.ended is False

        # A goodbye that never finishes is hard-stopped.
        stuck = fresh()
        stuck.last_activity_mono = now - (rt_module.CALL_SILENCE_HANGUP_SECONDS + 1)
        # Her voice finished then too: the ladder counts from whoever
        # spoke last, so a stale last_activity alone is not silence.
        stuck.speaking_until_mono = stuck.last_activity_mono
        stuck._watchdog_tick(now)
        await _asyncio.sleep(0)
        stuck._watchdog_tick(now + 26)
        assert stuck.ended is True and stuck.end_reason == "silence"

    _asyncio.run(scenario())


def test_speaking_over_the_farewell_cancels_the_hangup(db, rt_env):
    """The abrupt-end bug (live 2026-07-31): close_conversation armed the
    hangup, the customer said 'also, forget me' over the farewell, and the
    armed close cut the erasure conversation dead at the next reply's end.
    A REAL customer turn disarms the pending close; the drain task, if
    already counting down, aborts instead of firing; and a goodbye that
    STANDS still ends the call."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    class Fake:
        async def cancel_response(self) -> None:
            pass

        async def request_response(self, lang=None, *, note=None) -> None:
            pass

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # Goodbye: the model closes the conversation — hangup armed.
        session._note_outcome("close_conversation", {"closed": True})
        assert session.pending_close is True

        # The customer reopens it with real words: disarmed, call continues.
        session._decide_turn(9600, "wait, one more thing — forget me please")
        await _asyncio.sleep(0)
        assert session.pending_close is False
        assert session.ended is False

        # A drain already counting down aborts when the flag is withdrawn.
        session.pending_close = True
        task = _asyncio.get_running_loop().create_task(
            session._end_after_drain("closed")
        )
        await _asyncio.sleep(0)
        session.pending_close = False  # the customer spoke mid-drain
        await task
        assert session.ended is False

        # A goodbye that stands still hangs up.
        session.pending_close = True
        await session._end_after_drain("closed")
        assert session.ended is True and session.end_reason == "closed"

    _asyncio.run(scenario())


def test_rtc_takeover_waits_for_the_reply_boundary():
    """The two-voices bug (live 2026-07-31): ICE connected while the
    greeting was mid-flight on WS-PCM, the per-frame routing split the one
    reply across two transports, and the client's buffered head played OVER
    the rtc track's live tail. The takeover now waits for the reply
    boundary; with no reply in flight it is immediate; a transport death
    while deferred clears the pending flag."""
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]

    # Mid-reply: deferred, and the client is NOT told.
    session.audio_open = True
    session._rtc_up()
    assert session.rtc_pending is True and session.rtc_active is False
    assert not any(m["type"] == "rtc_active" for m in sent)

    # The reply ends — the takeover lands at the boundary.
    session._finish_response()
    assert session.rtc_active is True and session.rtc_pending is False
    assert any(m["type"] == "rtc_active" for m in sent)

    # No reply in flight: immediate, the original behaviour.
    fresh = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    fresh_sent: list[dict] = []
    fresh._send_json = fresh_sent.append  # type: ignore[method-assign]
    fresh._rtc_up()
    assert fresh.rtc_active is True
    assert any(m["type"] == "rtc_active" for m in fresh_sent)

    # A transport death while deferred clears the flag — WS carries on.
    dying = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    dying._send_json = lambda _m: None  # type: ignore[method-assign]
    dying.audio_open = True
    dying._rtc_up()
    dying._rtc_down()
    assert dying.rtc_pending is False and dying.rtc_active is False


def test_call_started_hands_the_browser_its_ice_servers(db, client, rt_env):
    """The browser cannot negotiate on host candidates alone: Chrome publishes
    them as mDNS `.local` names, which aiortc cannot resolve, so OUR side ends
    up with no usable address for it — measured 2026-08-04, when a proven-good
    public relay candidate on our side still never reached `connected`. The
    server therefore hands the client a STUN server in call_started, shaped for
    RTCConfiguration.iceServers so it passes straight through."""
    rt_env["monkeypatch"].setattr(
        rt_module, "client_ice_servers", lambda: [{"urls": "stun:relay.example.org:3478"}]
    )
    rt_env["install"]({1: REPLY_EVENTS})
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        started, _ = _drain_until(ws, "call_started")
        assert started["ice_servers"] == [{"urls": "stun:relay.example.org:3478"}]


def test_unconfigured_relay_sends_an_empty_ice_list(db, client, rt_env):
    """Localhost: both ends are the same machine, host candidates work, and
    dialling a STUN host that is not there would only add latency. The key is
    always PRESENT so the client never has to branch on undefined."""
    rt_env["install"]({1: REPLY_EVENTS})
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        started, _ = _drain_until(ws, "call_started")
        assert started["ice_servers"] == []


def test_greeting_waits_for_the_transport_to_settle(db, client, rt_env):
    """Dial-until-ready (2026-08-03, the doubled-mic fix): with the audio
    upgrade on the table the greeting WAITS — the client keeps its ring tone
    until call_ready — and mic audio sent while it waits is not part of the
    call, exactly as words spoken into a still-ringing phone are not. The
    wait is bounded: no rtc_offer inside the window falls back to WS-PCM,
    call_ready goes out, and the call proceeds as it was born."""
    rt_env["monkeypatch"].setattr(rt_module, "_RTC_OFFER_WAIT_S", 0.4)
    rt_env["monkeypatch"].setattr(rt_module, "_RTC_SETTLE_WAIT_S", 0.2)
    rt_env["install"]({1: REPLY_EVENTS})
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        started, _ = _drain_until(ws, "call_started")
        assert started["engine"] == "realtime" and started["rtc"] is True
        # Spoken over the ring: dropped, never answered — the fake's script
        # is keyed on the FIRST send_audio and it must not fire yet.
        ws.send_bytes(PCM_HALF_SECOND)
        _drain_until(ws, "call_ready")
        session = rt_env["session"]
        assert session.audio_calls == 0
        # After the pickup the same frame IS the call: script #1 fires and
        # her reply streams back over WS-PCM.
        ws.send_bytes(PCM_HALF_SECOND)
        _drain_until(ws, "pcm_start")
        assert session.audio_calls == 1
        assert session.greeted is True


def test_ws_mic_is_dropped_once_rtc_audio_flows():
    """The doubled-mic fix (2026-08-03): the first frame off the peer
    connection makes the WS uplink a DUPLICATE of the same microphone —
    during the takeover window the vendor used to hear every word twice,
    interleaved — so from that frame the WS copy dies at the door. A
    transport death hands the mic back to WS: a dead upgrade must never
    cost the call its ears."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    fake = FakeAgentSession(lambda _e: None, {})
    session.session = fake

    async def scenario():
        await session._on_bytes(b"\x01\x02" * 100)
        assert fake.audio_calls == 1  # WS carries the mic at birth
        await session._on_rtc_pcm(b"\x01\x02" * 100)
        assert fake.audio_calls == 2 and session._rtc_mic_live is True
        await session._on_bytes(b"\x01\x02" * 100)
        assert fake.audio_calls == 2  # the WS copy is now a duplicate — dropped
        session.rtc_active = True
        session._rtc_down()
        assert session._rtc_mic_live is False
        await session._on_bytes(b"\x01\x02" * 100)
        assert fake.audio_calls == 3  # transport died — WS is the mic again

    _asyncio.run(scenario())


def test_a_refused_supersede_recreate_is_reissued_when_the_old_reply_ends():
    """The mute-forever bug (2026-08-03): a supersede raises the stale-audio
    embargo and re-asks; when the re-ask loses the already-active race the
    vendor refuses it, no response_started ever comes, and the embargo —
    lifted only by that event — stayed up for the REST OF THE CALL: she kept
    generating, every audio frame died at the door, the customer heard
    nothing. Now the refusal is surfaced (response_rejected) and the gateway
    re-asks by itself the moment the blocking response finishes; the embargo
    stays up across the gap so none of the dead reply's tail plays, and
    lifts on the recreated reply's own response_started."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    fake = FakeAgentSession(lambda _e: None, {})
    session.session = fake

    async def scenario():
        # A reply is mid-flight; real words over it supersede it.
        session._on_agent_event({"type": "response_started"})
        session._respond(supersede=True)
        await _asyncio.sleep(0)  # let the cancel + re-ask task run
        assert fake.cancelled is True and fake.requested == 1
        assert session.discard_stale_audio is True

        # The vendor refuses the re-ask: the old response was still active.
        session._on_agent_event(
            {"type": "response_rejected",
             "code": "conversation_already_has_active_response"}
        )
        assert session._recreate_after_done is True
        # The dead reply's tail still dies at the door meanwhile.
        session._on_agent_event({"type": "audio", "data": b"\x01"})
        assert session.audio_open is False

        # The blocking response finally ends — the gateway asks again itself.
        session._on_agent_event({"type": "response_done"})
        await _asyncio.sleep(0)
        assert fake.requested == 2
        assert session.discard_stale_audio is True  # still up across the gap

        # The recreated reply begins: the embargo lifts, audio flows again.
        session._on_agent_event({"type": "response_started"})
        assert session.discard_stale_audio is False
        session._on_agent_event({"type": "audio", "data": b"\x02"})
        assert session.audio_open is True  # her voice reaches the wire again

    _asyncio.run(scenario())


def test_a_confirmation_over_her_recap_is_never_echo():
    """The swallowed-confirmation fix (2026-08-03): 'Yes, Saturday at seven
    at the showroom', confirmed over her recap, is built entirely of HER
    words — and the old any-order bag-of-words echo guard discarded it as
    echo at the climax of the booking. Two rules now narrow the match: a
    LEADING yes/no word is never echo, and echo must be a verbatim IN-ORDER
    fragment of one reply (a human confirming rearranges; echo cannot)."""
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session.reply_text = (
        "Perfect so that is Saturday at seven pm at the Deira showroom "
        "shall I confirm"
    )
    words = rt_module._normalize_words

    # A leading verdict word is a confirmation, never echo (rule a).
    assert session._is_reply_echo(words("Yes, Saturday at seven at the showroom")) is False
    # Her words REARRANGED are a human phrasing, never echo (rule b).
    assert session._is_reply_echo(words("Saturday the showroom at seven")) is False
    # A verbatim in-order fragment IS still echo — the guard's whole point.
    assert session._is_reply_echo(words("Saturday at seven pm at the Deira showroom")) is True
    # Echo with one transcriber-dropped word still matches in her order.
    assert session._is_reply_echo(words("Saturday at seven the showroom")) is True
    # The previous reply is still matched — playback outlives generation.
    session.last_reply_text, session.reply_text = session.reply_text, ""
    assert session._is_reply_echo(words("at the Deira showroom")) is True

    # Only a LEADING verdict bypasses: her own "no ..." echo must not slip
    # through on the strength of a CONTAINED no.
    session.reply_text = "I am afraid there is no availability that evening"
    session.last_reply_text = ""
    assert session._is_reply_echo(words("there is no availability")) is True
    assert session._is_reply_echo(words("no availability that evening")) is False


def test_a_stuck_embargo_expires_instead_of_muting_the_call():
    """The safety net behind the precise fix above: whatever ELSE ever eats
    the lifting response_started, the watchdog expires an embargo older than
    _EMBARGO_MAX_S — seconds of possibly-stale audio beat a call that stays
    mute to the ten-minute cap."""
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session._start_embargo()
    assert session.discard_stale_audio is True

    # Young embargo: untouched.
    session._watchdog_tick(session._embargo_since + 1.0)
    assert session.discard_stale_audio is True

    # Overdue: cleared, and the call is otherwise untouched.
    session._watchdog_tick(session._embargo_since + rt_module._EMBARGO_MAX_S + 0.1)
    assert session.discard_stale_audio is False
    assert session._embargo_since is None
    assert session.ended is False


def test_harvest_amounts_and_slot_ids():
    """The two new whitelist harvesters: amounts are digit-form >= 1000
    (commas and Eastern digits normalised); slot ids are every slot_id key,
    recursively."""
    assert rt_module._harvest_amounts('{"price_aed_min": 99000, "price_aed_max": 105000}') == {99000, 105000}
    assert rt_module._harvest_amounts("From AED 99,000 to AED 105,000") == {99000, 105000}
    assert rt_module._harvest_amounts("السعر ٩٩٬٠٠٠ درهم".replace("٬", ",")) == {99000}
    # Small numbers (slot ids, times) never enter the price whitelist.
    assert rt_module._harvest_amounts("slot 142 at 7pm on the 31st") == set()
    assert rt_module._harvest_slot_ids(
        {"slots": [{"slot_id": 7, "start": "x"}, {"slot_id": 9}], "slot_id": 3}
    ) == {7, 9, 3}
    assert rt_module._harvest_slot_ids({"booking": {"ref": "TD-2026-0001"}}) == set()


def test_slot_guard_refuses_a_slot_never_offered(db, rt_env, slots):
    """Slot provenance (2026-07-31): lock_slot/create_booking with a slot id
    no successful tool result ever named is refused before dispatch — a
    real-but-unoffered slot would book a time nobody discussed. A failure
    envelope must not launder the model's own argument into the whitelist."""
    from datetime import datetime
    from unittest.mock import MagicMock

    from app.models import Slot, SlotStatus

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]

    slot = (
        slots.query(Slot)
        .filter(Slot.status == SlotStatus.open, Slot.start > datetime.now())
        .first()
    )

    # Real slot, never offered on this call: refused, never dispatched.
    refused = session._run_tool_sync("lock_slot", {"slot_id": slot.id}, "call_s1")
    assert refused["error"] == "slot_not_offered"
    db.expire_all()
    assert db.get(Slot, slot.id).status == SlotStatus.open

    # create_booking is held to the same rule.
    assert session._fact_guard("create_booking", {"slot_id": slot.id, "vehicle_id": 1})[
        "error"
    ] == "slot_not_offered"

    # A failure envelope echoing the model's argument teaches nothing.
    session._note_outcome(
        "lock_slot", {"locked": False, "reason": "slot_not_found", "slot_id": 424242}
    )
    assert 424242 not in session.known_slot_ids

    # The slot arrives in a real availability result — now the lock passes.
    offered = session._run_tool_sync(
        "get_available_slots", {"vehicle_id": slot.vehicle_id}, "call_s2"
    )
    session._note_outcome("get_available_slots", offered)
    assert slot.id in session.known_slot_ids
    locked = session._run_tool_sync("lock_slot", {"slot_id": slot.id}, "call_s3")
    assert locked["locked"] is True


def test_price_guard_kills_an_invented_amount(rt_env):
    """Rule 2's price clause, conservative: a digit-form AED amount no tool
    result contained is killed mid-breath and redone with a corrective
    note; a tool-returned amount plays untouched; an amount still streaming
    is never judged from its prefix; one retry, then it plays."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append("create+note" if note else "create")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]
    session.session = Fake()
    session._note_outcome(
        "get_vehicle_details",
        {"vehicle": {"price_aed_min": 99000, "price_aed_max": 105000}},
    )

    async def scenario() -> None:
        session._respond(supersede=False)
        await _asyncio.sleep(0)
        calls.clear()

        # A tool-returned price plays untouched.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({
            "type": "assistant_delta",
            "text": "The T2 starts at AED 99,000 for the base trim. ",
        })
        await _asyncio.sleep(0)
        assert calls == []

        # A price still streaming is not judged from its prefix...
        session._on_agent_event({"type": "assistant_delta", "text": "Today it's AED 87,5"})
        await _asyncio.sleep(0)
        assert calls == []
        # ...but the completed invention dies, with the corrective note.
        session._on_agent_event({"type": "assistant_delta", "text": "00 all in."})
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create+note"]
        calls.clear()

        # The retry is spent: a persisting invention plays with a warning
        # rather than killing the line.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({
            "type": "assistant_delta",
            "text": "It would be AED 87,500 in total, shall we proceed?",
        })
        await _asyncio.sleep(0)
        assert calls == []

    _asyncio.run(scenario())


def test_call_outcomes_reach_the_chat_checkpoint(db, rt_env):
    """The split-memory narrowing (2026-07-31): a successful state-changing
    tool call on the CALL is written into the LangGraph checkpoint as a
    synthetic pair — so the customer can hang up, type 'move it to Sunday',
    and the chat agent already knows them. A refused call writes nothing,
    and forget_me is never in the whitelist (a pair written after erasure
    would resurrect the purged thread)."""
    from unittest.mock import MagicMock

    from app.agent import build_graph, new_sqlite_checkpointer

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _msg: None  # type: ignore[method-assign]

    def tool_uses() -> list[str]:
        graph = build_graph(new_sqlite_checkpointer(rt_module.AGENT_CHECKPOINT_PATH))
        state = graph.get_state({"configurable": {"thread_id": SESSION}})
        if not state or not state.values:
            return []
        return [
            b["name"]
            for m in state.values.get("messages", [])
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]

    # A refused (fabricated-mobile) call writes nothing.
    refused = session._run_tool_sync(
        "find_or_create_customer", {"name": "X", "mobile": "+971509999999"}, "cp_0"
    )
    assert refused["error"] == "mobile_not_heard"
    assert tool_uses() == []

    # A successful identification lands in the checkpoint.
    session._finish_utterance(9600, "my number is 050 111 2233, I'm Omar")
    result = session._run_tool_sync(
        "find_or_create_customer", {"name": "Omar", "mobile": "+971501112233"}, "cp_1"
    )
    assert "error" not in result
    assert tool_uses() == ["find_or_create_customer"]

    # Erasure must never be replayed into the store it just purged.
    assert "forget_me" not in rt_module._CHECKPOINT_TOOLS


def test_interrupted_greeting_does_not_regreet(db, rt_env):
    """The double-greeting bug (live 2026-07-31): speaking over the greeting
    cancels it, a cancelled response leaves nothing in the vendor thread, and
    the replacement reply greeted afresh. The FIRST reply's supersede now
    carries a one-shot don't-greet-again note; later supersedes carry none."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[tuple] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append(("cancel",))

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(("create", lang, note is not None))
            if note:
                assert "do NOT greet" in note

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # The caller speaks OVER the greeting (first exchange, reply live).
        session.response_active = True
        session.speech_over_reply = True
        session._decide_turn(9600, "hi there")
        await _asyncio.sleep(0)
        assert calls == [("cancel",), ("create", "en", True)]  # note rides
        calls.clear()

        # A guard REDO during the first exchange carries the note too — the
        # language guard's redo once regreeted exactly like the supersede
        # did (live 2026-08-01: greeting killed for language, replacement
        # greeted afresh).
        session.exchanges = 1
        session._kill_and_redo_reply()
        await _asyncio.sleep(0)
        assert calls == [("cancel",), ("create", "en", True)]
        calls.clear()

        # A later interruption is an ordinary supersede — no note.
        session.exchanges = 2
        session._on_agent_event({"type": "response_started"})
        session.speech_over_reply = True
        session._decide_turn(9600, "actually make it Sunday please")
        await _asyncio.sleep(0)
        assert calls == [("cancel",), ("create", "en", False)]

        # And a guard redo later in the call carries only its own note.
        session._kill_and_redo_reply()
        await _asyncio.sleep(0)
        assert calls[-1] == ("create", "en", False)

    _asyncio.run(scenario())


def test_one_word_interruptions_do_not_supersede(db, rt_env):
    """The substance rule (2026-08-03): a single word heard OVER her live
    reply — an echo fragment, a listener noise the backchannel list missed,
    a noise-burst hallucination — is heard and delivered but never cancels
    the reply (one call lost five replies to such supersedes). The same word
    while she is QUIET answers normally; a real sentence still barges in."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[tuple] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append(("cancel",))

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(("create", lang))

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()
    session.exchanges = 2  # past the greeting

    async def scenario() -> None:
        # One word over her live reply: delivered, not superseding.
        session.response_active = True
        session.speech_over_reply = True
        session._decide_turn(9600, "Stop.")
        await _asyncio.sleep(0)
        assert calls == []  # no cancel, no new response
        assert any(m.get("type") == "utterance" for m in sent)
        assert session.speech_over_reply is False  # playback resumed

        # A real sentence over her reply still barges in.
        session.speech_over_reply = True
        session._decide_turn(9600, "stop, I want a different day")
        await _asyncio.sleep(0)
        assert ("cancel",) in calls

        # And the same single word while she is quiet answers normally.
        calls.clear()
        session.response_active = False
        session.speech_over_reply = False
        session._decide_turn(9600, "Stop.")
        await _asyncio.sleep(0)
        assert ("create", "en") in calls

    _asyncio.run(scenario())


def test_farewell_drains_before_the_line_drops(db, rt_env):
    """Her goodbye was cut mid-word (live 2026-07-31): response_done means
    generation finished, but the rtc track still held seconds of farewell.
    The close now waits for the queue to drain (bounded) before ending.

    The tail is pinned SHORT here (2026-08-04): it became configurable and
    defaults to a second, which is a real network's jitter buffer — not
    something a unit test should sit through. The tail's VALUE is config; what
    this test owns is that the queue is waited on at all."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    rt_env["monkeypatch"].setattr(rt_module, "CALL_FAREWELL_TAIL_SECONDS", 0.05)

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.pending_close = True

    class Track:
        queued_ms = 800

        def pause(self) -> None: ...
        def resume(self) -> None: ...
        def clear(self) -> None: ...

    class Rtc:
        track = Track()

        async def close(self) -> None: ...

    session.rtc = Rtc()
    session.rtc_active = True

    async def scenario() -> None:
        session.reply_text = "Ma'a salama, we'll see you Saturday!"
        session._on_agent_event({"type": "response_done"})
        await _asyncio.sleep(0.15)
        assert session.ended is False  # farewell still playing — line held
        Rtc.track.queued_ms = 0        # ...the last frame leaves the speaker
        await _asyncio.sleep(0.6)
        assert session.ended is True
        assert session.end_reason == "closed"

    _asyncio.run(scenario())


def test_corrective_note_rides_one_response_only():
    """The reference guard's correction is a ONE-SHOT: it rides that redo's
    instructions alongside the language pin, and the next payload is clean —
    a persistent correction would nag every later reply about a mistake
    already fixed."""
    from app.services import realtime_agent as ra

    session = ra.RealtimeAgentSession(
        instructions="PERSONA", vocabulary="", on_event=lambda e: None
    )
    session._pinned_lang = "en"
    with_note = session._response_payload("CORRECTION: no such booking")
    assert "CORRECTION: no such booking" in with_note["response"]["instructions"]
    assert "English ONLY" in with_note["response"]["instructions"]
    # The next payload is not merely note-free — it is BARE (2026-08-01):
    # no response instructions exist for the model to read aloud.
    assert session._response_payload() == {"type": "response.create"}
    # Unpinned + noted still carries the persona with the note.
    session._pinned_lang = None
    unpinned = session._response_payload("CORRECTION: no such booking")
    assert unpinned["response"]["instructions"].startswith("PERSONA")


def test_cancel_names_its_target_response():
    """A cancel carries the id of the reply it means. A bare response.cancel
    kills 'the active response' — which, in the supersede sequence, can be the
    SUCCESSOR the cancel was making room for."""
    import asyncio as _asyncio

    from app.services import realtime_agent as ra

    session = ra.RealtimeAgentSession(
        instructions="x", vocabulary="", on_event=lambda e: None
    )
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    session._send = fake_send  # type: ignore[method-assign]
    session._active_response_id = "resp_A"
    _asyncio.run(session.cancel_response())
    assert sent == [{"type": "response.cancel", "response_id": "resp_A"}]

    # With no tracked id (nothing open), the cancel stays bare — the vendor
    # answers with the benign response_cancel_not_active.
    sent.clear()
    session._active_response_id = None
    _asyncio.run(session.cancel_response())
    assert sent == [{"type": "response.cancel"}]


def test_words_decide_turns_not_sounds(db, rt_env):
    """The turn engine (2026-07-30): VAD events pause and segment, but only
    the TRANSCRIPT decides. Noise resumes her reply; her own echoed words
    resume it silently; a backchannel over her speech resumes it (with the
    bubble kept); real words supersede — named cancel, then our re-ask —
    because with create_response false nobody else will answer the turn."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    def fresh_speech(session) -> None:
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 50_000  # what _on_pcm_24k would have counted
        session._on_agent_event({"type": "user_speech_stopped"})

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()
    session.response_active = True
    session.audio_open = True
    session.reply_text = "The Jetour T2 is a lovely petrol SUV with five seats"

    async def scenario() -> None:
        # 1. The hint alone touches nothing.
        session._on_agent_event({"type": "user_speech_started"})
        await _asyncio.sleep(0)
        assert calls == [] and session.audio_open is True

        # 2. A sub-perceptual blip commit: resumed, no reply drawn.
        session._on_agent_event({"type": "user_speech_stopped"})
        await _asyncio.sleep(0)
        assert calls == []

        # 3. Her own words back (echo past AEC): resumed, NO bubble — her
        #    words must never land in a customer bubble.
        fresh_speech(session)
        session._on_agent_event({"type": "user_transcript", "text": "the Jetour T2 petrol"})
        await _asyncio.sleep(0)
        assert calls == []
        assert all(m.get("type") != "utterance" for m in sent)

        # 4. A backchannel over her reply: bubble kept, reply NOT superseded.
        fresh_speech(session)
        session._on_agent_event({"type": "user_transcript", "text": "Yeah."})
        await _asyncio.sleep(0)
        assert calls == []
        assert any(m.get("type") == "utterance" for m in sent)
        assert all(m.get("type") != "stop_audio" for m in sent)

        # 5. Real words over her reply: supersede + respond.
        fresh_speech(session)
        session._on_agent_event(
            {"type": "user_transcript", "text": "actually wait, can we do Deira instead?"}
        )
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create:en"]
        assert {"type": "stop_audio"} in sent
        assert session.response_active is False

        # 6. A normal turn with nobody talking: respond, no cancel.
        calls.clear()
        session.speech_over_reply = False
        fresh_speech(session)
        session._on_agent_event({"type": "user_transcript", "text": "hi Aisha"})
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        assert calls == ["create:en"]  # the reply's language is PINNED, chat-style

    _asyncio.run(scenario())


def test_generation_phase_interruptions_cancel_and_stale_audio_dies(db, rt_env):
    """The she-finishes-the-old-reply-first bug (live 2026-07-30): a reply
    exists from response_started, seconds before its first audio — an
    interruption in that window must cancel it, and the cancelled reply's
    in-flight audio frames must never play. The embargo lifts when the NEXT
    reply begins."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    class FakeTrack:
        def __init__(self) -> None:
            self.played = b""
            self.queued_ms = 0

        def play(self, pcm: bytes) -> None:
            self.played += pcm

        def pause(self) -> None: ...
        def resume(self) -> None: ...
        def clear(self) -> None: ...

    class FakeRtc:
        def __init__(self) -> None:
            self.track = FakeTrack()

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda p: None  # type: ignore[method-assign]
    session.session = Fake()
    session.rtc = FakeRtc()
    session.rtc_active = True

    async def scenario() -> None:
        # A reply starts GENERATING — no audio yet.
        session._on_agent_event({"type": "response_started"})
        assert session.response_active is True
        # The customer interrupts inside the generation window.
        session._on_agent_event({"type": "user_speech_started"})
        assert session.speech_over_reply is True  # recognised without audio
        session.utterance_bytes = 50_000
        session._on_agent_event({"type": "user_speech_stopped"})
        session._on_agent_event(
            {"type": "user_transcript", "text": "wait, make it Saturday instead"}
        )
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        assert calls == ["cancel", "create:en"]  # cancelled DESPITE no audio yet
        # The cancelled reply's audio arrives late — it must die on arrival.
        session._on_agent_event({"type": "audio", "data": b"\x09\x09" * 100})
        assert session.rtc.track.played == b""
        # The replacement reply begins: embargo lifts, its audio plays.
        session._on_agent_event({"type": "response_started"})
        session._on_agent_event({"type": "audio", "data": b"\x07\x07" * 100})
        assert session.rtc.track.played == b"\x07\x07" * 100

    _asyncio.run(scenario())


def test_arabic_turns_are_first_class(db, rt_env):
    """The bilingual call (2026-07-30): an Arabic transcript gets lang=ar on
    its utterance event (RTL rendering, fallback voice), and an Arabic
    backchannel over her reply resumes playback instead of interrupting —
    exactly as its English twin does."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def cancel_response(self) -> None:
            calls.append("cancel")

        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    session._send_json = sent.append  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        # An ordinary Arabic turn: responded to, labelled ar.
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 50_000
        session._on_agent_event({"type": "user_speech_stopped"})
        session._on_agent_event(
            {"type": "user_transcript", "text": "أبغى أجرب سيارة جيتور تي تو"}
        )
        await _asyncio.sleep(0)
        utterances = [m for m in sent if m.get("type") == "utterance"]
        assert utterances and utterances[-1]["lang"] == "ar"
        assert calls == ["create:ar"]  # reply pinned to Arabic deterministically

        # An Arabic backchannel over her reply: she keeps talking.
        calls.clear()
        session.response_active = True
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 50_000
        session._on_agent_event({"type": "user_speech_stopped"})
        session._on_agent_event({"type": "user_transcript", "text": "تمام."})
        await _asyncio.sleep(0)
        assert calls == []  # not superseded, no reply drawn

    _asyncio.run(scenario())


def test_missing_transcript_still_gets_a_reply(db, rt_env, monkeypatch):
    """The model heard the audio natively — a dead transcriber costs the
    written record, never the reply."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    calls: list[str] = []

    class Fake:
        async def request_response(self, lang=None, *, note=None) -> None:
            calls.append(f"create:{lang}")

    monkeypatch.setattr(rt_module, "_TRANSCRIPT_FALLBACK_S", 0.01)
    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda p: None  # type: ignore[method-assign]
    session.session = Fake()

    async def scenario() -> None:
        session._on_agent_event({"type": "user_speech_started"})
        session.utterance_bytes = 50_000
        session._on_agent_event({"type": "user_speech_stopped"})
        await _asyncio.sleep(0.05)  # the fallback fires; no transcript ever came
        assert calls == ["create:en"]  # no transcript: the standing pin rides

    _asyncio.run(scenario())


def test_barge_in_cancels_the_vendor_response(db, client, rt_env):
    rt_env["install"]({
        1: HEARD_EVENTS + STOPPED_EVENTS + [
            {"type": "assistant_delta", "text": "Let me tell you all about"},
            {"type": "audio", "data": b"\x05\x06" * 600},
        ],
    })
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        ws.send_bytes(PCM_HALF_SECOND)
        _drain_until(ws, "pcm_start")
        ws.send_json({"type": "barge_in"})
        # The stream is closed to the client and the vendor told to stop.
        _drain_until(ws, "pcm_end")
        ws.send_json({"type": "hangup"})
    assert rt_env["session"].cancelled is True


def test_summary_row_anchors_the_bookings_the_call_made(db, rt_env, monkeypatch):
    """A call that booked something leaves its refs as an ordinary `cards`
    anchor on the summary row — the client rebuilds the booking card through
    the gated /api/booking, exactly as a typed booking's card does."""
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    # Result envelopes as booking.create/reschedule really shape them.
    session._note_outcome("create_booking", {"booking": {"booking_ref": "TD-2026-0001"}})
    session._note_outcome("reschedule_booking", {"booking": {"booking_ref": "TD-2026-0001"}})
    session._note_outcome("create_booking", {"error": "slot_unavailable"})  # no ref
    session._note_outcome("cancel_booking", {"booking": {"booking_ref": "TD-2026-0002"}})
    assert session.booking_refs == ["TD-2026-0001"]  # deduped; cancels never anchor

    session._store_summary_sync()
    db.expire_all()
    row = db.scalar(select(ConversationTurn).where(ConversationTurn.session_id == SESSION))
    assert row is not None and row.speaker is Speaker.system
    assert row.cards == [{"booking_refs": ["TD-2026-0001"]}]


def test_a_call_where_nothing_happened_leaves_nothing(db, rt_env):
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._store_summary_sync()
    db.expire_all()
    assert db.scalars(select(ConversationTurn)).all() == []


def test_natural_persona_is_one_config_flip_away(db, client, rt_env):
    """CALL_PERSONA=natural swaps the brief, the voice and the greeting —
    nothing else. The classic-path tests above prove the other half: their
    fakes accept only the original three kwargs, so a classic call passing
    persona extras would fail structurally, not silently."""
    from app.agent.call_persona import NATURAL_GREETING
    from app.agent.prompt import realtime_instructions

    rt_env["monkeypatch"].setattr(rt_module, "CALL_PERSONA", "natural")

    def factory_fn(*, instructions, vocabulary, on_event, **extras):
        rt_env["instructions"] = instructions
        rt_env["extras"] = extras
        rt_env["session"] = FakeAgentSession(on_event, {})
        return rt_env["session"]

    rt_env["monkeypatch"].setattr(rt_module, "make_agent_session", factory_fn)

    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        _drain_until(ws, "vad")
        ws.send_json({"type": "hangup"})
        _drain_until(ws, "call_ended")

    assert rt_env["extras"] == {
        "voice": rt_module.CALL_NATURAL_VOICE,
        "greeting": NATURAL_GREETING,
    }
    # A genuinely different brief — same load-bearing rules, different voice
    # direction — under the same TPM budget as the classic one.
    assert rt_env["instructions"] != realtime_instructions()
    assert "live voice call" in rt_env["instructions"].lower()
    assert "never a bot working through a script" in rt_env["instructions"]
    # PHONE LENGTH, still. The number guards the TPM budget — realtime
    # re-counts instructions on EVERY response — not a byte count for its own
    # sake, and this stays an order of magnitude under the 20k master prompt.
    # Raised from 6000 on 2026-08-03 to buy the answer-discipline paragraph,
    # after a live call where she chose a showroom the customer never named
    # and then read their Arabic greeting as consent to hold the slot.
    assert len(rt_env["instructions"]) < 6800
    assert rt_env["session"].greeted is True


def test_benign_vendor_races_never_kill_the_session():
    """The vendor read loop shrugs at the two RACE codes — a late cancel and a
    retry colliding with the VAD's own response — and still dies on real
    errors. Matched by EXACT code: a fuzzy substring ("active response", with
    a space) missed the underscored code and killed a live call, 2026-07-29."""
    import asyncio
    import json as _json

    from app.services.realtime_agent import RealtimeAgentSession

    def run(frames: list[dict]) -> list[dict]:
        events: list[dict] = []
        session = RealtimeAgentSession(
            instructions="", vocabulary="", on_event=events.append
        )

        class FakeWS:
            def __aiter__(self):
                async def gen():
                    for frame in frames:
                        yield _json.dumps(frame)

                return gen()

        session._ws = FakeWS()
        asyncio.run(session._read_loop())
        return events

    benign = run([
        {"type": "error", "error": {"code": "response_cancel_not_active"}},
        {"type": "error", "error": {"code": "conversation_already_has_active_response"}},
    ])
    # Both shrugged off; the loop ran to its natural end (agent_closed), and
    # no agent_error ever surfaced. REVERSED IN PART 2026-08-03 (the
    # mute-forever fix): the already-active refusal is no longer swallowed
    # SILENTLY — it surfaces as response_rejected so the gateway can re-ask
    # once the blocking response finishes (a refused supersede re-ask meant
    # no response_started ever came to lift the stale-audio embargo, and the
    # call stayed mute to the cap). Still benign: the session lives on and
    # a late cancel stays a pure shrug.
    assert [e["type"] for e in benign] == ["response_rejected", "agent_closed"]
    assert benign[0]["code"] == "conversation_already_has_active_response"

    fatal = run([{"type": "error", "error": {"code": "session_expired"}}])
    assert fatal == [{"type": "agent_error", "detail": "session_expired"}]


def test_rate_limit_retries_are_genuinely_bounded():
    """Three CONSECUTIVE rate-limit failures retry; the fourth ends the
    session as rate_limited. The budget resets only on a COMPLETED response —
    it used to reset on response.created, which the retry itself fires, so
    every failure was 'attempt 1/3' forever: an unbounded spiral whose own
    re-asks kept the TPM window saturated (live 2026-07-29)."""
    import asyncio
    import json as _json

    from app.services.realtime_agent import RealtimeAgentSession

    failed = {
        "type": "response.done",
        "response": {
            "status": "failed",
            "status_details": {
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Rate limit reached. Please try again in 0.001s.",
                },
            },
        },
    }
    completed = {"type": "response.done", "response": {"status": "completed"}}

    def run(frames: list[dict]) -> list[dict]:
        events: list[dict] = []
        session = RealtimeAgentSession(
            instructions="", vocabulary="", on_event=events.append
        )

        class FakeWS:
            def __aiter__(self):
                async def gen():
                    for frame in frames:
                        yield _json.dumps(frame)

                return gen()

            async def send(self, raw: str) -> None:  # the retry's response.create
                pass

            async def close(self) -> None:
                pass

        session._ws = FakeWS()
        asyncio.run(session._read_loop())
        return events

    # failure -> completed (budget RESETS) -> three failures retried silently
    # -> the fourth consecutive failure is the honest end.
    events = run([failed, completed, failed, failed, failed, failed])
    assert [e["type"] for e in events] == ["response_done", "agent_error"]
    assert events[-1]["detail"] == "rate_limited"


def test_pipeline_mode_is_untouched_by_the_switch(db, client, rt_env, monkeypatch):
    """Flipping back to pipeline must not touch the realtime path at all."""
    monkeypatch.setattr(call_module, "CALL_MODE", "pipeline")
    monkeypatch.setattr(call_module, "make_transcriber", lambda **kw: None)
    stt_calls = []
    monkeypatch.setattr(voice_service, "_client", lambda: (_ for _ in ()).throw(RuntimeError))
    with client.websocket_connect(f"/api/call/{SESSION}") as ws:
        assert ws.receive_json()["type"] == "call_started"
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        # Pipeline greeting: the client's detector owns turn-taking here.
        assert ws.receive_json() == {"type": "vad", "server": False}
        ws.send_json({"type": "hangup"})
        _drain_until(ws, "call_ended")
    assert "session" not in rt_env  # the realtime factory was never invoked
    assert stt_calls == []


def test_the_idle_ladder_waits_for_her_voice_to_finish_leaving(db, rt_env):
    """SHE GENERATES FASTER THAN SHE TALKS, and the ladder used to forget it.

    `response_done` means generated, not spoken: a long reply arrives in a few
    seconds and is then paced out at conversational speed. Anchored on
    generation end, the check-in landed inside her own sentence — measured
    live 2026-08-04, a 30-word reply logged at 08:03:28 drew "Are you still
    with me?" at 08:03:41.

    Here a 12-second reply is generated in one go and the clock is wound
    forward past the rung. Nothing may fire while that audio is still on its
    way out; the nudge lands CALL_NUDGE_SECONDS after it stops, not before.
    """
    from unittest.mock import MagicMock

    fired: list[str] = []
    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session._nudge = lambda: fired.append("nudge")  # type: ignore[method-assign]
    session._silence_goodbye = lambda _n: fired.append("goodbye")  # type: ignore[method-assign]

    # 12 seconds of speech, delivered as one burst of PCM (24 kHz, 16-bit mono).
    twelve_seconds = b"\x00\x00" * (rt_module.VENDOR_SAMPLE_RATE * 12)
    session._on_agent_event({"type": "audio", "data": twelve_seconds})
    session.response_active = False
    session.state = "listening"

    start = session.speaking_until_mono - 12.0

    # Mid-sentence, and well past the rung on the OLD anchor: still silent.
    session._watchdog_tick(start + rt_module.CALL_NUDGE_SECONDS + 1)
    assert fired == []

    # Her last word is out, but the customer's 10 seconds have not run yet.
    session._watchdog_tick(start + 12.0 + 1)
    assert fired == []

    # Ten seconds of real quiet after she stops — now she checks in.
    session._watchdog_tick(start + 12.0 + rt_module.CALL_NUDGE_SECONDS + 1)
    assert fired == ["nudge"]


def test_killed_audio_re_arms_the_ladder_immediately(db, rt_env):
    """A barge-in throws the unplayed remainder away, so the silence clock
    must start at the kill — not at the end of a reply nobody heard. Without
    this, interrupting a 15-second answer would leave the ladder asleep
    through 15 seconds of audible silence."""
    from unittest.mock import MagicMock

    fired: list[str] = []
    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session._nudge = lambda: fired.append("nudge")  # type: ignore[method-assign]
    session._silence_goodbye = lambda _n: fired.append("goodbye")  # type: ignore[method-assign]

    session._on_agent_event(
        {"type": "audio", "data": b"\x00\x00" * (rt_module.VENDOR_SAMPLE_RATE * 15)}
    )
    queued_until = session.speaking_until_mono

    session._silence_playback()          # the barge-in
    assert session.speaking_until_mono < queued_until

    session.response_active = False
    session.state = "listening"
    killed_at = session.speaking_until_mono
    session._watchdog_tick(killed_at + rt_module.CALL_NUDGE_SECONDS + 1)
    assert fired == ["nudge"]


def test_the_hangup_rung_shares_the_same_anchor(db, rt_env):
    """Both rungs move together, so the nudge still cannot postpone the
    hangup it precedes — and neither counts her own voice as silence."""
    from unittest.mock import MagicMock

    fired: list[str] = []
    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session._nudge = lambda: fired.append("nudge")  # type: ignore[method-assign]
    session._silence_goodbye = lambda _n: fired.append("goodbye")  # type: ignore[method-assign]

    session._on_agent_event(
        {"type": "audio", "data": b"\x00\x00" * (rt_module.VENDOR_SAMPLE_RATE * 8)}
    )
    session.response_active = False
    session.state = "listening"
    stops_at = session.speaking_until_mono

    # Past the hangup measured from GENERATION end (the old anchor), but only
    # 52s of real silence: the nudge is due by now and fires, the goodbye is
    # NOT — the 8 seconds she spent speaking are not the customer's silence.
    session._watchdog_tick(stops_at - 8.0 + rt_module.CALL_SILENCE_HANGUP_SECONDS + 1)
    assert "goodbye" not in fired

    # Only once the full hangup window has passed since she STOPPED.
    session._watchdog_tick(stops_at + rt_module.CALL_SILENCE_HANGUP_SECONDS + 1)
    assert "goodbye" in fired


# --------------------------------------------------------------------------- #
# Truncation — the correction that makes an interruption honest (2026-08-04)
# --------------------------------------------------------------------------- #
class _TruncSession:
    """A vendor session that records the corrections it is sent."""

    def __init__(self) -> None:
        self.truncated: list[int] = []
        self.item_id: str | None = "item_abc"

    async def cancel_response(self) -> None:
        pass

    async def request_response(self, lang=None, *, note=None, tools_off=False) -> None:
        pass

    async def truncate_reply(self, audio_end_ms: int) -> bool:
        if not self.item_id or audio_end_ms <= 0:
            return False
        self.item_id = None
        self.truncated.append(audio_end_ms)
        return True


def _speaking_session(rtc_queued_ms: int | None = None):
    """A session mid-reply, with a known amount of her audio already fed."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    vendor = _TruncSession()
    session.session = vendor
    if rtc_queued_ms is not None:
        track = MagicMock()
        track.queued_ms = rtc_queued_ms
        rtc = MagicMock()
        rtc.track = track
        session.rtc = rtc
        session.rtc_active = True
    return session, vendor, _asyncio


def test_an_interrupted_reply_is_truncated_at_what_was_heard(db, rt_env):
    """THE BUG THIS FIXES (measured on the dev VM, 2026-08-04): 68 of 106 turns
    superseded a live reply and exactly ONE reached response.cancel — she
    generates far faster than she speaks, so by transcript time generation had
    finished and only her AUDIO was still playing. We silenced that audio and
    told the vendor nothing, so the model believed the customer had heard the
    whole reply and reasoned from a conversation that never happened."""
    session, vendor, _asyncio = _speaking_session(rtc_queued_ms=6000)
    # 10 seconds generated, 6 still queued -> 4 seconds actually heard.
    session._reply_fed_ms = 10_000.0

    async def scenario() -> None:
        session._silence_playback()
        await _asyncio.sleep(0)

    _asyncio.run(scenario())
    assert vendor.truncated == [4000]


def test_nothing_heard_means_nothing_to_correct(db, rt_env):
    """A reply cut off before a single word left the speaker needs no
    correction — the customer heard nothing, so the model saying nothing is
    already the truth. Sending audio_end_ms=0 would be a vendor error."""
    session, vendor, _asyncio = _speaking_session(rtc_queued_ms=9000)
    session._reply_fed_ms = 9000.0  # everything generated, none played

    async def scenario() -> None:
        session._silence_playback()
        await _asyncio.sleep(0)

    _asyncio.run(scenario())
    assert vendor.truncated == []


def test_the_correction_can_never_claim_more_than_was_generated(db, rt_env):
    """`audio_end_ms` above the item's real duration is the one value the vendor
    REJECTS. The count comes from bytes the vendor itself sent, so it cannot
    exceed by arithmetic — and a queue reporting nonsense cannot push it over
    either: the clamp turns that into 'all of it was heard', which sends no
    correction at all. Wrong in the harmless direction, by construction."""
    session, vendor, _asyncio = _speaking_session(rtc_queued_ms=-500)
    session._reply_fed_ms = 3000.0
    assert session._played_ms() <= session._reply_fed_ms

    async def scenario() -> None:
        session._silence_playback()
        await _asyncio.sleep(0)

    _asyncio.run(scenario())
    assert vendor.truncated == []


def test_a_new_reply_resets_the_count_only_when_its_item_arrives(db, rt_env):
    """`response_started` is TOO EARLY to forget the byte count: at that moment
    the reply being silenced is still the PREVIOUS one, and it is that one the
    vendor must be told about. The count is spent when the new reply's own
    item is announced."""
    session, _vendor, _asyncio = _speaking_session(rtc_queued_ms=0)
    session._reply_fed_ms = 5000.0
    session._on_agent_event({"type": "response_started"})
    assert session._reply_fed_ms == 5000.0
    session._on_agent_event({"type": "reply_item", "item_id": "item_next"})
    assert session._reply_fed_ms == 0.0


def test_audio_advances_the_count_at_the_vendors_own_rate(db, rt_env):
    """48 bytes per millisecond — 24 kHz, 16-bit, mono."""
    session, _vendor, _asyncio = _speaking_session(rtc_queued_ms=0)
    session._on_agent_event({"type": "audio", "data": b"\x00" * 4800})
    assert session._reply_fed_ms == 100.0


def test_one_word_now_interrupts_her_on_the_webrtc_path(db, rt_env):
    """WHAT THE CUSTOMER ASKED FOR (2026-08-04): saying "hmm" or "ok ok" over a
    car description means STOP, exactly as it would to a person. The gates that
    refused short words existed because interrupting CORRUPTED her memory;
    truncation removed that cost, so on the path that has both an honest
    truncation point and AEC3, the model decides what a short word means."""
    session, _vendor, _asyncio = _speaking_session(rtc_queued_ms=4000)
    session._reply_fed_ms = 8000.0
    session.speech_over_reply = True
    superseded: list[bool] = []
    session._respond = lambda *, supersede: superseded.append(supersede)  # type: ignore[method-assign]

    session._decide_turn(9600, "hmm")
    assert superseded == [True]


def test_the_fallback_path_still_refuses_short_words(db, rt_env):
    """WS-PCM keeps the old exposure and therefore the old gates: playback is
    the CLIENT'S there, so the truncation point is an estimate rather than a
    fact, and without AEC3 her own voice still reaches the microphone."""
    session, _vendor, _asyncio = _speaking_session()  # no rtc -> fallback
    session.speech_over_reply = True
    superseded: list[bool] = []
    session._respond = lambda *, supersede: superseded.append(supersede)  # type: ignore[method-assign]

    session._decide_turn(9600, "hmm")
    assert superseded == []


def test_a_reply_that_finished_playing_is_not_corrected(db, rt_env):
    """She said all of it and the customer heard all of it — there is nothing
    to take back. _silence_playback runs on every new turn, so without this the
    previous, complete reply would be 'truncated' at its own full length every
    time: a pointless round trip one rounding error away from the single
    audio_end_ms value the vendor rejects."""
    session, vendor, _asyncio = _speaking_session(rtc_queued_ms=0)
    session._reply_fed_ms = 7000.0  # generated 7s, none left queued

    async def scenario() -> None:
        session._silence_playback()
        await _asyncio.sleep(0)

    _asyncio.run(scenario())
    assert vendor.truncated == []


# --------------------------------------------------------------------------- #
# The goodbye cutoff (2026-08-04)
# --------------------------------------------------------------------------- #
def _farewell_session(*, rtc: bool):
    """A session that has said goodbye and armed the hangup, with the customer
    speaking OVER the farewell — which is the whole situation under test.

    `_respond` is stubbed rather than run: it schedules a task on a live loop,
    and what these tests own is the DECISION (does she get cut off, does the
    hangup survive), not the reply that may follow it."""
    from unittest.mock import MagicMock

    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.session = _TruncSession()
    session.pending_close = True
    session.response_active = True    # her farewell is still playing
    session.speech_over_reply = True  # ...and they are talking across it
    session.rtc_active = rtc
    session.responded = []
    session._respond = lambda **kw: session.responded.append(kw)  # type: ignore[method-assign]
    return session


def test_a_short_word_over_her_goodbye_does_not_cut_it(db, rt_env):
    """⚠️ THE REPORTED BUG. On WebRTC a single word supersedes — deliberate, and
    what finally let "hmm" cut her off mid-conversation. At the FAREWELL it was
    wrong: "thanks, bye" over "thank you for calling" is two people finishing
    the same exchange, and ungated it killed her goodbye mid-word AND cancelled
    the hangup, so the call limped on after she had already said goodbye."""
    session = _farewell_session(rtc=True)
    session._decide_turn(rt_module._MIN_UTTERANCE_BYTES, "Bye.")

    assert session.pending_close is True   # the hangup survives
    assert session.responded == []          # she was never superseded


def test_a_real_sentence_over_her_goodbye_still_reopens_the_call(db, rt_env):
    """The 2026-07-31 behaviour is untouched: "also, forget me" spoken over the
    farewell must reopen the conversation. Only SHORT noises are held back."""
    session = _farewell_session(rtc=True)
    session._decide_turn(rt_module._MIN_UTTERANCE_BYTES, "Actually, wait, I also wanted to cancel it.")

    assert session.pending_close is False
    assert session.responded == [{"supersede": True}]


def test_a_gated_word_does_not_cancel_the_hangup_on_its_way_past(db, rt_env):
    """The disarm used to sit BETWEEN the two gates, so an utterance the
    min-words gate was about to discard had already cancelled the close. She
    had said goodbye, nothing replaced it, and the line sat open until the
    silence ladder fired a minute later."""
    session = _farewell_session(rtc=False)  # gates armed on the fallback anyway
    # NOT a backchannel — that gate returns BEFORE the disarm under either
    # ordering, so a "hmm" here would pass whatever the order is. A lone
    # stray word is what actually reaches the min-words gate, and it is also
    # the realistic shape: a noise-burst hallucination or an echo fragment.
    session._decide_turn(rt_module._MIN_UTTERANCE_BYTES, "Saturday")

    assert session.pending_close is True
    assert session.responded == []


def test_the_farewell_drains_on_the_ws_fallback_too(db, rt_env):
    """It used to consult ONLY the rtc queue, so with no peer connection
    `track is None` broke the loop on its first pass and the line dropped
    mid-goodbye every time on the fallback transport. speaking_until_mono is
    the transport-agnostic answer and _played_ms already uses it for exactly
    this case."""
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import MagicMock

    rt_env["monkeypatch"].setattr(rt_module, "CALL_FAREWELL_TAIL_SECONDS", 0.05)
    session = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    session._send_json = lambda _m: None  # type: ignore[method-assign]
    session.pending_close = True
    assert session.rtc is None  # the WS-PCM fallback

    async def scenario() -> None:
        # Half a second of her farewell still to be heard by the client.
        session.speaking_until_mono = _time.monotonic() + 0.5
        task = _asyncio.get_running_loop().create_task(
            session._end_after_drain("closed")
        )
        await _asyncio.sleep(0.2)
        assert session.ended is False   # would have dropped instantly before
        await _asyncio.sleep(0.6)
        assert session.ended is True
        await task

    _asyncio.run(scenario())
