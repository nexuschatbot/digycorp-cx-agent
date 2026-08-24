"""Voice notes (input only) — services/voice.py.

NO LIVE VENDOR CALLS. Every transcription here is a monkeypatched client. The
one thing a live call would prove that a fake cannot — that gpt-4o-transcribe
returns Gulf Arabic in Arabic SCRIPT rather than Latin transliteration — was
established once, by hand, in the V0 spike on 2026-07-21, and is recorded in
CLAUDE.md. Pinning it in CI would spend money on every run to re-answer a
question about a vendor, not about our code.

WHAT IS DELIBERATELY NOT TESTED HERE: transcription ACCURACY. That is a property
of the model and the audio, not of this module, and a test asserting a specific
transcript would be testing OpenAI. What IS tested is everything accuracy
depends on that we own — that the biasing vocabulary is built from the live
catalogue, and that it is actually passed on the call.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import AuditEvent, AuditType, Customer, Language, VoiceNote
from app.services import voice as voice_service

SESSION = "sess-voice-1"
OTHER_SESSION = "sess-voice-2"
AUDIO = b"\x00\x01fake-audio-bytes"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeTranscriptions:
    def __init__(self, text: str | None = None, raises: Exception | None = None):
        self.text = text
        self.raises = raises
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return type("R", (), {"text": self.text})()


def _fake_client(monkeypatch, text=None, raises=None) -> _FakeTranscriptions:
    """Patch the vendor call AND the key, so no test depends on the dev's .env."""
    fake = _FakeTranscriptions(text=text, raises=raises)
    client = type("C", (), {"audio": type("A", (), {"transcriptions": fake})()})()
    monkeypatch.setattr(voice_service, "_client", lambda: client)
    monkeypatch.setattr(voice_service, "OPENAI_API_KEY", "test-key")
    return fake


def _customer(db) -> int:
    c = Customer(mobile="+971505550199", name_en="Voice Tester")
    db.add(c)
    db.flush()
    return c.id


def _store(db, monkeypatch, text="I'd like to test drive the Jetour T2", **kw):
    _fake_client(monkeypatch, text=text)
    return voice_service.transcribe_and_store(
        db, session_id=kw.pop("session_id", SESSION), audio=AUDIO,
        mime=kw.pop("mime", "audio/mp4"), **kw,
    )


# --------------------------------------------------------------------------- #
# The vocabulary prompt — the V0 spike's actual finding, locked in
# --------------------------------------------------------------------------- #
def test_vocabulary_comes_from_the_live_catalogue(db):
    """Unprompted, the vendor heard "Jetour" as "Jetter" and "Deira" as "Dera" —
    it corrupted the brand and the showroom, i.e. the two entities a booking is
    made of. The fix is passing the catalogue as the biasing prompt, which makes
    this string load-bearing rather than decorative.

    It must be BUILT, never typed: a hardcoded list drifts from the catalogue it
    describes, and the first symptom would be the agent failing to recognise a
    car we had just added."""
    vocab = voice_service._vocabulary(db)

    names = db.scalars(select(__import__("app.models", fromlist=["x"]).VehicleModel.name)).all()
    for name in names:
        assert name in vocab, f"{name!r} missing from the biasing vocabulary"
    assert "Deira" in vocab
    assert "Jetour" in vocab
    # Arabic-script proper nouns too: the catalogue holds "Deira", never "ديرة",
    # so without these the prompt helps an English speaker and nobody else.
    assert "ديرة" in vocab
    assert "جيتور" in vocab


def test_the_vocabulary_is_actually_passed_to_the_vendor(db, monkeypatch):
    """A vocabulary that is built and then not sent is worse than none — it
    reads as solved."""
    fake = _fake_client(monkeypatch, text="hello")
    voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    assert "Jetour" in fake.calls[0]["prompt"]


def test_no_language_is_forced_on_the_vendor(db, monkeypatch):
    """Rule 3: which language the customer chose is the thing we are trying to
    learn. Pinning `language=` would defeat the bilingual requirement outright."""
    fake = _fake_client(monkeypatch, text="hello")
    voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    assert "language" not in fake.calls[0]


# --------------------------------------------------------------------------- #
# Guards — rejected before anything is stored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"audio": b""}, "empty_recording"),
        ({"audio": b"x" * (9 * 1024 * 1024)}, "recording_too_large"),
        ({"duration_ms": 999_000}, "recording_too_long"),
        ({"mime": "video/mp4"}, "unsupported_format"),
    ],
)
def test_rejected_uploads_store_nothing(db, monkeypatch, kwargs, reason):
    _fake_client(monkeypatch, text="never reached")
    args = {"audio": AUDIO, "mime": "audio/mp4", "session_id": SESSION, **kwargs}
    out = voice_service.transcribe_and_store(db, **args)

    assert out["ok"] is False
    assert out["reason"] == reason
    assert out["audio_id"] is None
    assert db.scalar(select(func.count(VoiceNote.id))) == 0


