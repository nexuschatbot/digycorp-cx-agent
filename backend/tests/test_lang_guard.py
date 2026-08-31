"""The rule 3 script guard — _ScriptWindow and _default_generate's retry loop.

The prompt's language anchor is an instruction and can lose to conversation
momentum (seen live 2026-07-27: a bare English "no" mid-Arabic-conversation drew
an Arabic reply through two anchor hardenings). The guard is the enforcement:
the reply's opening is held until its script is known, a mismatch is discarded
unseen and retried once. These tests drive the window as a pure class and the
retry loop against a FAKE Anthropic client — fully offline, per the no-live-
vendor-calls rule.
"""

from types import SimpleNamespace

import app.agent.graph as graph
from app.agent.graph import _ScriptWindow


# --- the window as a pure class --------------------------------------------

def _feed_all(window, text, chunk=7):
    """Feed text in small delta-sized chunks, returning everything released."""
    out = []
    for i in range(0, len(text), chunk):
        out.append(window.feed(text[i:i + chunk]))
    return "".join(out)


def test_english_reply_under_english_directive_streams_intact():
    w = _ScriptWindow("en")
    text = "No problem at all! What is it that you're looking for today?"
    released = _feed_all(w, text) + w.flush()
    assert released == text
    assert not w.violated


def test_arabic_opening_under_english_directive_is_withheld():
    w = _ScriptWindow("en")
    released = _feed_all(w, "عندنا خيارات ممتازة في هذا النطاق!")
    assert released == ""
    assert w.violated


def test_first_arabic_char_decides_immediately():
    # The violation must land on the first delta carrying Arabic — before the
    # window fills — so the abort costs as few generated tokens as possible.
    w = _ScriptWindow("en")
    assert w.feed("عن") == ""
    assert w.violated


def test_arabic_reply_opening_with_model_name_passes_arabic_directive():
    # "Jetour X50 تبدأ من..." is an Arabic sentence; Latin letters alone must
    # not condemn it while Arabic arrives within the window.
    w = _ScriptWindow("ar")
    text = "Jetour X50 تبدأ من 73,000 درهم وهي الأوسع مساحة."
    released = _feed_all(w, text) + w.flush()
    assert released == text
    assert not w.violated


def test_english_reply_under_arabic_directive_is_withheld():
    w = _ScriptWindow("ar")
    released = _feed_all(w, "Sure! What is the maximum budget you have in mind?")
    assert released == ""
    assert w.violated


def test_short_reply_decides_at_flush():
    # "Ok!" never fills the window; flush() must settle it on what it holds.
    w = _ScriptWindow("en")
    assert w.feed("Ok!") == ""
    assert w.flush() == "Ok!"
    assert not w.violated

    w = _ScriptWindow("ar")
    assert w.feed("Ok!") == ""
    assert w.flush() == ""
    assert w.violated


def test_script_neutral_text_violates_nothing():
    # Digits and punctuation belong to no script; either directive passes them.
    for expected in ("en", "ar"):
        w = _ScriptWindow(expected)
        assert w.feed("12345") == ""
        assert w.flush() == "12345"
        assert not w.violated


def test_no_expectation_is_transparent():
    w = _ScriptWindow(None)
    assert w.feed("أي نص on any script") == "أي نص on any script"
    assert not w.violated
    w = _ScriptWindow("mixed")  # only en/ar arm the guard
    assert w.feed("anything") == "anything"


def test_after_decision_text_passes_straight_through():
    # Mid-reply script mixing (names, refs) is legitimate — the guard only ever
    # judges the opening.
    w = _ScriptWindow("en")
    _feed_all(w, "Here are three options within your budget: ")
    assert w.feed("الـ Jetour X50") == "الـ Jetour X50"
    assert not w.violated


def test_observe_mode_releases_immediately_but_still_records():
    # The retry attempt streams unconditionally; `violated` is only for the log.
    w = _ScriptWindow("en", enforce=False)
    text = "عندنا خيارات ممتازة"
    assert w.feed(text) == text
    assert w.violated


