"""Read-aloud — services/speech.py (2026-07-21).

NO LIVE VENDOR CALLS. Every synthesis here is monkeypatched. The one thing a
live call would prove — whether OpenAI's Arabic is good enough to ship — was
settled by ear in the listening spike and recorded in CLAUDE.md; asking CI to
re-answer it would burn money on a question about a vendor, not about our code.

WHAT IS NOT TESTED HERE: audio quality, and the voice mapping sounding right.
Those are judgements. What IS tested is everything that decides WHETHER a
customer's data leaves the country and whether it comes back: the turn lookup,
the session gate, the cache, retention, and erasure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditEvent,
    AuditType,
    ConversationTurn,
    Language,
    Speaker,
    SpeechClip,
)
from app.services import speech as speech_service

SESSION = "sess-speech-1"
OTHER = "sess-speech-2"
AUDIO = b"ID3fake-mp3-bytes"


class _FakeSpeech:
    def __init__(self, data=AUDIO, raises=None):
        self.data, self.raises, self.calls = data, raises, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return type("R", (), {"content": self.data})()


def _fake(monkeypatch, data=AUDIO, raises=None) -> _FakeSpeech:
    fake = _FakeSpeech(data, raises)
    client = type("C", (), {"audio": type("A", (), {"speech": fake})()})()
    monkeypatch.setattr(speech_service, "_client", lambda: client)
    monkeypatch.setattr(speech_service, "OPENAI_API_KEY", "test-key")
    return fake


def _turn(db, text="Your booking reference is TD-2026-0142.", *,
          speaker=Speaker.agent, lang=Language.en, session=SESSION) -> int:
    t = ConversationTurn(session_id=session, speaker=speaker, lang=lang, text=text)
    db.add(t)
    db.flush()
    return t.id


# --------------------------------------------------------------------------- #
# Normalisation — the Python twin of frontend/src/speech-text.ts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Your reference is TD-2026-0142.", "Your reference is T D, 2026, 0 1 4 2."),
        ("حجزك برقم TD-2026-0142 مؤكد.", "حجزك برقم T D, 2026, 0 1 4 2 مؤكد."),
        ("We'll text +971505550103.", "We'll text +971, 50, 555, 01 03."),
        ("Saturday at 7:00 pm.", "Saturday at 7 pm."),
        # The bilingual trap: JavaScript's \b is ASCII-only, so the TS twin
        # silently skipped every Arabic time until a lookahead replaced it. Pinned
        # in BOTH languages here for the same reason.
        ("الساعة 7:00 م في ديرة.", "الساعة 7 م في ديرة."),
        ("Saturday at 7:30 pm.", "Saturday at 7:30 pm."),
        ("The **Jetour T2** is available.", "The Jetour T2 is available."),
        ("Nothing special at all.", "Nothing special at all."),
    ],
)
def test_normalisation_matches_the_frontend_rules(raw, expected):
    """Same cases as scripts/speech_harness.ts. Two implementations exist because
    the server holds the text and the browser fallback does not — so they are
    pinned to one set of expectations rather than trusted to stay aligned."""
    assert speech_service.for_speech(raw) == expected


# --------------------------------------------------------------------------- #
# The turn lookup IS the security boundary
# --------------------------------------------------------------------------- #
def test_only_this_session_can_have_its_turns_read(db, monkeypatch):
    """A turn from someone else's conversation is simply NOT FOUND, so a caller
    guessing ids learns nothing and can never have another customer's reply —
    which quotes their name and mobile — read out."""
    _fake(monkeypatch)
    tid = _turn(db, session=OTHER)

    out = speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    assert out == {"ok": False, "reason": "turn_not_found"}
    assert db.scalar(select(func.count(SpeechClip.id))) == 0


def test_a_customer_turn_is_never_read_aloud(db, monkeypatch):
    """Only Aisha is spoken. The customer knows what they said, and their own
    voice note is already playable where they left it."""
    _fake(monkeypatch)
    tid = _turn(db, "I'd like the T2 on Saturday", speaker=Speaker.customer)

    assert speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=tid
    )["reason"] == "not_an_agent_turn"


def test_an_unknown_turn_is_not_found(db, monkeypatch):
    _fake(monkeypatch)
    assert speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=999_999
    )["reason"] == "turn_not_found"


def test_an_empty_turn_synthesises_nothing(db, monkeypatch):
    """A failed voice note leaves an empty agent-adjacent turn; there is no
    silence worth a vendor call."""
    _fake(monkeypatch)
    tid = _turn(db, "   ")
    assert speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=tid
    )["reason"] == "empty_turn"


def test_absurdly_long_text_is_refused(db, monkeypatch):
    _fake(monkeypatch)
    tid = _turn(db, "x" * 50_000)
    assert speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=tid
    )["reason"] == "text_too_long"


# --------------------------------------------------------------------------- #
# What actually reaches the vendor
# --------------------------------------------------------------------------- #
def test_the_vendor_receives_normalised_text_not_the_raw_turn(db, monkeypatch):
    """The bubble keeps its own text; only the SPOKEN string is rewritten, so
    what is read and what is on screen stay the same message."""
    fake = _fake(monkeypatch)
    tid = _turn(db, "Your reference is TD-2026-0142.")
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)

    assert fake.calls[0]["input"] == "Your reference is T D, 2026, 0 1 4 2."
    assert db.get(ConversationTurn, tid).text == "Your reference is TD-2026-0142."


def test_voice_follows_the_turns_language(db, monkeypatch):
    from app.config import TTS_VOICE_AR, TTS_VOICE_EN

    fake = _fake(monkeypatch)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db, "Hello"))
    speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=_turn(db, "أهلاً", lang=Language.ar)
    )
    assert fake.calls[0]["voice"] == TTS_VOICE_EN
    assert fake.calls[1]["voice"] == TTS_VOICE_AR


def test_the_tone_instruction_is_sent(db, monkeypatch):
    """gpt-4o-mini-tts is steerable, and that is the reason it was chosen over
    tts-1 — the only place rule 14's register reaches a non-Claude model."""
    fake = _fake(monkeypatch)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db, "Hello"))
    assert "instructions" in fake.calls[0]
    assert fake.calls[0]["instructions"].strip()