def test_missing_api_key_degrades_rather_than_raising(db, monkeypatch):
    """The ordinary state of a text-only checkout and of CI. The UI needs to be
    able to offer typing instead — which needs a reason, not a 500."""
    monkeypatch.setattr(voice_service, "OPENAI_API_KEY", "")
    out = voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    assert out == {"ok": False, "reason": "stt_not_configured", "audio_id": out["audio_id"]}
    assert out["audio_id"] is not None


# --------------------------------------------------------------------------- #
# Failure keeps the recording — the whole reason we store before transcribing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"raises": RuntimeError("vendor down")}, "transcription_failed"),
        ({"text": "   "}, "no_speech_detected"),
    ],
)
def test_a_failed_transcription_still_leaves_a_playable_recording(
    db, monkeypatch, kw, reason
):
    """The customer DID record something. The player must work even when we
    could not make out a word, and the audio is the only evidence of what was
    actually said — deleting on failure would destroy exactly the case worth
    investigating."""
    _fake_client(monkeypatch, **kw)
    out = voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )

    assert out["ok"] is False
    assert out["reason"] == reason
    assert out["audio_id"]

    note = db.scalar(select(VoiceNote))
    assert note.audio == AUDIO
    assert note.transcript == ""
    assert voice_service.get(db, audio_id=out["audio_id"], session_id=SESSION) is not None


def test_a_vendor_outage_never_raises(db, monkeypatch):
    """Errors are data (the tool contract). A live turn must not die because a
    US endpoint had a bad minute."""
    _fake_client(monkeypatch, raises=TimeoutError("boom"))
    out = voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    assert out["ok"] is False


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #
def test_a_successful_transcription_stores_transcript_and_language(db, monkeypatch):
    out = _store(db, monkeypatch, text="أبغى أجرب الجيتور تي تو بكرة")

    assert out["ok"] is True
    assert out["lang"] == "ar"
    note = db.scalar(select(VoiceNote))
    assert note.transcript == "أبغى أجرب الجيتور تي تو بكرة"
    assert note.lang is Language.ar
    assert note.size_bytes == len(AUDIO)


def test_language_follows_the_transcript_script(db, monkeypatch):
    """The whole bilingual requirement rests on this: the reply language is
    derived from what was SAID, via the same script rule typed turns use."""
    assert _store(db, monkeypatch, text="Saturday evening in Deira")["lang"] == "en"
    assert _store(db, monkeypatch, text="بكرة بعد المغرب")["lang"] == "ar"


def test_audio_ids_are_unguessable_and_unique(db, monkeypatch):
    a = _store(db, monkeypatch)["audio_id"]
    b = _store(db, monkeypatch)["audio_id"]
    assert a != b
    assert len(a) == 36  # uuid4, not a sequential integer anyone can count


# --------------------------------------------------------------------------- #
# Playback gate
# --------------------------------------------------------------------------- #
def test_only_the_recording_session_can_replay_it(db, monkeypatch):
    """Stricter than the booking gate, on purpose. A booking is legitimately
    reachable by reference alone (FR-RS-01), so there `session_id` only decides
    whether the PII fields are included. A recording has no second legitimate
    route — so a mismatch returns nothing, not a redacted variant."""
    audio_id = _store(db, monkeypatch)["audio_id"]

    assert voice_service.get(db, audio_id=audio_id, session_id=SESSION) is not None
    assert voice_service.get(db, audio_id=audio_id, session_id=OTHER_SESSION) is None
    assert voice_service.get(db, audio_id=audio_id, session_id=None) is None
    assert voice_service.get(db, audio_id="no-such-id", session_id=SESSION) is None


def test_expiry_is_enforced_on_read_not_only_by_the_sweep(db, monkeypatch):
    """Otherwise a recording stays retrievable for however long it takes a purge
    to run, and "48h retention" becomes a claim about our scheduling rather than
    about the data."""
    audio_id = _store(db, monkeypatch)["audio_id"]
    note = db.scalar(select(VoiceNote))
    note.expires_at = datetime.now() - timedelta(seconds=1)
    db.flush()

    assert voice_service.get(db, audio_id=audio_id, session_id=SESSION) is None