# --- the retry loop, against a fake client ---------------------------------

class _FakeStream:
    """Stands in for the SDK's MessageStream context manager."""

    def __init__(self, events, stop_reason="end_turn"):
        self._events = events
        self.closed = False
        self._stop = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="", citations=None)],
            stop_reason=self._stop,
            usage=None,
        )


class _FakeClient:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = []  # kwargs of every messages.stream(...) call
        fake = self

        class _Messages:
            def stream(self, **kwargs):
                fake.calls.append(kwargs)
                return fake._streams.pop(0)

        self.messages = _Messages()


def _text_events(*chunks):
    return [SimpleNamespace(type="text", text=c) for c in chunks]


def _run_generate(monkeypatch, streams, expected_lang):
    client = _FakeClient(streams)
    emitted = []
    monkeypatch.setattr(graph, "_client", client)
    monkeypatch.setattr(graph, "_safe_writer", lambda: emitted.append)
    result = graph._default_generate(
        [{"role": "user", "content": "hi"}],
        model="test-model",
        system="SYSTEM",
        tools=[],
        config={"configurable": {"session_id": "s-test", "expected_lang": expected_lang}},
    )
    return client, emitted, result


def test_wrong_opening_is_discarded_and_retried_once(monkeypatch):
    wrong = _FakeStream(_text_events("عندنا خيارات ", "ممتازة!"))
    right = _FakeStream(_text_events("We have great ", "options for you today, sir!"))
    client, emitted, result = _run_generate(monkeypatch, [wrong, right], "en")

    # Two calls: the discarded attempt and the retry.
    assert len(client.calls) == 2
    # Aborting the first attempt closed its HTTP stream (stops generation server-side).
    assert wrong.closed
    # Not one Arabic character reached the writer.
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "We have great options for you today, sir!"
    # The corrective rides INSIDE the last user message on the retry — after the
    # history, where it is the last thing read before generating (a system-block
    # corrective was defied live: it renders before the Arabic history and loses
    # to momentum). The system blocks stay byte-identical, preserving the cache.
    assert client.calls[0]["system"] == client.calls[1]["system"]
    retry_last = client.calls[1]["messages"][-1]
    assert retry_last["role"] == "user"
    note = retry_last["content"][-1]["text"]
    assert "discarded" in note and "English" in note
    assert note.startswith("[System note")
    # The original words are preserved ahead of the note (first call sends one
    # block; the retry sends the same block plus the note behind it).
    assert retry_last["content"][0]["text"] == "hi"
    first_last = client.calls[0]["messages"][-1]
    assert [b["text"] for b in first_last["content"]] == ["hi"]
    assert result["role"] == "assistant"


def test_right_opening_streams_on_the_first_attempt(monkeypatch):
    ok = _FakeStream(_text_events("Happy to help! ", "Which showroom suits you?"))
    client, emitted, _ = _run_generate(monkeypatch, [ok], "en")
    assert len(client.calls) == 1
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "Happy to help! Which showroom suits you?"


def test_wrong_pretool_narration_retries_before_the_tool_runs(monkeypatch):
    # Wrong-language narration followed by a tool_use must abort at the seal —
    # the window cannot fill further — and nothing must have been emitted.
    tool_start = SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", id="tu_1", name="search_vehicles"),
    )
    wrong = _FakeStream(_text_events("حسناً، ") + [tool_start], stop_reason="tool_use")
    right = _FakeStream(_text_events("Let me check that for you."))
    client, emitted, _ = _run_generate(monkeypatch, [wrong, right], "en")
    assert len(client.calls) == 2
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "Let me check that for you."


def test_no_expectation_never_retries(monkeypatch):
    # A mixed/kickoff/voice-failed turn arms nothing: one call, streamed as-is.
    ok = _FakeStream(_text_events("أهلاً! كيف أقدر أساعدك؟"))
    client, emitted, _ = _run_generate(monkeypatch, [ok], None)
    assert len(client.calls) == 1
    streamed = "".join(e["text"] for e in emitted if e.get("type") == "token")
    assert streamed == "أهلاً! كيف أقدر أساعدك؟"