# --------------------------------------------------------------------------- #
# Cache — a replay must never cost a second vendor call
# --------------------------------------------------------------------------- #
def test_replaying_a_message_does_not_call_the_vendor_again(db, monkeypatch):
    fake = _fake(monkeypatch)
    tid = _turn(db)

    first = speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    second = speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)

    assert first["cached"] is False and second["cached"] is True
    assert len(fake.calls) == 1
    assert db.scalar(select(func.count(SpeechClip.id))) == 1


def test_changing_the_voice_misses_the_cache(db, monkeypatch):
    """Voice and model are in the key, so a config change re-synthesises instead
    of serving audio that no longer matches the configured persona."""
    fake = _fake(monkeypatch)
    tid = _turn(db)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)

    monkeypatch.setattr(speech_service, "TTS_VOICE_EN", "nova")
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    assert len(fake.calls) == 2


def test_the_cache_is_scoped_to_a_session(db, monkeypatch):
    """Clips are Aisha quoting the customer's own name and mobile back to them.
    A globally shared row containing a phone number belongs to nobody, which
    makes retention and rule 12 meaningless for it — so identical text in two
    sessions is synthesised twice, on purpose."""
    fake = _fake(monkeypatch)
    same = "Saturday at 7 pm works."
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db, same))
    speech_service.synthesize_turn(
        db, session_id=OTHER, turn_id=_turn(db, same, session=OTHER)
    )
    assert len(fake.calls) == 2
    assert db.scalar(select(func.count(SpeechClip.id))) == 2