# --------------------------------------------------------------------------- #
# Identification backfill — what makes erasure able to find early recordings
# --------------------------------------------------------------------------- #
def test_backfill_links_recordings_made_before_identification(db, monkeypatch):
    """A customer records BEFORE they identify far more often than after: "I want
    to test drive the T2" comes many turns before a mobile number. Without the
    backfill, "forget me" searching by customer would miss precisely the
    recordings most likely to exist."""
    _store(db, monkeypatch)
    _store(db, monkeypatch)
    assert all(n.customer_id is None for n in db.scalars(select(VoiceNote)).all())

    cid = _customer(db)
    assert voice_service.attach_customer(db, session_id=SESSION, customer_id=cid) == 2
    assert all(n.customer_id == cid for n in db.scalars(select(VoiceNote)).all())
    # Idempotent — runs on every turn, must attach nothing the second time.
    assert voice_service.attach_customer(db, session_id=SESSION, customer_id=cid) == 0


def test_backfill_does_not_touch_another_session(db, monkeypatch):
    _store(db, monkeypatch, session_id=OTHER_SESSION)
    cid = _customer(db)
    assert voice_service.attach_customer(db, session_id=SESSION, customer_id=cid) == 0


# --------------------------------------------------------------------------- #
# Rule 12 — erasure takes the audio with it
# --------------------------------------------------------------------------- #
def test_forget_me_hard_deletes_the_recordings(db, monkeypatch):
    """Voice is personal data and arguably biometric-adjacent, so an erasure
    that kept the audio would be the tombstone rule 12 refuses — and S-08 walks
    this trail on screen."""
    audio_id = _store(db, monkeypatch)["audio_id"]
    cid = _customer(db)
    voice_service.attach_customer(db, session_id=SESSION, customer_id=cid)

    assert voice_service.delete_for_customer(db, customer_id=cid, session_id=SESSION) == 1
    assert db.scalar(select(func.count(VoiceNote.id))) == 0
    assert voice_service.get(db, audio_id=audio_id, session_id=SESSION) is None


def test_erasure_catches_recordings_never_linked_to_the_customer(db, monkeypatch):
    """The belt to the backfill's braces: audio recorded in the live conversation
    the erasure is happening in, before identification resolved. Deleting a bit
    too much of the REQUESTER'S OWN audio is the safe direction to err in."""
    _store(db, monkeypatch)
    cid = _customer(db)  # deliberately NOT attached

    assert voice_service.delete_for_customer(db, customer_id=cid, session_id=SESSION) == 1
    assert db.scalar(select(func.count(VoiceNote.id))) == 0


def test_erasure_does_not_delete_another_customers_recordings(db, monkeypatch):
    _store(db, monkeypatch, session_id=OTHER_SESSION)
    cid = _customer(db)

    assert voice_service.delete_for_customer(db, customer_id=cid, session_id=SESSION) == 0
    assert db.scalar(select(func.count(VoiceNote.id))) == 1


def test_erasure_is_audited_with_ids_but_never_the_words(db, monkeypatch):
    _store(db, monkeypatch, text="my name is Ravi and my number is 0505550103")
    cid = _customer(db)
    voice_service.attach_customer(db, session_id=SESSION, customer_id=cid)
    voice_service.delete_for_customer(db, customer_id=cid, session_id=SESSION)

    row = db.scalars(
        select(AuditEvent).where(AuditEvent.type == AuditType.erasure)
    ).all()[-1]
    assert row.payload["action"] == "forget_me_voice"
    assert row.payload["deleted"] == 1
    # What was said is exactly what is being erased — recording it in the trail
    # would defeat the request being honoured.
    assert "Ravi" not in str(row.payload)


# --------------------------------------------------------------------------- #
# Reload replay — the transcript carries a POINTER, never a copy
# --------------------------------------------------------------------------- #
def test_a_spoken_turn_replays_its_recording_pointer(db, monkeypatch):
    """The frontend rebuilds the player from this id, so a reloaded spoken turn
    looks like the one the customer left on screen."""
    from app.models import ConversationTurn, Speaker
    from app.services import conversation

    out = _store(db, monkeypatch, text="Saturday evening in Deira")
    db.add(
        ConversationTurn(
            session_id=SESSION, speaker=Speaker.customer, lang=Language.en,
            text=out["text"], audio_id=out["audio_id"],
        )
    )
    db.add(
        ConversationTurn(
            session_id=SESSION, speaker=Speaker.agent, lang=Language.en, text="Of course."
        )
    )
    db.flush()

    turns = conversation.history(db, session_id=SESSION)["turns"]
    assert turns[0]["audio_id"] == out["audio_id"]
    # Aisha never speaks — TTS is out of scope, so an agent turn can never carry
    # a recording. Present-and-null rather than absent, so the client never has
    # to distinguish "typed" from "written before this field existed".
    assert turns[1]["audio_id"] is None