def test_effort_rides_every_model_call(monkeypatch):
    # AGENT_EFFORT (config, default "medium") must reach the API on every call —
    # unset, this model silently runs at "high", the slowest setting. Both the
    # first attempt and a guard retry carry it.
    monkeypatch.setattr(graph, "AGENT_EFFORT", "medium")
    wrong = _FakeStream(_text_events("عندنا خيارات"))
    right = _FakeStream(_text_events("We have great options."))
    client, _, _ = _run_generate(monkeypatch, [wrong, right], "en")
    assert len(client.calls) == 2
    for call in client.calls:
        assert call["output_config"] == {"effort": "medium"}


def test_empty_effort_omits_the_parameter(monkeypatch):
    # AGENT_EFFORT="" is the escape hatch back to the provider default: the
    # parameter must be absent entirely, not sent as an empty value.
    monkeypatch.setattr(graph, "AGENT_EFFORT", "")
    ok = _FakeStream(_text_events("Happy to help!"))
    client, _, _ = _run_generate(monkeypatch, [ok], "en")
    assert "output_config" not in client.calls[0]


# --- the momentum predictor and the proactive note --------------------------

from app.agent.graph import _momentum_mismatch  # noqa: E402  (imported here, under its own section heading)


def test_momentum_mismatch_detects_a_switch_to_english():
    messages = [
        {"role": "user", "content": "ما هي سيارتكم الرائدة؟"},
        {"role": "assistant", "content": "السيارة الرائدة هي Jetour G700."},
        {"role": "user", "content": "no i want cheaper"},
    ]
    assert _momentum_mismatch(messages, "en")


def test_momentum_mismatch_detects_a_switch_to_arabic():
    messages = [
        {"role": "user", "content": "tell me about the T2"},
        {"role": "assistant", "content": "The T2 is a mid-size off-road SUV."},
        {"role": "user", "content": "ما سعرها؟"},
    ]
    assert _momentum_mismatch(messages, "ar")


def test_no_mismatch_when_momentum_agrees():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Welcome to Elite! How can I help?"},
        {"role": "user", "content": "show me options"},
    ]
    assert not _momentum_mismatch(messages, "en")


def test_no_mismatch_on_a_fresh_conversation():
    # No assistant text yet — there is no momentum to fight.
    assert not _momentum_mismatch([{"role": "user", "content": "hello"}], "en")
    assert not _momentum_mismatch([], "en")


def test_tool_use_only_assistant_turns_are_skipped():
    # The LAST assistant message may be a tool_use carrier with no text; the
    # predictor must read the last assistant message that actually SPOKE.
    messages = [
        {"role": "assistant", "content": "عندنا خيارات ممتازة!"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
    ]
    assert _momentum_mismatch(messages, "en")


def test_visible_switch_injects_the_note_on_the_first_attempt(monkeypatch):
    # The whole point of the predictor: when the switch is visible up front, the
    # note rides attempt one, the model complies, and there is NO second call.
    ok = _FakeStream(_text_events("Happy to help! What is your budget?"))
    client = _FakeClient([ok])
    emitted = []
    monkeypatch.setattr(graph, "_client", client)
    monkeypatch.setattr(graph, "_safe_writer", lambda: emitted.append)
    graph._default_generate(
        [
            {"role": "user", "content": "أبغى سيارة"},
            {"role": "assistant", "content": "أهلاً وسهلاً! أي نوع تفضل؟"},
            {"role": "user", "content": "no"},
        ],
        model="test-model",
        system="SYSTEM",
        tools=[],
        config={"configurable": {"session_id": "s-test", "expected_lang": "en"}},
    )
    assert len(client.calls) == 1
    last = client.calls[0]["messages"][-1]
    assert last["content"][0]["text"] == "no"
    note = last["content"][-1]["text"]
    assert note.startswith("[System note") and "switched language" in note and "English" in note