def test_an_expired_clip_is_regenerated_in_place(db, monkeypatch):
    """The (session, key) pair is unique, so an expired row must be refreshed
    rather than re-inserted — otherwise the constraint rejects the new clip."""
    _fake(monkeypatch)
    tid = _turn(db)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    clip = db.scalar(select(SpeechClip))
    clip.expires_at = datetime.now() - timedelta(hours=1)
    db.flush()

    out = speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    assert out["ok"] is True and out["cached"] is False
    assert db.scalar(select(func.count(SpeechClip.id))) == 1


# --------------------------------------------------------------------------- #
# Failure degrades, never raises — the client falls back to the device voice
# --------------------------------------------------------------------------- #
def test_a_vendor_outage_is_data_not_an_exception(db, monkeypatch):
    _fake(monkeypatch, raises=RuntimeError("upstream down"))
    out = speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db))

    assert out == {"ok": False, "reason": "synthesis_failed"}
    assert db.scalar(select(func.count(SpeechClip.id))) == 0


def test_missing_key_reports_unconfigured_rather_than_failing(db, monkeypatch):
    monkeypatch.setattr(speech_service, "OPENAI_API_KEY", "")
    assert speech_service.synthesize_turn(
        db, session_id=SESSION, turn_id=_turn(db)
    )["reason"] == "tts_not_configured"


# --------------------------------------------------------------------------- #
# Audit + erasure
# --------------------------------------------------------------------------- #
def test_synthesis_is_audited_without_the_spoken_words(db, monkeypatch):
    """The reply is already the ConversationTurn. Copying it here would put a
    second copy of a message containing a name and mobile into a table the
    console renders wholesale and rule 12 does not erase."""
    _fake(monkeypatch)
    secret = "Mr Sharma, we'll text +971505550103."
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db, secret))

    row = db.scalar(select(AuditEvent).where(AuditEvent.type == AuditType.voice))
    assert row.payload["action"] == "synthesize"
    assert row.payload["outcome"] == "ok"
    assert row.payload["vendor"] == "openai"
    assert "Sharma" not in str(row.payload)
    assert "971505550103" not in str(row.payload)
    assert row.actor.value == "system"


def test_a_cached_replay_is_not_audited_again(db, monkeypatch):
    """A replay is not a new disclosure. The text went to the vendor once, and
    logging every tap would bury that fact under UI noise."""
    _fake(monkeypatch)
    tid = _turn(db)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=tid)

    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.type == AuditType.voice)
    ).all()
    assert len([r for r in rows if r.payload.get("action") == "synthesize"]) == 1


def test_forget_me_deletes_the_clips(db, monkeypatch):
    """These are recordings of the customer's own name and number being read
    OUT — the most quotable form the data takes."""
    _fake(monkeypatch)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=_turn(db))
    assert db.scalar(select(func.count(SpeechClip.id))) == 1

    assert speech_service.delete_for_session(db, session_id=SESSION) == 1
    assert db.scalar(select(func.count(SpeechClip.id))) == 0

    row = db.scalars(
        select(AuditEvent).where(AuditEvent.type == AuditType.erasure)
    ).all()[-1]
    assert row.payload["action"] == "forget_me_speech"


def test_erasure_leaves_another_sessions_clips_alone(db, monkeypatch):
    _fake(monkeypatch)
    speech_service.synthesize_turn(
        db, session_id=OTHER, turn_id=_turn(db, session=OTHER)
    )
    assert speech_service.delete_for_session(db, session_id=SESSION) == 0
    assert db.scalar(select(func.count(SpeechClip.id))) == 1


def test_purge_removes_only_expired_clips(db, monkeypatch):
    _fake(monkeypatch)
    keep = _turn(db, "Keep this one.")
    drop = _turn(db, "This one has aged out.")
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=keep)
    speech_service.synthesize_turn(db, session_id=SESSION, turn_id=drop)
    stale = db.scalar(select(SpeechClip).where(SpeechClip.turn_id == drop))
    stale.expires_at = datetime.now() - timedelta(hours=1)
    db.flush()

    assert speech_service.purge_expired(db) == 1
    assert db.scalar(select(func.count(SpeechClip.id))) == 1