def test_a_failed_voice_note_still_replays_as_a_turn(db, monkeypatch):
    """An untranscribable recording is stored as an EMPTY turn carrying only its
    audio_id — the player is the whole message, and the honest record is that the
    customer spoke and no words were recovered. The replay must keep it: dropping
    empty-text turns would make the thread disagree with itself on reload about
    whether they had said anything."""
    from app.models import ConversationTurn, Speaker
    from app.services import conversation

    _fake_client(monkeypatch, text="")
    out = voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    db.add(
        ConversationTurn(
            session_id=SESSION, speaker=Speaker.customer, lang=Language.en,
            text="", audio_id=out["audio_id"],
        )
    )
    db.flush()

    turns = conversation.history(db, session_id=SESSION)["turns"]
    assert turns[0]["text"] == ""
    assert turns[0]["audio_id"] == out["audio_id"]


def test_the_transcript_stores_a_pointer_not_the_audio(db, monkeypatch):
    """The bytes have ONE home and ONE lifetime. A second copy on the turn would
    outlive both the 48h retention and the rule 12 delete — the row that replays
    a recording must not itself be a recording."""
    from app.models import ConversationTurn, Speaker
    from app.services import conversation

    out = _store(db, monkeypatch)
    db.add(
        ConversationTurn(
            session_id=SESSION, speaker=Speaker.customer, lang=Language.en,
            text=out["text"], audio_id=out["audio_id"],
        )
    )
    db.flush()

    turn = conversation.history(db, session_id=SESSION)["turns"][0]
    assert set(turn) == {
        "turn_id", "speaker", "lang", "text", "ts", "cards", "audio_id",
    }
    assert isinstance(turn["audio_id"], str)

    # Erase the recording; the TURN survives with its words, the POINTER now
    # resolves to nothing, and the player disappears rather than rendering a
    # shell (FR-CX-07).
    cid = _customer(db)
    voice_service.attach_customer(db, session_id=SESSION, customer_id=cid)
    voice_service.delete_for_customer(db, customer_id=cid, session_id=SESSION)

    turn = conversation.history(db, session_id=SESSION)["turns"][0]
    assert turn["text"] == out["text"]
    assert voice_service.get(db, audio_id=turn["audio_id"], session_id=SESSION) is None


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_purge_deletes_only_what_has_expired(db, monkeypatch):
    keep = _store(db, monkeypatch)["audio_id"]
    drop = _store(db, monkeypatch)["audio_id"]
    stale = db.scalar(select(VoiceNote).where(VoiceNote.audio_id == drop))
    stale.expires_at = datetime.now() - timedelta(hours=1)
    db.flush()

    assert voice_service.purge_expired(db) == 1
    assert voice_service.get(db, audio_id=keep, session_id=SESSION) is not None
    assert db.scalar(select(func.count(VoiceNote.id))) == 1


def test_recording_sets_a_retention_horizon(db, monkeypatch):
    _store(db, monkeypatch)
    note = db.scalar(select(VoiceNote))
    assert note.expires_at > note.created_at


# --------------------------------------------------------------------------- #
# Audit (rule 15) — the mechanical fact, not the customer's words
# --------------------------------------------------------------------------- #
def test_transcription_is_audited_without_the_transcript(db, monkeypatch):
    """The transcript is already the ConversationTurn. Copying it here would put
    a second, ungated copy of the customer's words in a table the console
    renders wholesale and rule 12 does not erase. What belongs here is the
    mechanical fact: audio went to a vendor, which one, and how it went."""
    secret = "my mobile is 0505550103"
    _store(db, monkeypatch, text=secret)

    row = db.scalar(select(AuditEvent).where(AuditEvent.type == AuditType.voice))
    assert row.payload["action"] == "transcribe"
    assert row.payload["outcome"] == "ok"
    assert row.payload["vendor"] == "openai"
    assert row.payload["lang"] == "en"
    assert secret not in str(row.payload)
    assert "transcript" not in row.payload
    # actor=system: the customer pressed record, but transcription is something
    # WE then did to their audio — neither they nor the agent decided it.
    assert row.actor.value == "system"


@pytest.mark.parametrize(
    "kw,outcome",
    [({"raises": RuntimeError("x")}, "error"), ({"text": ""}, "no_speech")],
)
def test_failed_transcriptions_are_audited_too(db, monkeypatch, kw, outcome):
    """A vendor that silently failed on a third of the demo's recordings must be
    visible afterwards, not merely felt at the time."""
    _fake_client(monkeypatch, **kw)
    voice_service.transcribe_and_store(
        db, session_id=SESSION, audio=AUDIO, mime="audio/mp4"
    )
    row = db.scalar(select(AuditEvent).where(AuditEvent.type == AuditType.voice))
    assert row.payload["outcome"] == outcome
