"""The realtime call session — `CALL_MODE=realtime` (2026-07-27).

The speech-to-speech engine behind the SAME WebSocket the pipeline mode uses:
`call.py`'s `call_ws` branches here after its guards, and the client speaks one
protocol for both modes (the only addition is PCM audio segments —
pcm_start / binary frames / pcm_end — since the vendor speaks raw PCM, not
mp3, plus `stop_audio` for its barge-in).

WHAT IS KEPT FROM THE PIPELINE, deliberately, because these are the properties
that make it Aisha and not a chatbot wearing her name:

  TOOLS + GUARDS + AUDIT — every function call the vendor model makes runs
  through app/agent/tool_node._dispatch: same REGISTRY, same trusted-context
  injection (customer_id resolved server-side, never model-supplied), same
  error-as-data envelopes; and each call is audited at the same choke-point
  shape (actor=agent tool_call row, correlation_id = the vendor call_id).

  LIFECYCLE — CALL_ENABLED, the silence and max-minutes watchdogs, spoken
  refusals, call_started/call_ended audit rows.

THE CALL PERSISTS ITS TRANSCRIPT (2026-08-04, re-enabling the per-turn storage
that the 2026-07-29 "ephemeral call" decision had removed — the read-back-the-
call feature). Every customer utterance and every reply she speaks is written
to the thread as an ordinary ConversationTurn (see _store_call_turn), so the
WHOLE call can be read back in the chat once it ends. On hangup it ALSO leaves
the summary marker it always has: a Speaker.system "call:<seconds>" row carrying
the booking refs the call produced as a `cards` anchor, which the client renders
as a muted "call happened" line plus the live booking card. Consequences, all
intended:
  - PDPL gets HEAVIER again: the transcript rows are back on the D1 surface.
    Customer and agent TEXT now persists here, so the rule-12 erasure sweep
    reaches it exactly as it does typed turns (both are ConversationTurn rows,
    keyed by session_id). This REVERSES the "stored nothing" half of the
    2026-07-29 note; CLAUDE.md's D1 section must reflect that the call is a
    stored-text surface once more. AUDIO is still never stored — only the
    transcript text, and nothing on the DB hot path mid-turn (the writes are
    off the audio loop; see _store_call_turn).
  - The BOOKING is still real: tools, guards, audit rows are untouched.
  - The thread is now the FULL record, not just a marker: what was said is
    readable, which is the whole point of the read-back feature.

KNOWN GAPS, stated rather than hidden:
  - SPLIT MEMORY with the CLAUDE CHECKPOINT persists: the transcript reaches
    the THREAD (the rows above) but NOT the LangGraph checkpoint, so an agent
    asked "as I said on the call…" still knows only what the tools recorded
    (the booking itself) plus the seeded typed thread. Only call OUTCOMES are
    written into the checkpoint (see _CHECKPOINT_TOOLS).
  - RULE 2 BY COMPLIANCE: grounding rests on GPT obeying "facts come from
    tools", not on Claude's tuned discipline.
  - close_conversation: honoured when the model calls it (the call ends with
    the chat), but GPT is less reliable about calling it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from fastapi import WebSocket, WebSocketDisconnect

from app.agent import build_graph, new_sqlite_checkpointer
from app.agent.call_persona import NATURAL_GREETING, natural_instructions
from app.agent.prompt import realtime_instructions
from app.agent.tool_node import _CLOSE_TOOL, _IDENTITY_TOOLS, _dispatch
from app.api.intents import _synthetic_pair
from app.config import (
    AGENT_CHECKPOINT_PATH,
    CALL_FAREWELL_TAIL_SECONDS,
    CALL_MAX_MINUTES,
    CALL_NUDGE_SECONDS,
    CALL_NATURAL_VOICE,
    CALL_PERSONA,
    CALL_SILENCE_HANGUP_SECONDS,
    CALL_SPEECH_GATE,
    CALL_TRANSPORT,
    RTC_STUN_URL,
    RTC_TURN_URL,
    client_ice_servers,
    turn_credentials,
)
from app.db import SessionLocal
from app.models import Actor, AuditType, ConversationTurn, Language, Speaker
from app.services import audit as audit_service
from app.services import call_trace
from app.services import voice as voice_service
from app.services.realtime_agent import VENDOR_SAMPLE_RATE, make_agent_session
from app.services.realtime_stt import resample_pcm16
from app.services.rtc_transport import make_rtc_transport
from app.services.speech_gate import make_speech_gate
from app.tools.call_schemas import CALL_TOOL_FNS

log = logging.getLogger("app.api.call_realtime")

_MIN_UTTERANCE_BYTES = 4800
# The per-side, per-turn audio-buffer ceiling for CALL_TRACE_AUDIO (2026-08-04):
# 90 s of PCM16 mono @ 24kHz (48 bytes/ms). A turn that runs longer keeps only
# its first 90 s in the trace — a demo diagnostic, not a recording, and a
# bound is what keeps a stuck turn from growing memory without limit.
_MAX_TRACE_AUDIO_BYTES = 24_000 * 2 * 90
_DEFAULT_SAMPLE_RATE = 24_000
_SAMPLE_RATES = {8_000, 16_000, 24_000, 48_000}

# Call outcomes the CHAT agent must know about (split-memory narrowing,
# 2026-07-31) — the state-changing tools whose successful results are written
# into the LangGraph checkpoint as synthetic pairs. forget_me is deliberately
# absent: see the note at the write site.
_CHECKPOINT_TOOLS = frozenset({
    "find_or_create_customer",
    # The manage-a-booking front door (2026-08-05). Same reason as its sibling
    # above: a customer who identified themselves on the CALL must not be asked
    # again the moment they hang up and type.
    "identify_by_reference_or_mobile",
    "record_consent",
    "create_booking",
    "reschedule_booking",
    "cancel_booking",
})

# The turn engine's dials (2026-07-30, words-decide-everything rewrite).
# How long to wait for a transcript before answering from audio alone — the
# model heard the audio natively, so a dead transcriber costs the record,
# never the reply.
_TRANSCRIPT_FALLBACK_S = 2.5
# Dial-until-ready (2026-08-03, the doubled-mic fix). The greeting used to
# fire the moment the vendor session was up, while the WebRTC upgrade was
# still negotiating — so the greeting was exactly the reply that straddled
# the transport flip, and for that whole window the vendor heard the SAME
# microphone twice (the WS-PCM uplink AND the new peer connection),
# interleaved. Now the client keeps its ring tone until call_ready and the
# greeting waits for the transport to settle: up to _RTC_OFFER_WAIT_S for the
# client's rtc_offer to arrive, then up to _RTC_SETTLE_WAIT_S for ICE to
# connect. Whatever happens — refusal, negotiation failure, timeout — the
# call starts on WS-PCM: the wait costs a moment of honest ringing, never
# the call. The suite zeroes the offer wait (conftest) so every existing
# test greets inline, exactly as before the deferral existed.
_RTC_OFFER_WAIT_S = 1.0
_RTC_SETTLE_WAIT_S = 3.0
# The stale-audio embargo's ceiling (2026-08-03, the mute-forever fix). The
# embargo raised by a supersede is lifted by the NEXT response_started — an
# event the vendor does not guarantee: when the re-ask loses the
# already-active race, no new response ever starts, the flag stays up, and
# every later audio frame dies at the door for the rest of the call. The
# watchdog now clears an embargo older than this; the known race also
# recovers precisely (see response_rejected). Worst case becomes seconds of
# lost audio, never a silent call.
_EMBARGO_MAX_S = 3.0

# THE REPLY THAT NEVER CAME (2026-08-03, the go-silent bug — four live calls
# on the dev VM, every one of them dead from its first interruption). A
# customer turn is answered by exactly one response.create; the vendor has
# been seen to ACCEPT it and then produce nothing at all — no response.created,
# no error, no rejection to recover from (response_rejected handles the one
# refusal it does announce). The call is then stuck in "thinking" for good, and
# nothing rescues it: the idle ladder only runs while LISTENING, so neither the
# nudge nor the silence hangup can fire, and the customer repeats themselves
# into a line that will never answer. One bounded re-ask, then the call is
# handed back to the idle ladder — a call that cannot answer must at least be
# able to say goodbye.
_REPLY_START_MAX_S = 6.0
# An "interruption" of at most this many words can be judged her own echo
# surviving AEC — but only as a verbatim IN-ORDER fragment of her reply with
# no leading yes/no word (hardened 2026-08-03, see _is_reply_echo: the old
# any-order bag-of-words match swallowed real confirmations spoken over her
# recap, which reuse exactly her words).
_ECHO_MAX_WORDS = 12
# Cutting Aisha off MID-REPLY takes at least this many words (2026-08-03, the
# LiveKit interruption-substance rule): a single word heard over her voice is
# a listener noise, an echo fragment, or a noise-burst hallucination far more
# often than a real "stop everything" — one call lost five replies to such
# supersedes. The word is still delivered as a heard turn; she just keeps
# talking. A single word spoken while she is QUIET answers normally.
# ⚠️ NARROWED TO THE WS-PCM FALLBACK 2026-08-04 — see the `gated` comment in
# _decide_turn. This rule paid for a cost that truncation removed.
_MIN_INTERRUPT_WORDS = 2
# Listener noises that mean "go on", not "stop": heard OVER her reply they
# resume playback instead of interrupting. Matched against the WHOLE
# normalized transcript, so "okay but wait" is never swallowed. Both call
# languages (Arabic added 2026-07-30 with the bilingual call).
# ⚠️ NARROWED TO THE WS-PCM FALLBACK 2026-08-04, same reason as
# _MIN_INTERRUPT_WORDS: on the rtc path "hmm" now means stop, as it would to a
# person, because stopping her no longer costs her memory of what she said.
_BACKCHANNELS = {
    "yeah", "yes", "yep", "ya", "ok", "okay", "okey", "mm", "mhm", "mm hmm",
    "uh huh", "hmm", "right", "sure", "alright", "all right", "got it",
    "cool", "nice", "great", "i see", "go on", "carry on", "go ahead",
    "okay yeah", "yeah okay", "yes yes", "yeah yeah",
    # Arabic listener noises — normalized (no punctuation, no diacritics
    # expected from the transcriber).
    "نعم", "ايوه", "أيوه", "اي", "إي", "ايه", "تمام", "اوكي", "أوكي",
    "طيب", "ماشي", "زين", "صح", "اها", "أها", "ممم", "اوك",
    "تمام تمام", "اي نعم", "ايوا",
}

# A LEADING verdict word marks a confirmation, not echo (the echo guard's
# 2026-08-03 hardening — see _is_reply_echo). Deliberately narrower than
# _BACKCHANNELS: only words a customer LEADS a confirmation or denial with,
# and none she routinely opens her own sentences with ("perfect", "great",
# "sure" are her openers — an echoed opener must not skip the guard).
_YESNO_WORDS = {
    "yes", "yeah", "yep", "yup", "no", "nope", "correct",
    "نعم", "ايوه", "أيوه", "اي", "إي", "لا", "صح", "مضبوط", "ايوا",
}


# A booking reference as she might speak it: TD-YYYY-XXXX (booking._make_ref's
# random base32 suffix). Case-insensitive because ref lookup is (a ref is a
# code, not case-sensitive prose — services/booking).
_REF_RE = re.compile(r"\bTD-\d{4}-[A-Z0-9]{4}\b", re.IGNORECASE)

# The same reference as a HUMAN says it, which is the shape a transcriber writes:
# "TD 2026 HTXE", "td2026htxe", "TD-2026 HTXE". Nobody speaks the dashes. Used
# ONLY to whitelist references the customer themselves spoke (see
# _finish_utterance) — the guard that checks HER replies keeps the strict pattern
# above, so this looser one can never let an invented reference through: it can
# only recognise one the customer already said.
_SPOKEN_REF_RE = re.compile(r"\bTD[\s\-.]*(\d{4})[\s\-.]*([0-9A-Z]{4})\b", re.IGNORECASE)

# ---- spoken-digit normalization (the mobile evidence guard, 2026-07-31) ----
# The transcriber writes numbers as digits, digit words, or Eastern Arabic
# numerals depending on its mood; evidence must survive all three. EN and AR
# digit words only — the two call languages.
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "صفر": "0", "واحد": "1", "وحده": "1",
    "اثنين": "2", "اثنان": "2", "إثنين": "2", "ثنين": "2",
    "ثلاثة": "3", "ثلاث": "3", "أربعة": "4", "اربعة": "4", "أربع": "4",
    "اربع": "4", "خمسة": "5", "خمس": "5", "ستة": "6", "ست": "6",
    "سبعة": "7", "سبع": "7", "ثمانية": "8", "ثمانيه": "8", "ثمان": "8",
    "تسعة": "9", "تسع": "9",
}
_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _spoken_digits(text: str) -> str:
    """Every digit in `text`, in spoken order, whatever form it took —
    '050 987 6543', 'zero five oh', 'double five', 'خمسة صفر', '٠٥٠'. The
    currency of the mobile evidence guard: what the customer's mouth actually
    produced, reduced to a comparable stream."""
    out: list[str] = []
    repeat = 1
    for w in _normalize_words(text.translate(_EASTERN_DIGITS)):
        if w == "double":
            repeat = 2
            continue
        if w == "triple":
            repeat = 3
            continue
        d = _DIGIT_WORDS.get(w)
        if d is None and w.isdigit():
            d = w
        if d is not None:
            out.append(d * repeat if len(d) == 1 else d)
        repeat = 1
    return "".join(out)


def _harvest_amounts(text: str) -> set[int]:
    """Every digit-form number >= 1000 in the text (commas and Eastern
    digits normalised) — the price guard's whitelist currency. Harvested
    from tool-result JSON and the seeded thread, checked against AED
    claims in her replies."""
    plain = text.translate(_EASTERN_DIGITS)
    out: set[int] = set()
    for m in re.finditer(r"\d[\d,]*", plain):
        try:
            n = int(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if n >= 1000:
            out.add(n)
    return out


def _harvest_slot_ids(result: object) -> set[int]:
    """Every value under a 'slot_id' key, recursively — the slot provenance
    guard's whitelist. Callers pass only SUCCESSFUL results (see the guard's
    laundering note in __init__)."""
    out: set[int] = set()
    if isinstance(result, dict):
        for key, value in result.items():
            if key == "slot_id" and isinstance(value, int):
                out.add(value)
            else:
                out |= _harvest_slot_ids(value)
    elif isinstance(result, list):
        for item in result:
            out |= _harvest_slot_ids(item)
    return out


def _harvest_refs(text: str) -> set[str]:
    """Every booking reference present in `text`, uppercased — the currency of
    the reference guard's known-refs set."""
    return {m.group(0).upper() for m in _REF_RE.finditer(text)}


def _harvest_spoken_refs(text: str) -> set[str]:
    """Every booking reference in `text` however loosely it is punctuated,
    returned in CANONICAL form (2026-08-05).

    Canonical, not verbatim, because the set is compared against her replies,
    where the model writes the dashes back in — a whitelist holding "TD 2026
    HTXE" would not recognise "TD-2026-HTXE" as the same reference and the guard
    would fire on a read-back that is exactly right.
    """
    return {
        f"TD-{m.group(1)}-{m.group(2).upper()}" for m in _SPOKEN_REF_RE.finditer(text)
    }


def _edit1(a: str, b: str) -> bool:
    """Edit distance ≤ 1 — one substitution, insertion or deletion."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        diff += 1
        if diff > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return diff + (len(b) - j) + (len(a) - i) <= 1


# THE SAME HELLO, ANSWERED TWICE (2026-08-03, the two-voices bug). Live on the
# dev VM: one spoken "hello" arrived as TWO transcripts 2.4 s apart —
# 'Hey, hello, Aitha.' then 'Hi, hello Aisha.' — and each drew its own reply,
# so two of her voices played at once. The duplicate is not word-identical
# (the transcriber spells the same audio differently each pass), so equality
# cannot catch it; per-word edit-1 tolerance can. This is the CUSTOMER-side
# twin of _is_reply_echo, which does the same job for her own voice.
_DUPLICATE_WINDOW_S = 4.0
# The tighter window for a ONE-WORD repeat (2026-09-07, see _decide_turn).
# Deliberately half the general one: the whole reason `_near_repeat` refuses
# single words is that "yes" after "yes" is usually two real answers, so the
# only case worth catching is the one where the clock says a human could not
# have produced both. The live pair that motivated it arrived ~1s apart.
_SHORT_DUPLICATE_WINDOW_S = 2.0
_DUPLICATE_SHARE = 0.6


def _near_repeat(new: list[str], previous: list[str]) -> bool:
    """Is `new` the same utterance as `previous`, re-transcribed? Judged by
    the share of its words that appear in the previous one, allowing a
    one-character difference per word (Aisha/Aitha, hello/helo). Two words
    minimum: a lone "yes" legitimately follows a lone "yes"."""
    if len(new) < 2 or not previous:
        return False
    matched = sum(
        1 for w in new if any(w == p or _edit1(w, p) for p in previous)
    )
    return matched / len(new) >= _DUPLICATE_SHARE


def _tail_heard(claimed: str, heard: str) -> bool:
    """Were the last seven digits of `claimed` heard? Exact substring first;
    otherwise ONE transcriber slip is forgiven — the model hears the audio
    natively while the evidence is the written transcript, and the two once
    disagreed by a single digit on a number the customer really did say
    (live 2026-07-31). One dropped, added or misheard digit must cost
    nothing; a different number entirely still fails by a mile."""
    tail = claimed[-7:]
    if not tail:
        return True
    if tail in heard:
        return True
    n = len(tail)
    for w in range(max(1, n - 1), n + 2):
        for i in range(0, max(0, len(heard) - w) + 1):
            if _edit1(tail, heard[i:i + w]):
                return True
    return False


def _normalize_words(text: str) -> list[str]:
    """Lowercased words, punctuation stripped — the currency of the turn
    engine's comparisons. \\w is unicode-aware, so Arabic script survives."""
    return [w for w in re.sub(r"[^\w\s]", " ", text.lower()).split() if w]


def _detect_lang(text: str) -> str:
    """The system-wide rule (per-message Arabic-script detection), applied to
    call events so an Arabic turn renders RTL and the on-device fallback
    voice picks the right language."""
    return "ar" if re.search(r"[؀-ۿ]", text) else "en"


# A clear spoken SIGN-OFF in her reply — the conversation-end auto-close signal
# (2026-08-05). The realtime model reliably SAYS goodbye at the end of a call
# but does NOT reliably call close_conversation (module docstring's known gap),
# so a natural farewell used to leave the line open until the silence ladder or
# the max-minutes cap caught it (QA report). Detecting the goodbye she actually
# speaks closes the call the moment she finishes it.
#
# STRONG closings only, EN + AR, so it never fires on a mid-conversation phrase:
# unambiguous sign-offs match anywhere; ambiguous words (bare "bye", "take
# care") must END the reply. A misread is self-correcting anyway — the customer
# speaking during the goodbye drain reopens the call (_decide_turn's
# pending-close cancel).
_FAREWELL_RE = re.compile(
    r"good\s*bye"
    r"|have a (?:great|good|nice|lovely|wonderful|pleasant) (?:day|evening|night|weekend|one)"
    r"|thank(?:s| you)(?: so much| very much)? for calling"
    r"|(?:bye|take care|talk soon|speak soon|talk to you soon)\b[\s.!،]*$"
    r"|مع السلامة"
    r"|في أمان الله"
    r"|في حفظ الله"
    r"|نهارك سعيد|يومك سعيد|تصبح على خير|تصبحين على خير"
    r"|شكرا?ً? لاتصالك|شكرا?ً? لتواصلك",
    re.IGNORECASE,
)


def _is_farewell(text: str) -> bool:
    """Is this reply a clear closing sign-off (see _FAREWELL_RE)?"""
    return bool(_FAREWELL_RE.search((text or "").strip()))


# LETTERS, not the whole Arabic block: the block also holds Eastern digits
# (٠-٩), diacritics and punctuation, and a phone number transcribed as
# "٠٥٠١٢٣٤٥٦٧" mid-English-booking must not read as an Arabic sentence.
_AR_LETTER_RE = re.compile(r"[ء-ي]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def _pin_lang(text: str) -> str | None:
    """The REPLY pin's detection — majority script, not any-char (2026-07-31,
    the mixed-reply bug). _detect_lang's any-Arabic-char rule is right for
    rendering (one Arabic word makes a bubble RTL) and wrong for pinning: a
    mid-conversation code-switch often yields a MIXED transcript, and pinning
    'Arabic ONLY' off one Arabic word against an English thread made the
    model split the difference and speak both. The rule the customer was
    promised: a sentence spoken entirely in Arabic gets Arabic; anything
    mixed or Latin gets English. 'Entirely' is judged by letter majority so
    Latin brand names inside a real Arabic sentence ("أبغى أجرب الـ Jetour
    T2") don't disqualify it. No letters at all (digits, noise) returns None
    — keep the previous pin; the customer almost certainly continued in
    their own language."""
    arabic = len(_AR_LETTER_RE.findall(text))
    latin = len(_LATIN_LETTER_RE.findall(text))
    if arabic + latin == 0:
        return None
    return "ar" if arabic > latin else "en"


# ONE LANGUAGE PER CALL (product decision 2026-08-03): the verdict that LOCKS
# a call's language is deliberately stricter than _pin_lang. The transcriber
# hallucinates one-word foreign-script "utterances" out of background noise
# ('آه', '뭐에?' — both seen live), and a call must never be committed to a
# language by a sound nobody made.
_MIN_LOCK_WORDS = 2


def _lock_verdict(text: str) -> str | None:
    """A verdict clear enough to lock a whole call: at least two real words
    AND a letter-majority script. None means 'stay on the default until
    something clearer arrives' — the call keeps working, unlocked."""
    if len(_normalize_words(text)) < _MIN_LOCK_WORDS:
        return None
    return _pin_lang(text)


def _seed_pin(seed: list[tuple[str, str]]) -> str | None:
    """The CALL's language, decided the deterministic way (2026-07-31; a
    permanent LOCK since 2026-08-03). The last customer turn of the seeded
    thread that yields a script verdict decides — i.e. the language the chat
    is in at the moment the customer presses Call — and that verdict now
    fixes the whole call, greeting included. None (no seed, or nothing
    judgeable) leaves the call unlocked on the English default until the
    first clear utterance (_lock_verdict)."""
    for speaker, text in reversed(seed):
        if speaker != "customer":
            continue
        lang = _pin_lang(text)
        if lang is not None:
            return lang
    return None

# The trailing thread seeded into the session — enough to continue a booking
# conversation, small enough not to bloat every response's input (halved
# 2026-07-28: every seeded turn is re-counted on every response, against a
# 40k-TPM tier).
_SEED_TURNS = 10


class RealtimeCallSession:
    """One live speech-to-speech call. Mirrors call._CallSession's shape:
    ordered out-queue + sender, receive loop, watchdog — with the vendor
    session in place of the STT/agent/TTS pipeline."""

    def __init__(self, ws: WebSocket, session_id: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self.loop: asyncio.AbstractEventLoop | None = None
        self.out: asyncio.Queue = asyncio.Queue()
        self.state = "listening"
        self.muted = False
        self.sample_rate = _DEFAULT_SAMPLE_RATE
        self.session = None  # RealtimeAgentSession
        self.gate = None  # SpeechGate | None — set in run(); None = ungated
        # WebRTC transport (2026-07-30): audio moves to a peer connection when
        # the client negotiates one; the WS keeps everything else. None until
        # an rtc_offer arrives; rtc_active only once ICE actually connected —
        # the WS-PCM path carries the call before, and instead of, that.
        self.rtc = None
        self.rtc_active = False
        # ICE connected while a reply was mid-flight on WS-PCM: the takeover
        # is deferred to the reply boundary (see _rtc_up — the two-voices bug).
        self.rtc_pending = False
        # Dial-until-ready (2026-08-03): the greeting defers while the
        # transport settles. _greet_task is the deferral (None = greet ran or
        # runs inline); _greeted flips exactly once, when call_ready goes out.
        # _rtc_mic_live: the peer connection has delivered mic audio — from
        # that first frame the WS copy of the same microphone is a duplicate,
        # not a fallback, and is dropped (the doubled-mic bug). Reset by
        # _rtc_down so a mid-call transport death hands the mic back to WS.
        self._greeted = False
        self._greet_task: asyncio.Task | None = None
        self._rtc_mic_live = False
        # Bytes heard between VAD start/stop — a LENGTH, not the audio: nothing
        # is stored (ephemeral-call decision, module docstring), but the
        # sub-perceptual hallucination guard still needs to know how much sound
        # a "transcript" was born from.
        self.utterance_bytes = 0
        # The vendor VAD's start timestamp for the utterance in flight —
        # paired with speech_stopped's end timestamp, its first-hand measure
        # of the speech our byte counter can undercount (see the handler).
        self._speech_start_ms: float | None = None
        self.capturing = False
        # The reply being spoken: transcript accumulator + whether audio is
        # mid-segment on the wire.
        self.reply_text = ""
        # The previous reply's text — playback outlives generation, so the
        # echo guard must be able to match against words whose response
        # already finished.
        self.last_reply_text = ""
        self.audio_open = False
        self.response_active = False
        # Milliseconds of audio the vendor has sent for the CURRENT reply.
        # Reset when the next reply's item is announced, not when a response
        # starts — at `response.created` the item still being cut off is the
        # PREVIOUS one, and it is that one which needs correcting.
        self._reply_fed_ms = 0.0
        # Turn-engine state: did the current speech begin over a live reply
        # (captured at speech_started), and the answer-from-audio fallback
        # armed while a transcript is awaited.
        self.speech_over_reply = False
        self._transcript_fallback: asyncio.Task | None = None
        # The transcript's DELTAS, accumulated: the vendor sometimes never
        # sends the completed event (live twice, 2026-08-01), and what was
        # already streamed is a better answer source than raw audio.
        self._pending_transcript = ""
        # After a supersede, audio frames of the CANCELLED reply are still in
        # flight — and the rtc audio handler resumes playback per frame, so
        # without this the dead reply's tail would play anyway. True = drop
        # audio until the NEXT response begins.
        self.discard_stale_audio = False
        # The embargo's lifecycle guards (2026-08-03, the mute-forever fix):
        # when it was raised, so the watchdog can expire one whose lifting
        # event never came (_EMBARGO_MAX_S); and whether the vendor refused
        # the supersede's re-ask (response_rejected) — in which case the
        # gateway re-asks itself the moment the blocking response finishes.
        self._embargo_since: float | None = None
        self._recreate_after_done = False
        # The go-silent rescue (2026-08-03 — see _REPLY_START_MAX_S): when we
        # asked for a reply that has not begun, and whether the one permitted
        # re-ask has been spent on it.
        self._awaiting_reply_since: float | None = None
        self._reask_done = False
        # The last customer turn we ANSWERED, and when — the duplicate guard's
        # reference (see _near_repeat).
        self._answered_words: list[str] = []
        self._answered_at: float = 0.0
        # Whether the reply now being awaited answers a NEW CUSTOMER TURN, as
        # opposed to continuing the current one (a tool follow-up). Only the
        # former may silence audio still playing — see response_started.
        self._reply_for_new_turn = False
        # ONE LANGUAGE PER CALL (product decision 2026-08-03, superseding
        # 2026-08-01's per-transcript re-pinning). The call's language is
        # decided ONCE — by the seeded thread at bootstrap (_seed_pin), else
        # by the first CLEAR utterance (_lock_verdict: ≥2 words, letter-
        # majority script) — and then NEVER moves, however the customer
        # speaks. A mid-call switch is understood and answered in the call's
        # language, without comment (the persona says so). The pin is still
        # never None ("en" from the first breath) and EVERY response carries
        # it: customer replies, tool follow-ups, guard redos, the nudge, the
        # goodbye. The model's own judgment never outranks it. What the lock
        # buys beyond predictability: no transcript, however garbled — and
        # noise-burst hallucinations arrive in ANY script — can flip the
        # call's language again. Known cost, accepted with the decision: a
        # Gulf speaker whose first clear transcript comes back ROMANIZED
        # locks English and stays there for the whole call.
        self.reply_lang_pin: str = "en"
        self.lang_locked = False
        self._lang_checked = False
        self._lang_retry_done = False
        # The reference guard (2026-07-31, the phantom-confirmation incident):
        # every booking reference a tool result or the seeded thread ever
        # contained. A TD- reference in her reply that is NOT in this set was
        # invented — the model once "confirmed" a booking that never existed
        # and later "cancelled" TD-2026-0307, a ref it composed from a slot
        # id. Facts come from tools (rule 2); this makes the reference clause
        # of that rule structural for the spoken channel.
        self.known_refs: set[str] = set()
        self._ref_retry_done = False
        self._ref_warned = False
        # The mobile evidence guard (2026-07-31, the invented-identity
        # incident): the model once passed find_or_create_customer a name,
        # title and mobile the customer NEVER SAID — one second after the
        # licence step, no exchange in between — then recorded consent nobody
        # gave and created a real booking for a fabricated person. Identity
        # is the chokepoint (no customer → no consent row, no booking), so a
        # mobile in any tool argument must have been HEARD: this is every
        # digit the customer's own utterances produced this call, plus the
        # typed thread the call resumed. In-memory only, dies with the call —
        # no new retention surface in ephemeral mode.
        self.heard_digits = ""
        # Consent needs its own answer (rule 9, enforced): how many customer
        # utterances had arrived when identity was last established — a
        # consent grant recorded before the customer spoke AGAIN cannot be
        # the answer to a consent question they were actually asked.
        self.customer_turns = 0
        self.turns_at_identity: int | None = None
        # The slot provenance guard (2026-07-31, closing the reference
        # guard's sibling hole): every slot id a SUCCESSFUL tool result ever
        # named. lock_slot/create_booking with a slot id that never appeared
        # in one would book the customer a time nobody discussed. Successful
        # results only — a failed lock echoes the model's own argument back
        # ({"locked": false, ..., "slot_id": <echo>}), and harvesting that
        # would let the model launder an invented id through one failure.
        self.known_slot_ids: set[int] = set()
        # The price guard's whitelist: every amount (>= 1000, digit form) a
        # tool result or the seeded thread contained. Digit-form AED claims
        # in her replies are checked against it; word-form prices
        # ("ninety-nine thousand") are beyond a stream guard and stay
        # compliance-only — the guard is conservative by design, because a
        # guard that kills correct replies is worse than the misquote.
        self.known_amounts: set[int] = set()
        self._price_retry_done = False
        self._price_warned = False
        # Trusted context for tool dispatch — resolved from tool RESULTS
        # (find_or_create_customer), exactly as graph state does it. Never
        # from the model's arguments.
        self.customer_id: int | None = None
        self.pending_close = False
        # The spoken idle nudge (rule 19's first rung, 2026-08-01): ONE
        # "are you still there?" per silence stretch, at CALL_NUDGE_SECONDS.
        # The nudge's own reply must not stamp last_activity — our line is
        # not the customer's presence, and restarting the clock would
        # postpone the silence hangup the nudge precedes (the chat ladder's
        # exact rule, spoken).
        self._nudge_done = False
        self._suppress_activity_stamp = False
        # The silence hangup's warm goodbye (2026-08-01): armed at this
        # monotonic time; the drain ends the call as _close_reason. None =
        # not armed. The customer speaking disarms it with pending_close.
        self._silence_farewell_at: float | None = None
        self._close_reason = "closed"
        # What the call amounted to — for the ONE summary row it leaves behind.
        self.exchanges = 0
        self.booking_refs: list[str] = []
        self.started_mono = time.monotonic()
        self.last_activity_mono = self.started_mono
        # When her voice will finish LEAVING (not finish generating). The idle
        # ladder counts from whichever of the two of them last made a sound —
        # see _extend_speaking for why generation end is the wrong anchor.
        self.speaking_until_mono = self.started_mono
        self.ended = False
        self.end_reason: str | None = None
        # LangSmith voice-turn tracing (2026-08-04, see services/call_trace).
        # The realtime call never touches wrap_anthropic, so its turns are
        # invisible in LangSmith unless emitted here. We accumulate ONE run per
        # CUSTOMER TURN — the utterance, every reply it drew (tool follow-ups
        # and guard redos concatenated), and the tools it invoked — and flush
        # it when the NEXT turn begins or the call ends, into the chat's project
        # grouped by session_id. Purely a diagnostic mirror: nothing here feeds
        # the turn engine, and it is inert unless LANGSMITH_TRACING is on.
        self._trace_customer: str | None = None
        self._trace_replies: list[str] = []
        self._trace_tools: list[str] = []
        # CALL_TRACE_AUDIO extension (2026-08-04, demo): when audio tracing is
        # on we also buffer each turn's PCM — the caller's utterance (inbound,
        # at _on_pcm_24k while capturing) and Aisha's reply (the outbound
        # `audio` frames) — and attach them as WAV to the turn's run so the
        # audio-native voice evaluators have something to judge. Captured once
        # per session into a bool so an untraced call never checks the flag
        # again; bounded so a long turn cannot grow memory without limit;
        # dropped at hangup (nothing reaches our DB).
        self._trace_audio = call_trace.audio_enabled()
        self._trace_cust_pcm = bytearray()          # current utterance, inbound
        self._trace_customer_pcm_answered: bytes | None = None  # snapshot at turn
        self._trace_agent_pcm = bytearray()          # her reply(ies), this turn

    # ---------------------------------------------------------------- sending
    def _put(self, item: tuple) -> None:
        self.out.put_nowait(item)

    def _send_json(self, payload: dict) -> None:
        self._put(("json", payload))

    def _set_state(self, value: str) -> None:
        self.state = value
        self._send_json({"type": "state", "value": value})

    async def _sender(self) -> None:
        while True:
            kind, payload = await self.out.get()
            try:
                if kind == "json":
                    await self.ws.send_json(payload)
                elif kind == "bytes":
                    await self.ws.send_bytes(payload)
                elif kind == "close":
                    await self.ws.close()
                    return
            except Exception:  # noqa: BLE001 — socket already dead
                return

    def _end(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        if self.audio_open:
            self._send_json({"type": "pcm_end"})
            self.audio_open = False
        self._send_json({"type": "call_ended", "reason": reason})
        self._put(("close", None))

    async def _watchdog(self) -> None:
        while not self.ended:
            await asyncio.sleep(1)
            self._watchdog_tick(time.monotonic())

    def _watchdog_tick(self, now: float) -> None:
        """One clock check — split from the loop so the ladder is testable.
        The idle ladder, spoken (rule 19): a nudge at CALL_NUDGE_SECONDS, a
        WARM GOODBYE at CALL_SILENCE_HANGUP_SECONDS (2026-08-01 — the line
        used to just drop), and a hard stop if that goodbye never finishes.
        Neither rung stamps activity, so each fires from the same anchor."""
        if self.ended:
            return
        # The embargo's safety net (the mute-forever fix, 2026-08-03): an
        # embargo is lifted by the next response_started, an event the vendor
        # does not guarantee. One stuck longer than _EMBARGO_MAX_S is expired
        # here — seconds of possibly-stale audio beat a call gone mute for
        # good. The known refusal race recovers precisely via
        # response_rejected; this catches every cause nobody has seen yet.
        if (
            self.discard_stale_audio
            and self._embargo_since is not None
            and now - self._embargo_since > _EMBARGO_MAX_S
        ):
            log.warning(
                "stale-audio embargo expired unlifted — clearing (session=%s)",
                self.session_id[:8],
            )
            self.discard_stale_audio = False
            self._embargo_since = None
        # The owed reply that never began (the go-silent fix, 2026-08-03 — see
        # _REPLY_START_MAX_S). Ordered before the idle ladder because a stuck
        # turn holds the state at "thinking", which is exactly what stops the
        # ladder below from ever running.
        if (
            self._awaiting_reply_since is not None
            and not self.response_active
            and now - self._awaiting_reply_since > _REPLY_START_MAX_S
        ):
            self._rescue_owed_reply(now)
            return
        if now - self.started_mono > CALL_MAX_MINUTES * 60:
            self._end("max_minutes")
            return
        if self._silence_farewell_at is not None:
            # The goodbye is in flight; _end_after_drain ends the call when
            # it finishes. If it never does — vendor dead, response lost —
            # nobody is held on a silent line forever.
            if now - self._silence_farewell_at > 25:
                self._end("silence")
            return
        idle = self.state == "listening" and not self.response_active
        if not idle or self.pending_close:
            return
        # SILENCE MEANS NEITHER OF THEM IS MAKING A SOUND, so both rungs count
        # from whichever spoke last — the customer, or Aisha's audio finishing
        # its way out. Anchored on last_activity alone (i.e. on GENERATION
        # end), a long reply drew "Are you still with me?" over her own voice:
        # measured 2026-08-04, a 30-word reply at 08:03:28 nudged at 08:03:41,
        # mid-sentence. Both rungs move together and keep the SAME anchor, so
        # the nudge still cannot postpone the hangup it precedes.
        quiet_since = max(self.last_activity_mono, self.speaking_until_mono)
        if now - quiet_since > CALL_SILENCE_HANGUP_SECONDS:
            self._silence_goodbye(now)
        elif not self._nudge_done and now - quiet_since > CALL_NUDGE_SECONDS:
            self._nudge()

    def _rescue_owed_reply(self, now: float) -> None:
        """A customer turn was asked for and nothing came back. Ask once more
        under the standing pin; if that is also unanswered, stop waiting and
        put the call back in the idle ladder's hands. Deliberately NOT an
        endless re-ask: whatever swallowed the first two creates would swallow
        the rest, and a call that answers nothing should reach its own warm
        goodbye rather than hold a silent line open."""
        session = self.session
        if self._reask_done or session is None:
            log.warning(
                "no reply after the re-ask — releasing the call to the idle "
                "ladder (session=%s)",
                self.session_id[:8],
            )
            self._awaiting_reply_since = None
            # Whatever the dead reply left raised must come down with it, or
            # the customer's next turn plays into a muted channel.
            self.discard_stale_audio = False
            self._embargo_since = None
            if self.state != "listening":
                self._set_state("listening")
            return
        self._reask_done = True
        self._awaiting_reply_since = now
        log.warning(
            "reply never started after %.0fs — re-asking once (session=%s)",
            _REPLY_START_MAX_S, self.session_id[:8],
        )
        pin = self.reply_lang_pin
        note = self._first_exchange_note()
        asyncio.get_running_loop().create_task(
            session.request_response(pin, note=note)
        )

    def _silence_goodbye(self, now: float) -> None:
        """The silence hangup's last words: one warm goodbye, spoken, then
        the ordinary pending-close drain ends the call as 'silence'. The
        customer speaking mid-goodbye disarms it like any farewell."""
        session = self.session
        if session is None:
            self._end("silence")
            return
        self._silence_farewell_at = now
        self.pending_close = True
        self._close_reason = "silence"
        log.info(
            "silence hangup — warm goodbye first (session=%s)", self.session_id[:8]
        )
        # An exact line, like the nudge — see the note there.
        line = (
            "يبدو أنك مشغول الآن — شكراً لاتصالك بإيليت جروب، واتصل فينا "
            "متى ما حبيت. مع السلامة!"
            if self.reply_lang_pin == "ar" else
            "It seems you're busy right now — thank you for calling Elite "
            "Group, and please call back anytime. Goodbye!"
        )
        note = (
            "Say EXACTLY this, warmly, and then stop — nothing before it, "
            f"nothing after it: \"{line}\""
        )
        asyncio.get_running_loop().create_task(
            session.request_response(self.reply_lang_pin, note=note, tools_off=True)
        )

    def _nudge(self) -> None:
        """Rule 19's first rung, spoken (2026-08-01): after CALL_NUDGE_SECONDS
        of customer silence, one warm check-in — once per silence stretch.
        The reply rides a one-shot note under the standing language pin, and
        its completion is barred from stamping last_activity (see
        _finish_response), so the silence hangup still fires on schedule if
        the customer really is gone. The customer answering re-arms
        everything through the ordinary activity paths."""
        session = self.session
        if session is None:
            return
        self._nudge_done = True
        self._suppress_activity_stamp = True
        log.info(
            "silence nudge — checking the caller is still there (session=%s)",
            self.session_id[:8],
        )
        # AN EXACT LINE, not a brief (2026-08-01, after two live failures):
        # asked merely to "check in", the model answered stale questions and
        # called tools instead. A verbatim sentence leaves nothing to
        # improvise; tools_off (tools [] + tool_choice none) is the second
        # lock on the same door.
        ar = self.reply_lang_pin == "ar"
        if self.muted:
            line = (
                "عذراً، ما أقدر أسمعك حالياً — يمكن المايك مقفل عندك."
                if ar else
                "Sorry, I can't seem to hear you right now — your "
                "microphone may be muted."
            )
        else:
            line = (
                "هل ما زلت معي؟" if ar else "Are you still with me?"
            )
        note = (
            "Say EXACTLY this, warmly, and then stop — nothing before it, "
            f"nothing after it: \"{line}\""
        )
        asyncio.get_running_loop().create_task(
            session.request_response(self.reply_lang_pin, note=note, tools_off=True)
        )

    # ------------------------------------------------------------ persistence
    def _store_summary_sync(self) -> None:
        """The call's ONE row (module docstring): a Speaker.system marker —
        "call:<seconds>", a FACT the client phrases per language at render
        time, never display text — anchoring any booking refs the call
        produced through the ordinary `cards` union, so the booking card
        rebuilds through the gated /api/booking exactly like a typed one."""
        if self.exchanges == 0 and not self.booking_refs:
            return  # a call where nothing happened leaves nothing behind
        db = SessionLocal()
        try:
            db.add(ConversationTurn(
                session_id=self.session_id,
                speaker=Speaker.system,
                lang=Language.en,
                text=f"call:{int(time.monotonic() - self.started_mono)}",
                cards=[{"booking_refs": self.booking_refs}] if self.booking_refs else [],
            ))
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("call summary store failed — session=%s", self.session_id)
            db.rollback()
        finally:
            db.close()

    def _store_call_turn_sync(self, speaker: Speaker, text: str, lang_code: str) -> None:
        """Persist ONE spoken call turn as an ordinary ConversationTurn, so the
        chat thread can show the FULL call after it ends. This re-enables the
        per-turn record the 2026-07-29 decision removed (see the module docstring)
        — turned back on deliberately for the read-back-the-call feature. Same
        fail-open posture as _store_summary_sync: a DB error is logged and dropped,
        never allowed to disturb the live call."""
        text = (text or "").strip()
        if not text:
            return
        db = SessionLocal()
        try:
            db.add(ConversationTurn(
                session_id=self.session_id,
                speaker=speaker,
                lang=Language.ar if lang_code == "ar" else Language.en,
                text=text,
            ))
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("call turn store failed — session=%s", self.session_id[:8])
            db.rollback()
        finally:
            db.close()

    def _store_call_turn(self, speaker: Speaker, text: str, lang_code: str) -> None:
        """Persist a call turn WITHOUT blocking the audio loop: the DB write runs
        on a worker thread. No-op once the call has ended or the text is empty."""
        if self.ended or not (text or "").strip():
            return
        try:
            asyncio.get_running_loop().run_in_executor(
                None, self._store_call_turn_sync, speaker, text, lang_code)
        except RuntimeError:  # no running loop — write inline rather than lose it
            self._store_call_turn_sync(speaker, text, lang_code)
    # ------------------------------------------------------- langsmith tracing
    def _begin_voice_trace(self, customer_text: str) -> None:
        """A new ANSWERED customer turn is starting: close out the previous
        turn's run (all its replies are in) and open this one. Called only
        from the real-turn path of _decide_turn — backchannels, one-word
        non-interrupts and duplicate transcripts never reach it, so they never
        become their own runs. Inert unless tracing is on."""
        if not call_trace.trace_enabled():
            return
        self._flush_voice_trace()
        self._trace_customer = customer_text
        self._trace_replies = []
        self._trace_tools = []
        if self._trace_audio:
            # The utterance just answered IS this turn's caller audio (captured
            # between its speech_started and now); her reply audio accumulates
            # from here until the turn flushes.
            self._trace_customer_pcm_answered = bytes(self._trace_cust_pcm)
            self._trace_agent_pcm = bytearray()

    def _flush_voice_trace(self) -> None:
        """Emit the pending customer turn (utterance + every reply it drew +
        the tools it used) as one LangSmith run, then reset. A no-op when
        tracing is off or there is no pending customer utterance to score (the
        greeting and bare nudges answer no one). Fail-open: a tracing error is
        swallowed in call_trace and can never disturb the call."""
        customer = self._trace_customer
        reply = " ".join(p for p in self._trace_replies if p).strip()
        tools = self._trace_tools or None
        language = self.reply_lang_pin
        cust_pcm = self._trace_customer_pcm_answered
        agent_pcm = bytes(self._trace_agent_pcm) if self._trace_agent_pcm else None
        self._trace_customer = None
        self._trace_replies = []
        self._trace_tools = []
        self._trace_customer_pcm_answered = None
        self._trace_agent_pcm = bytearray()
        if not customer:
            return

        def _emit() -> None:
            call_trace.log_voice_turn(
                session_id=self.session_id,
                customer=customer,
                reply=reply,
                language=language,
                tools=tools,
                customer_pcm=cust_pcm,
                agent_pcm=agent_pcm,
            )

        # Off the audio loop: WAV-encoding and the upload enqueue must never sit
        # on the event loop mid-call. In the hangup path this already runs on a
        # worker thread (no loop) — emit inline there.
        try:
            asyncio.get_running_loop().run_in_executor(None, _emit)
        except RuntimeError:
            _emit()

    def _run_tool_sync(self, name: str, arguments: dict, call_id: str) -> dict:
        """The pipeline's exact choke point, minus the graph around it: audit
        the ask (actor=agent, correlation_id = the vendor call_id), dispatch
        with trusted context injected, return the envelope as data."""
        db = SessionLocal()
        try:
            audit_service.record(
                db,
                session=self.session_id,
                actor=Actor.agent,
                type=AuditType.tool_call,
                payload={"tool": name, "input": arguments, "engine": "realtime"},
                correlation_id=call_id or None,
            )
            refusal = self._fact_guard(name, arguments)
            if refusal is not None:
                # The attempt stays in the trail (the refused call above IS
                # the evidence of fabrication); the tool is never dispatched.
                db.commit()
                log.warning(
                    "fact guard refused %s: %s (session=%s)",
                    name, refusal["error"], self.session_id[:8],
                )
                return refusal
            # `fns` IS THE BINDING, and without it the eight tools the call now
            # OFFERS could not be RUN: _dispatch resolves against TOOL_FNS by
            # default, which holds the single-agent registry only, so
            # get_finance_estimate and its siblings would come back
            # `unknown_tool` after the model had already said "let me check".
            # CALL_TOOL_FNS names exactly what _openai_tools() offered, so the
            # two cannot disagree — anything else still falls through to
            # unknown_tool, which is the allowlist this path has always had.
            result = _dispatch(
                db, self.session_id, name, arguments, self.customer_id, call_id or "",
                fns=CALL_TOOL_FNS,
            )
            db.commit()
            envelope = result if isinstance(result, dict) else {"result": result}
            # SPLIT MEMORY, narrowed (2026-07-31): call content stays
            # ephemeral, but call OUTCOMES the chat agent must know — who
            # the customer is, what got booked, moved, cancelled, consented
            # — are written into the checkpoint as the tap intents write
            # theirs: a synthetic tool_use/tool_result pair, so the customer
            # can hang up, type "move it to Sunday", and not be re-asked
            # everything the call already established. Successful results
            # only (an error teaches the chat agent nothing), and NEVER
            # forget_me: erasure deletes the checkpoint, and writing a pair
            # after it would resurrect the thread we just purged.
            if name in _CHECKPOINT_TOOLS and "error" not in envelope:
                try:
                    graph = build_graph(new_sqlite_checkpointer(AGENT_CHECKPOINT_PATH))
                    graph.update_state(
                        {"configurable": {"thread_id": self.session_id}},
                        {"messages": _synthetic_pair(
                            call_id or f"call_{name}", name, arguments, envelope
                        )},
                    )
                except Exception:  # noqa: BLE001
                    # The chat agent's memory is best-effort here — a failed
                    # write must never break the live turn (the _log_usage
                    # rule).
                    log.exception(
                        "checkpoint write failed — session=%s tool=%s",
                        self.session_id, name,
                    )
            return envelope
        except Exception:  # noqa: BLE001
            log.exception("tool dispatch failed — session=%s tool=%s", self.session_id, name)
            db.rollback()
            return {"error": "tool_execution_failed", "tool": name}
        finally:
            db.close()

    def _seed_turns_sync(self) -> list[tuple[str, str]]:
        db = SessionLocal()
        try:
            rows = (
                db.query(ConversationTurn)
                .filter(
                    ConversationTurn.session_id == self.session_id,
                    # System markers (a previous call's summary row) are facts
                    # about the thread, not words anyone said — seeding
                    # "call:183" as a customer message would be nonsense.
                    ConversationTurn.speaker != Speaker.system,
                )
                .order_by(ConversationTurn.id.desc())
                .limit(_SEED_TURNS)
                .all()
            )
            return [(r.speaker.value, r.text or "") for r in reversed(rows)]
        except Exception:  # noqa: BLE001
            return []
        finally:
            db.close()

    # ---------------------------------------------------------- vendor events
    def _on_agent_event(self, event: dict) -> None:
        if self.ended:
            return
        kind = event.get("type")
        if kind == "user_speech_started":
            # A HINT, NOT AN INTERRUPT (2026-07-30). Sounds never interrupt;
            # only WORDS do, at transcript time below. The hint gets one
            # instant, fully recoverable reaction on the rtc path: HOLD the
            # playback, so the customer hears her go quiet the moment they
            # start talking. If the sound turns out to be nothing — echo
            # residue, a breath, a chair — the transcript decision resumes
            # exactly where she paused, and no reply was harmed.
            self.capturing = True
            self.utterance_bytes = 0  # a fresh turn's evidence starts at zero
            if self._trace_audio:
                self._trace_cust_pcm = bytearray()  # a fresh utterance's audio
            self._speech_start_ms = event.get("at_ms")
            self._pending_transcript = ""
            self.last_activity_mono = time.monotonic()
            # _nudge_done deliberately NOT reset here: a VAD trigger is a
            # sound, not presence — see _finish_utterance.
            # Whether this speech began OVER her reply — captured NOW, at the
            # honest moment, because playback may finish draining before the
            # transcript arrives to act on it.
            self.speech_over_reply = (
                self.response_active
                or self.audio_open
                or bool(
                    self.rtc is not None
                    and self.rtc.track is not None
                    and self.rtc.track.queued_ms > 0
                )
            )
            if self.rtc_active and self.rtc is not None and self.rtc.track is not None:
                self.rtc.track.pause()
            log.info(
                "vendor heard speech start (over her reply: %s) (session=%s)",
                self.speech_over_reply, self.session_id[:8],
            )
        elif kind == "user_speech_stopped":
            # The vendor committed a segment of audio. With create_response
            # false that DECIDES nothing — the words do, when the transcript
            # lands. Here: drop obvious blips, and arm the fallback that
            # answers from audio alone if the transcript never comes.
            self.capturing = False
            self.last_activity_mono = time.monotonic()
            # THE VENDOR'S OWN DURATION OUTRANKS OUR BYTE COUNT (live
            # 2026-08-01): counting starts at ITS speech_started event, so
            # audio forwarded during that round-trip is missed — a real
            # Arabic utterance once counted near zero and its transcript
            # was silently dropped as 'hallucination'. Its VAD measured the
            # speech first-hand; 48 bytes/ms = PCM16 mono at 24kHz.
            end_ms = event.get("at_ms")
            if isinstance(end_ms, (int, float)) and isinstance(
                self._speech_start_ms, (int, float)
            ):
                vendor_ms = max(0.0, float(end_ms) - float(self._speech_start_ms))
                self.utterance_bytes = max(self.utterance_bytes, int(vendor_ms * 48))
            if self.utterance_bytes < _MIN_UTTERANCE_BYTES:
                log.info(
                    "utterance dropped as blip: %s bytes of evidence (session=%s)",
                    self.utterance_bytes, self.session_id[:8],
                )
                self._resume_playback()
                return
            if self.state == "listening":
                self._set_state("thinking")
            self._arm_transcript_fallback()
        elif kind == "user_transcript_delta":
            # Transcription only runs on COMMITTED audio, so a delta proves
            # an utterance ended vendor-side — even when the speech events
            # or the completed transcript never arrive (live 2026-08-01).
            # Accumulate the text and make sure the fallback is armed to
            # answer from it.
            self._pending_transcript += event.get("text") or ""
            if self._transcript_fallback is None and not self.response_active:
                self._arm_transcript_fallback()
        elif kind == "user_transcript":
            heard_bytes, self.utterance_bytes = self.utterance_bytes, 0
            self._pending_transcript = ""
            self.last_activity_mono = time.monotonic()
            self._cancel_transcript_fallback()
            self._decide_turn(heard_bytes, event.get("text") or "")
        elif kind == "response_started":
            # A reply exists from this moment — audio arrives seconds later.
            # Judging "in flight" by audio alone left generation-phase
            # interruptions uncancelled (the she-finishes-the-old-reply-first
            # bug, live 2026-07-30). This reply is current, so any stale-audio
            # embargo from a supersede ends here — and the language guard
            # gets a fresh verdict to make.
            self.response_active = True
            self.discard_stale_audio = False
            self._embargo_since = None
            self._recreate_after_done = False  # a reply exists — nothing to redo
            self._awaiting_reply_since = None  # the owed reply arrived
            # ONE VOICE AT A TIME (2026-08-03, the two-voices bug). A reply to
            # a NEW customer turn silences whatever is still playing: the
            # supersede path already does this, but a second reply can also
            # begin without one (a duplicate transcript did, live — two of her
            # voices at once). Restricted to new turns because a TOOL FOLLOW-UP
            # continues the same answer, and clipping it would cut "one moment,
            # let me check" off mid-word.
            if self._reply_for_new_turn:
                self._reply_for_new_turn = False
                self._silence_playback("a new turn's reply beginning")
            self._lang_checked = False
            self._ref_warned = False
            self._price_warned = False
        elif kind == "reply_item":
            # A NEW reply owns her voice from here. Only now is the previous
            # one's byte count spent — `response_started` is too early, because
            # the reply being silenced at that moment is still the old one.
            self._reply_fed_ms = 0.0
        elif kind == "assistant_delta":
            self.reply_text += event.get("text") or ""
            self._send_json({"type": "token", "text": event.get("text") or ""})
            self._language_guard()
            self._reference_guard()
            self._price_guard()
        elif kind == "audio":
            if self.discard_stale_audio:
                return  # the tail of a cancelled reply — never let it play
            self.response_active = True
            self._extend_speaking(event["data"])
            # Buffer her spoken audio for the current turn's trace — only while a
            # turn is open (skips the greeting and bare nudges, which answer no
            # one), bounded like the caller side.
            if (
                self._trace_audio
                and self._trace_customer is not None
                and len(self._trace_agent_pcm) < _MAX_TRACE_AUDIO_BYTES
            ):
                self._trace_agent_pcm.extend(event["data"])
            # 24 kHz, 16-bit mono = 48 bytes per millisecond. This is the
            # numerator of every truncation: it can never exceed what the
            # vendor generated, because it IS what the vendor sent.
            self._reply_fed_ms += len(event["data"]) / 48.0
            if self.rtc_active and self.rtc is not None and self.rtc.track is not None:
                # Server playback: her voice goes down the peer connection —
                # AEC3-cancelled at the browser — never as WS bytes.
                if self.state != "speaking":
                    self._set_state("speaking")
                self.rtc.track.resume()
                self.rtc.track.play(event["data"])
            else:
                if not self.audio_open:
                    self.audio_open = True
                    if self.state != "speaking":
                        self._set_state("speaking")
                    self._send_json({"type": "pcm_start", "rate": VENDOR_SAMPLE_RATE})
                self._put(("bytes", event["data"]))
        elif kind == "function_call":
            asyncio.get_running_loop().create_task(
                self._handle_function_call(
                    event.get("name") or "",
                    event.get("arguments") or {},
                    event.get("call_id") or "",
                )
            )
        elif kind == "response_rejected":
            # The vendor refused our response.create: another response was
            # still active (the benign already-active race). Harmless — except
            # when the refused create was a SUPERSEDE's replacement: its
            # embargo would then never lift and the call went mute for good
            # (the mute-forever bug, 2026-08-03). Remember to ask again the
            # moment the blocking response finishes.
            if self.discard_stale_audio and not self.ended:
                self._recreate_after_done = True
        elif kind == "response_done":
            self._finish_response()
            if self._recreate_after_done and not self.ended and not self.response_active:
                # The blocking response is gone — re-issue the refused re-ask
                # under the standing pin. The embargo stays up until the
                # recreated reply's response_started lifts it, so none of the
                # dead reply's tail can play in the gap.
                self._recreate_after_done = False
                session = self.session
                if session is not None:
                    log.info(
                        "re-asking after refused supersede create (session=%s)",
                        self.session_id[:8],
                    )
                    note = self._first_exchange_note()
                    pin = self.reply_lang_pin
                    asyncio.get_running_loop().create_task(
                        session.request_response(pin, note=note)
                    )
        elif kind in ("agent_error", "agent_closed"):
            # Unlike the STT relay there is no engine to fall back to inside
            # this mode — the pipeline is a config flip away, not a runtime
            # one. An honest end beats a dead line. rate_limited keeps its
            # name all the way to the client: "call again in a minute" is
            # advice "we hit a problem" cannot give.
            log.warning(
                "realtime agent lost (%s) — session=%s",
                event.get("detail", kind), self.session_id[:8],
            )
            self._end(
                "rate_limited" if event.get("detail") == "rate_limited" else "error"
            )

    # ------------------------------------------------------- the turn engine
    def _start_embargo(self) -> None:
        """Raise the stale-audio embargo, STAMPED — so the watchdog can
        expire one whose lifting event (the next response_started) never
        arrives, instead of the call going mute for good (the mute-forever
        fix, 2026-08-03)."""
        self.discard_stale_audio = True
        self._embargo_since = time.monotonic()

    def _extend_speaking(self, pcm: bytes) -> None:
        """Push out the moment her voice will FINISH LEAVING, by this chunk's
        real duration.

        ⚠️ SHE GENERATES FASTER THAN SHE TALKS, and the idle ladder used to
        forget it. `response_done` means the TEXT AND AUDIO ARE GENERATED, not
        spoken: a ~12-second reply arrives in a few seconds and is then paced
        out at conversational speed, so `_finish_response` stamped
        last_activity and the silence clock began ticking while she was still
        mid-sentence. Measured live 2026-08-04: a 30-word reply logged at
        08:03:28 drew "Are you still with me?" at 08:03:41 — exactly
        CALL_NUDGE_SECONDS after GENERATION, and inside her own speech.

        Derived from BYTES rather than the rtc track's `queued_ms` so one
        mechanism covers both transports: the WS-PCM fallback has no
        server-side queue at all (the client schedules playback), and two
        estimates that can disagree is how this class of bug returns.
        """
        if not pcm:
            return
        seconds = len(pcm) / (VENDOR_SAMPLE_RATE * 2)  # 16-bit mono
        now = time.monotonic()
        self.speaking_until_mono = max(self.speaking_until_mono, now) + seconds

    def _played_ms(self) -> int:
        """How much of the CURRENT reply the customer has actually heard.

        Exact on the rtc path, because playback is ours: the track's own queue
        holds precisely what has not been sent yet, so fed minus queued is
        heard. On the WS fallback the client schedules playback and no such
        queue exists, so it falls back to the wall clock the idle ladder
        already keeps (`speaking_until_mono` = when her queued speech runs
        out). Estimated there, and knowingly so — an estimate that says she
        was cut off is still infinitely closer to the truth than the silence
        we send today.

        Clamped to what the vendor actually sent us, which is what keeps
        `audio_end_ms` inside the item's real duration in every case."""
        fed = self._reply_fed_ms
        if fed <= 0:
            return 0
        if self.rtc_active and self.rtc is not None and self.rtc.track is not None:
            remaining = float(self.rtc.track.queued_ms)
        else:
            remaining = max(0.0, self.speaking_until_mono - time.monotonic()) * 1000.0
        return max(0, min(int(fed), int(fed - remaining)))

    def _truncate_reply(self, why: str = "?") -> None:
        """THE CORRECTION THAT MAKES AN INTERRUPTION HONEST. Called wherever
        her audio is killed, BEFORE the queue is cleared — once it is cleared
        there is no longer any way to know what was heard.

        `why` NAMES THE CALLER, and it is not decoration (2026-09-07). Three
        paths silence her — a supersede, a new turn's reply beginning, and the
        client's own barge detector — and only the supersede logged anything of
        its own, so a truncation from either of the other two arrived as a bare
        line with no cause attached. A goodbye cut off at 420 ms on a live call
        could not be attributed afterwards: two explanations fitted the log
        equally well and nothing in it could separate them. The log line is
        written asynchronously, after the code that scheduled it, so ORDER
        cannot be used to infer the caller either — the name has to travel with
        the call.
        """
        session = self.session
        if session is None:
            return
        fed, played = self._reply_fed_ms, self._played_ms()
        self._reply_fed_ms = 0.0
        # Nothing heard -> nothing to correct (and audio_end_ms=0 is an error).
        # ALL of it heard -> also nothing to correct, and this is the ordinary
        # case: _silence_playback is idempotent and runs on every new turn, so
        # without this a reply that finished cleanly would be "truncated" at its
        # own full length on the next turn — a pointless round trip sitting one
        # rounding error away from the one error the vendor rejects.
        if played <= 0 or played >= fed:
            return

        async def _go() -> None:
            try:
                if await session.truncate_reply(played):
                    log.info(
                        "reply truncated at %s ms by %s — she said only what was "
                        "heard (session=%s)", played, why, self.session_id[:8],
                    )
            except Exception:  # never let a correction kill a live call
                log.warning("truncate failed (session=%s)", self.session_id[:8])

        try:
            asyncio.get_running_loop().create_task(_go())
        except RuntimeError:
            pass

    def _silence_playback(self, why: str = "?") -> None:
        """Stop her voice everywhere it might still be sounding: the server's
        own send queue, the peer connection's jitter buffer, and the client's
        WS player. Idempotent — clearing an empty queue costs nothing, which
        is what lets it be called on every new turn's reply."""
        # FIRST, while the queue still says what went unheard: tell the vendor
        # where her voice really stopped. Everything below destroys that
        # evidence.
        self._truncate_reply(why)
        # The unplayed remainder is gone, so the silence clock must start NOW.
        # Without this a barged-in 15-second reply would keep the ladder
        # asleep for 15 seconds of real, audible silence.
        self.speaking_until_mono = time.monotonic()
        if self.rtc is not None and self.rtc.track is not None:
            self.rtc.track.clear()
        if self.audio_open:
            self.audio_open = False
            self._send_json({"type": "pcm_end"})
        self._send_json({"type": "stop_audio"})

    def _resume_playback(self) -> None:
        """The sound was nothing: pick the reply back up where the hint
        paused it, and put the state back where the playback says it is."""
        if self.rtc is not None and self.rtc.track is not None:
            self.rtc.track.resume()
        self.speech_over_reply = False
        if self.state == "thinking" and not self.response_active:
            self._set_state("listening")

    def _respond(self, *, supersede: bool) -> None:
        """The ONLY place a reply is requested for a customer turn — with
        create_response false the vendor never auto-answers, so what gets a
        response is entirely this engine's decision. Supersede = the turn
        arrived over a live reply: stop its playback everywhere and cancel it
        BY NAME before asking for the new one. The reply's language is the
        call's locked pin — one language per call (2026-08-03); nothing a
        transcript contains re-decides it here."""
        if supersede:
            log.info(
                "interruption in words — superseding the reply (session=%s)",
                self.session_id[:8],
            )
            # Frames of the superseded reply still in flight must die on
            # arrival — the embargo lifts when the NEXT reply begins.
            self._start_embargo()
            self._silence_playback("an interruption superseding the reply")
            self._maybe_promote_rtc()
        self.speech_over_reply = False
        if self.state != "thinking":
            self._set_state("thinking")
        self._lang_retry_done = False
        self._ref_retry_done = False
        self._price_retry_done = False
        # This turn is now owed a reply — the watchdog rescues it if none ever
        # begins (the go-silent bug). A fresh customer turn earns a fresh
        # re-ask budget.
        self._awaiting_reply_since = time.monotonic()
        self._reask_done = False
        self._reply_for_new_turn = True
        # A customer turn's reply is real activity — only the NUDGE's own
        # reply is barred from stamping (a superseded nudge must not leave
        # the bar hanging over the reply that displaced it).
        self._suppress_activity_stamp = False
        session = self.session
        if session is None:
            return
        cancel_first = supersede and self.response_active
        self.response_active = False if supersede else self.response_active
        # The double-greeting bug (live 2026-07-31): a caller who speaks OVER
        # the greeting cancels it, and a cancelled response leaves nothing in
        # the vendor conversation — so the replacement reply saw an empty
        # thread and greeted again. Any first-exchange re-request carries the
        # no-regreet note (shared with the guards' redos — see
        # _first_exchange_note, added after the language guard's redo
        # regreeted the same way, live 2026-08-01).
        note = self._first_exchange_note() if supersede else None

        pin = self.reply_lang_pin

        async def _go() -> None:
            if cancel_first:
                await session.cancel_response()
            # ALWAYS pinned — no response is ever requested without an
            # explicit language (the policy's enforcement half).
            await session.request_response(pin, note=note)

        asyncio.get_running_loop().create_task(_go())

    def _language_guard(self) -> None:
        """The pin's enforcement (2026-07-31; hardened 2026-08-01 with the
        absolute policy). Her words stream to us as text alongside the
        audio, so the first ~20 letters are majority-script-checked against
        the pin — a match costs nothing; a mismatch is cancelled within its
        first breath and redone under the same pin. ONE retry, then it
        plays with a warning: a wrong-language reply beats a dead line. The
        model's insistence never flips the pin — the adoption ladder that
        once allowed that let the model out-vote the script rule, which is
        how a muted English call got an Arabic nudge. Script decides;
        nothing else votes."""
        if self._lang_checked or self.ended:
            return
        # Letter counts only (_AR_LETTER_RE): a number read back in Eastern
        # digits must not tip an English reply's script judgment.
        arabic = len(_AR_LETTER_RE.findall(self.reply_text))
        latin = len(_LATIN_LETTER_RE.findall(self.reply_text))
        if arabic + latin < 20:
            return  # too few letters to judge — brand names open Arabic replies
        self._lang_checked = True
        spoken = "ar" if arabic >= latin else "en"
        if spoken == self.reply_lang_pin:
            return
        if self._lang_retry_done:
            log.warning(
                "language guard: mismatch persists against pin=%s — letting it play (session=%s)",
                self.reply_lang_pin, self.session_id[:8],
            )
            return
        self._lang_retry_done = True
        log.warning(
            "language guard: reply began in %s against pin=%s — redoing (session=%s)",
            spoken, self.reply_lang_pin, self.session_id[:8],
        )
        # The redo must not narrate itself (live 2026-08-03: the model, seeing
        # its own cancelled Arabic opening, explained "I'll keep going in
        # English" — remarking on the very switch the persona says never to
        # remark on). The note rides only this one response.
        self._kill_and_redo_reply(
            "Answer what the customer said naturally, in the required "
            "language. Do NOT mention language and do NOT say you are "
            "switching, staying, or continuing in any language — just answer."
        )

    def _reference_guard(self) -> None:
        """Rule 2's reference clause, made structural for the spoken channel
        (2026-07-31). The model once told a customer they were booked off a
        bare slot hold, then 'cancelled' TD-2026-0307 — a reference it
        composed from a slot id; no booking existed. A TD- reference in her
        reply that no tool result (and no seeded turn) ever contained is an
        invention, and an invention must be UNFINISHABLE, not discouraged:
        the reply dies mid-breath and is redone with a corrective note naming
        the true state. The language guard's budget, for the same reason —
        one retry, then it plays with a warning (a wrong word beats a dead
        line, and the note usually lands on the first redo)."""
        if self.ended or not self.reply_text:
            return
        fake = next(
            (r for r in _harvest_refs(self.reply_text) if r not in self.known_refs),
            None,
        )
        if fake is None:
            return
        if self._ref_retry_done:
            if not self._ref_warned:
                self._ref_warned = True
                log.warning(
                    "reference guard: unknown ref %s persists — letting it play (session=%s)",
                    fake, self.session_id[:8],
                )
            return
        self._ref_retry_done = True
        log.warning(
            "reference guard: reply spoke unknown booking ref %s — redoing (session=%s)",
            fake, self.session_id[:8],
        )
        self._kill_and_redo_reply(note=(
            f"CORRECTION: you were about to say booking reference {fake}, "
            "which does not exist — no tool returned it. Only references "
            "returned by tool results are real. If create_booking has not "
            "returned a reference in this conversation, nothing is booked "
            "yet: say so honestly and continue from the true state. Never "
            "state or invent a reference."
        ))

    def _price_guard(self) -> None:
        """Rule 2's price clause (FR-CX-07), conservative by design. Only
        DIGIT-FORM amounts in an explicit AED/dirham context are judged —
        word-form prices ('ninety-nine thousand') are beyond a stream guard
        and stay compliance-only. An amount no tool result (and no seeded
        turn) ever contained is an invention: killed and redone, one retry,
        then it plays with a warning — the reference guard's budget, for
        the same reason. The AED context is what keeps phone numbers, slot
        ids and years out of the comparison."""
        if self.ended or not self.reply_text:
            return
        plain = self.reply_text.translate(_EASTERN_DIGITS)
        claimed = None
        for m in re.finditer(
            r"(?:AED|درهم|dirhams?)\s*([\d,]{4,})|([\d,]{4,})\s*(?:AED|درهم|dirhams?)",
            plain,
            re.IGNORECASE,
        ):
            if m.end() == len(plain):
                # The amount is still streaming — "AED 99,5" is not a claim
                # yet. Judged on a later delta, once a char follows it.
                continue
            try:
                n = int((m.group(1) or m.group(2)).replace(",", ""))
            except ValueError:
                continue
            if n >= 1000 and n not in self.known_amounts:
                claimed = n
                break
        if claimed is None:
            return
        if self._price_retry_done:
            if not self._price_warned:
                self._price_warned = True
                log.warning(
                    "price guard: unknown amount %s persists — letting it play (session=%s)",
                    claimed, self.session_id[:8],
                )
            return
        self._price_retry_done = True
        log.warning(
            "price guard: reply quoted unknown amount %s — redoing (session=%s)",
            claimed, self.session_id[:8],
        )
        self._kill_and_redo_reply(note=(
            f"CORRECTION: you were about to quote AED {claimed}, an amount "
            "no tool returned in this conversation. Prices come from tool "
            "results only — quote exactly what get_vehicle_details or "
            "search_vehicles returned, or say the price is not published. "
            "Never round, estimate or invent an amount."
        ))

    def _first_exchange_note(self) -> str | None:
        """The no-regreet note, due on ANY re-request while the greeting is
        the conversation so far: a cancelled response leaves nothing in the
        vendor thread, so its replacement would greet afresh. First seen on
        the supersede path (2026-07-31); the language guard's redo did the
        exact same thing a day later."""
        if self.exchanges > 1:
            return None
        return (
            "You already answered the phone and greeted the caller — do NOT "
            "greet or introduce yourself again; reply directly to what they "
            "said."
        )

    def _kill_and_redo_reply(self, note: str | None = None) -> None:
        """Shared teeth of the streaming guards: kill the reply exactly as a
        supersede would — playback, in-flight tail, and the generation
        itself — then re-ask under the same language pin, with an optional
        one-shot corrective note riding that response's instructions. The
        first-exchange no-regreet note is appended whenever it is due."""
        self._start_embargo()
        # Three guards share this path (language, reference, price), so the
        # reason names the mechanism rather than guessing which one fired.
        self._truncate_reply("a guard killing and redoing the reply")
        if self.rtc is not None and self.rtc.track is not None:
            self.rtc.track.clear()
        if self.audio_open:
            self.audio_open = False
            self._send_json({"type": "pcm_end"})
        self._maybe_promote_rtc()
        self._send_json({"type": "stop_audio"})
        self.reply_text = ""
        self.response_active = False
        session = self.session
        if session is None:
            return
        pin = self.reply_lang_pin
        regreet = self._first_exchange_note()
        if regreet:
            note = f"{note}\n\n{regreet}" if note else regreet

        async def _redo() -> None:
            await session.cancel_response()
            if note:
                await session.request_response(pin, note=note)
            else:
                await session.request_response(pin)

        asyncio.get_running_loop().create_task(_redo())

    def _arm_transcript_fallback(self) -> None:
        """The model heard the audio NATIVELY — the transcript is our record,
        not its input — so a lost transcription must cost the record, never
        the reply. If no transcript lands in time, respond from audio alone."""
        self._cancel_transcript_fallback()

        async def _fallback() -> None:
            await asyncio.sleep(_TRANSCRIPT_FALLBACK_S)
            if self.ended:
                return
            # The completed transcript never came — but its DELTAS may have
            # (live 2026-08-01, twice): answer from what was streamed, as an
            # ordinary turn with the words' full evidence weight. The byte
            # count is overridden — a transcriber that produced text did so
            # from committed audio, which is evidence our counter can miss.
            text = self._pending_transcript.strip()
            self._pending_transcript = ""
            self._transcript_fallback = None
            if text:
                log.warning(
                    "transcript completed never arrived — using its deltas: %r (session=%s)",
                    text[:60], self.session_id[:8],
                )
                heard = max(self.utterance_bytes, _MIN_UTTERANCE_BYTES)
                self.utterance_bytes = 0
                self.last_activity_mono = time.monotonic()
                self._decide_turn(heard, text)
                return
            log.warning(
                "transcript missing — responding from audio (session=%s)",
                self.session_id[:8],
            )
            # Real speech, even without a transcript, reopens a closing
            # conversation — same disarm as _decide_turn's real-turn path.
            self.pending_close = False
            self._silence_farewell_at = None
            self._close_reason = "closed"
            self.exchanges += 1
            self._respond(supersede=self.speech_over_reply)

        self._transcript_fallback = asyncio.get_running_loop().create_task(_fallback())

    def _cancel_transcript_fallback(self) -> None:
        task, self._transcript_fallback = self._transcript_fallback, None
        if task is not None:
            task.cancel()

    def _decide_turn(self, heard_bytes: int, text: str) -> None:
        """Words decide everything (2026-07-30). Keying turn-taking on VAD
        events chopped her speech relentlessly: AEC3's residual of HER OWN
        voice is speech-shaped, so it passed the Silero gate, tripped the
        vendor's VAD, and every false segment paused playback, superseded a
        live reply, or drew a reply nobody wanted. The transcript separates
        the three cases sounds cannot:
          - nothing / hallucinated noise -> resume, no reply;
          - her own words coming back (echo) -> resume, no reply, no bubble
            (a bubble would put HER words in the customer's mouth — the
            2026-07-28 transcription-leak bug in new clothes);
          - a backchannel while she talks ("yeah", "okay") -> resume, keep
            talking — a human doesn't stop mid-sentence because the listener
            agreed;
          - real words -> a real turn: supersede if it interrupted, respond."""
        if self.ended:
            return
        # Sub-perceptual audio produces NO turn, whatever the transcriber
        # claims it heard: a "transcript" born from a fraction of a second of
        # near-silence is hallucination by definition (the vocabulary-prompt
        # leak, live 2026-07-28).
        words = _normalize_words(text)
        if not words:
            # A sound with no words in it — a cough, a rustle, silence.
            # LOGGED SINCE 2026-08-04: this branch was silent, and a silent
            # branch is indistinguishable from "the microphone never reached
            # us" when a customer says their answer went unheard.
            log.info(
                "no words in %s bytes of audio (transcript %r) (session=%s)",
                heard_bytes, text[:40], self.session_id[:8],
            )
            self._resume_playback()
            return
        if heard_bytes < _MIN_UTTERANCE_BYTES:
            # WORDS WIN (live 2026-08-01): 'Yes, correct.' — a customer
            # confirming a cancellation — was dropped RIGHT HERE for
            # measuring 1536 bytes, and Aisha went silent at the exact
            # moment silence hurts most. The byte gate was built for the
            # prompt-leak hallucinations of 2026-07-28; both transcriber
            # prompts are gone now, so a transcript with real words is
            # believed on its words. Logged, never dropped.
            log.info(
                "short-audio transcript accepted on its words (%s bytes): %r (session=%s)",
                heard_bytes, text[:60], self.session_id[:8],
            )
        interrupting = self.speech_over_reply
        if interrupting and self._is_reply_echo(words):
            log.info("echo transcript ignored (session=%s)", self.session_id[:8])
            self._resume_playback()
            return
        # ⚠️ THE BACKCHANNEL AND ONE-WORD GATES NOW APPLY ONLY TO THE WS-PCM
        # FALLBACK (2026-08-04). They were built because interrupting her was
        # DESTRUCTIVE: the vendor kept the whole unheard reply, so every "yeah"
        # that superseded left her believing she had said things the customer
        # never heard. Truncation removed that cost, and with it the reason to
        # refuse short words — a customer who says "hmm" or "ok ok" over a car
        # description means STOP, exactly as they would to a person, and being
        # ignored for it was the loudest complaint about the call. The model
        # now decides what a short word means, with an honest record of how
        # much of its reply was actually heard.
        #
        # The fallback keeps both gates because it keeps the old exposure: its
        # playback is the CLIENT'S, so the truncation point there is an
        # estimate, and it has no AEC3 — the echo that made these gates
        # necessary is still real on that path.
        # ⚠️ THE FAREWELL RE-ARMS THE SHORT-WORD GATES, on every transport
        # (2026-08-04, the goodbye-cutoff fix).
        #
        # `not rtc_active` alone was right for the middle of a conversation and
        # wrong at the end of one. On WebRTC a single word supersedes — which is
        # the 2026-08-04 change that finally let "hmm" and "yeah" cut her off,
        # and that behaviour is deliberate and stays. But a goodbye is the ONE
        # moment where both people speak at once and NEITHER means to interrupt:
        # "thanks, bye" over her "thank you for calling" is two people finishing
        # the same exchange, not a barge-in. Ungated, it killed her farewell
        # mid-word AND cancelled the hangup, so the call limped on after she had
        # already said goodbye — the reported symptom.
        #
        # A real SENTENCE still supersedes and still reopens the call: that is
        # the 2026-07-31 behaviour ("also, forget me" over the farewell) and it
        # is untouched below. Only short noises are held back, and only here.
        gated = interrupting and (not self.rtc_active or self.pending_close)
        if gated and " ".join(words) in _BACKCHANNELS:
            log.info(
                "backchannel %r over her reply — she keeps talking (session=%s)",
                text[:40], self.session_id[:8],
            )
            self._finish_utterance(heard_bytes, text)
            self._resume_playback()
            return
        # Substance rule (2026-08-03): one word over her live reply is not a
        # barge-in — echo fragments and noise-burst hallucinations arrive
        # exactly this shape, and each one used to kill the reply mid-word.
        # Heard, delivered, not superseding. Real barge-ins are sentences
        # (or the client's own barge_in detector, which is unaffected).
        if gated and len(words) < _MIN_INTERRUPT_WORDS:
            log.info(
                "one-word interruption %r — she keeps talking (session=%s)",
                text[:40], self.session_id[:8],
            )
            self._finish_utterance(heard_bytes, text)
            self._resume_playback()
            return
        # The goodbye was premature: a REAL customer turn arriving while the
        # hangup is armed reopens the conversation (live 2026-07-31 — "also,
        # forget me" spoken over the farewell, and the armed close cut the
        # erasure talk dead at the next reply's end). Backchannels and echo
        # never reach here, so only genuine words disarm it.
        #
        # ⚠️ MOVED BELOW BOTH GATES (2026-08-04). It used to sit BETWEEN them,
        # so an utterance the min-words gate was about to discard had already
        # cancelled the hangup on its way past: she had said goodbye, nothing
        # replaced it, and the call sat open until the silence ladder fired a
        # minute later. Cancelling a close is a decision for an utterance we
        # actually treat as a turn — the gates decide that, so the disarm has
        # to come after them.
        if self.pending_close:
            self.pending_close = False
            self._silence_farewell_at = None
            self._close_reason = "closed"
            log.info(
                "pending close cancelled — the customer spoke again (session=%s)",
                self.session_id[:8],
            )
        # THE SAME UTTERANCE, TRANSCRIBED TWICE (2026-08-03 — see _near_repeat).
        # Only while the first copy's reply is still owed or in flight: once
        # she has finished answering, a customer repeating themselves means
        # they were not heard, and that MUST be answered afresh. Delivered as
        # a turn either way — dropping the bubble would lose words the
        # customer really said; what is suppressed is the second ANSWER.
        answering = self.response_active or self._awaiting_reply_since is not None
        _since_answered = time.monotonic() - self._answered_at
        if (
            answering
            and (
                (_since_answered < _DUPLICATE_WINDOW_S
                 and _near_repeat(words, self._answered_words))
                # A ONE-WORD ANSWER, TWICE, IN THE SAME BREATH (found live
                # 2026-09-07). `_near_repeat` requires two words on purpose —
                # "a lone 'yes' legitimately follows a lone 'yes'" — and that
                # is true of a customer answering two questions. It is not true
                # inside two seconds, while the first copy's reply is still
                # owed: nobody answers the same question twice that fast, and
                # the transcriber splitting one utterance is far likelier.
                #
                # WHAT IT COST. A customer said "No." and the vendor sent it
                # twice, a second apart. Both became turns. The first drew the
                # recap; the SECOND's response.create was refused
                # (conversation_already_has_active_response), so no reply ever
                # began for it, `_awaiting_reply_since` stayed set, and six
                # seconds later _rescue_owed_reply asked again — putting the
                # licence reminder and the WhatsApp consent question on the
                # line while the recap she had just asked about was still
                # unanswered. The rescue is right; it was rescuing a turn that
                # never needed answering.
                or (_since_answered < _SHORT_DUPLICATE_WINDOW_S
                    and len(words) == 1
                    and words == self._answered_words)
            )
        ):
            log.info(
                "duplicate transcript %r — already answering it (session=%s)",
                text[:40], self.session_id[:8],
            )
            self._finish_utterance(heard_bytes, text)
            self._resume_playback()
            return
        # One language per call (2026-08-03): the first CLEAR utterance locks
        # an unlocked call; after that no transcript re-decides anything —
        # the per-turn verdict is kept for the log, so a mid-call switch is
        # visible in diagnostics without ever changing what she speaks.
        verdict = _pin_lang(text)
        if not self.lang_locked:
            lock = _lock_verdict(text)
            if lock is not None:
                self.lang_locked = True
                self.reply_lang_pin = lock
                log.info(
                    "call language locked: %s (session=%s)",
                    lock, self.session_id[:8],
                )
        log.info(
            "heard: %r -> lang %s, call lang %s%s (session=%s)",
            text[:80], verdict or "?", self.reply_lang_pin,
            "" if self.lang_locked else " (unlocked)",
            self.session_id[:8],
        )
        self._answered_words = words
        self._answered_at = time.monotonic()
        # Open this turn's LangSmith run (flushing the previous one) — only the
        # real-turn path, so echo/backchannel/duplicate utterances above never
        # become runs.
        self._begin_voice_trace(text)
        self._finish_utterance(heard_bytes, text)
        self._respond(supersede=interrupting)

    def _is_reply_echo(self, words: list[str]) -> bool:
        """A short 'interruption' that is a VERBATIM, IN-ORDER fragment of
        her live or just-finished reply is her own voice surviving echo
        cancellation.

        HARDENED 2026-08-03 (the swallowed-confirmation fix). The original
        test was bag-of-words — same words, any order — and it discarded the
        one sentence that matters most: "Yes, Saturday at seven at the
        showroom", confirmed over her recap, is built entirely of HER words,
        so a real confirmation died as echo at the climax of the booking.
        Two rules now narrow the match, each with its own blind spot covered
        by the other:
          (a) a LEADING yes/no word is never echo — echo fragments start at
              arbitrary mid-sentence points of her reply, while a customer
              confirming leads with the verdict. Leading, not contained:
              her own "no availability that evening" echo must not slip
              through on the strength of its "no".
          (b) the words must appear in HER ORDER (an in-order subsequence of
              one reply, tolerating transcriber-dropped words) — echo is
              verbatim; a human confirming rearranges.
        Known residual, accepted with the fix: echo that comes back with its
        word ORDER garbled now slips the guard. If she ever starts answering
        her own voice again, revert (b) first and keep (a) — and this corner
        is on the real-hardware call test with the barge thresholds."""
        if not words or len(words) > _ECHO_MAX_WORDS:
            return False
        if words[0] in _YESNO_WORDS:
            return False
        def _in_order(fragment: list[str], reply: str) -> bool:
            reply_words = _normalize_words(reply)
            if not reply_words:
                return False
            it = iter(reply_words)
            return all(w in it for w in fragment)
        return _in_order(words, self.reply_text) or _in_order(
            words, self.last_reply_text
        )

    def _finish_utterance(self, heard_bytes: int, text: str) -> None:
        """Nothing is stored (ephemeral call) — the event is the protocol's
        record that a turn was heard, for any future caption UI. Callers
        have already decided the turn is real (words win — see
        _decide_turn), so no byte gate here: skipping it once silently
        broke the consent guard's turn count for short answers."""
        if self.ended:
            return
        self.exchanges += 1
        # Real words are presence — the idle ladder's rungs re-arm HERE, not
        # at speech_started: echo residue tripping the vendor VAD once
        # re-armed the nudge and it played twice in a silent stretch (live
        # 2026-08-01, the double 'are you still with me?').
        self._nudge_done = False
        self.customer_turns += 1
        # Digits their mouth actually produced — the evidence stream the
        # mobile guard checks tool arguments against. Concatenated across
        # utterances so a number split over two breaths still counts.
        self.heard_digits += _spoken_digits(text)
        # A REFERENCE THE CUSTOMER THEMSELVES SPOKE IS ONE SHE MAY REPEAT
        # (2026-08-05). Since the manage-a-booking journey identifies by TD
        # reference, the customer now says one BEFORE any tool result contains
        # it — and reading it back to confirm is the correct thing to do with a
        # code you just heard. The reference guard whitelists only tool results
        # and the seeded thread, so without this her read-back looked invented
        # and was cancelled mid-word. Safe for the same reason heard_digits is:
        # the model does not write the customer's transcript. Whitelisting a
        # MIS-heard reference costs nothing either — it governs what she may
        # SAY, never what exists; the tool still decides whether the booking is
        # real.
        self.known_refs |= _harvest_spoken_refs(text)
        self._send_json({
            "type": "utterance",
            "audio_id": None,
            "text": text,
            "lang": _detect_lang(text),
            "duration_ms": int(heard_bytes / 2 / self.sample_rate * 1000),
            "failed": not text,
        })
        # Persist the customer's spoken turn so the full call shows in the chat
        # once it ends (the call is no longer fully ephemeral — see the module
        # docstring's persistence note). Off the audio loop; fail-open.
        self._store_call_turn(Speaker.customer, text, _detect_lang(text))

    def _fact_guard(self, name: str, arguments: dict) -> dict | None:
        """Customer-supplied facts must have been SUPPLIED BY THE CUSTOMER
        (2026-07-31, the invented-identity incident). The write guards
        verify state; nothing verified provenance — so the model fabricated
        a name, a mobile and a consent grant and booked a real slot for a
        person who does not exist. Two provenance checks, both error-as-data
        so the model recovers by ASKING:

        - any tool argument carrying a mobile must match digits the
          customer's own utterances (or the resumed typed thread) actually
          contained — the last seven digits, contiguous, in any spoken form;
        - a consent GRANT must arrive after the customer has spoken again
          since identification: an answer must exist for it to record.
          (What a 'yes' meant is beyond any guard — rule 9's ambiguous-yes
          limit — but a grant with no answer at all behind it is refused
          outright, and the mobile guard keeps a falsely recorded consent
          from ever reaching a stranger's number.)

        Withdrawals are never blocked: refusing a customer's 'no' is the
        anti-customer direction, and a fabricated withdrawal only costs us
        a message we were allowed to send."""
        mobile = arguments.get("mobile")
        if mobile:
            claimed = re.sub(r"\D", "", str(mobile))
            if claimed and not _tail_heard(claimed, self.heard_digits):
                return {
                    "error": "mobile_not_heard",
                    "detail": (
                        "The customer has not said this mobile number in this "
                        "conversation. Never fill in a number yourself: ask "
                        "them for it, wait for their answer, and use exactly "
                        "the digits they say."
                    ),
                }
        if name in ("lock_slot", "create_booking"):
            # Slot provenance (2026-07-31): a slot id no successful tool
            # result ever named is an invention — booking it would commit
            # the customer to a time nobody discussed. Same whitelist
            # pattern as the reference guard, applied to arguments.
            try:
                slot_arg = int(arguments.get("slot_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                slot_arg = None
            if slot_arg is not None and slot_arg not in self.known_slot_ids:
                return {
                    "error": "slot_not_offered",
                    "detail": (
                        "This slot id did not come from a tool result in this "
                        "conversation. Never invent or reuse a slot id: call "
                        "get_available_slots, offer the customer real times, "
                        "and use the slot_id of the one they chose."
                    ),
                }
        if name == "record_consent" and arguments.get("messaging") is True:
            baseline = self.turns_at_identity if self.turns_at_identity is not None else 0
            if self.customer_turns <= baseline:
                return {
                    "error": "consent_not_heard",
                    "detail": (
                        "The customer has not answered a consent question yet. "
                        "Ask for WhatsApp consent on its own, wait for their "
                        "answer, and record only what they actually said."
                    ),
                }
            # Rule 9's separation, enforced (2026-07-31): a grant is only
            # believable if the consent QUESTION was actually asked — her
            # own COMPLETED previous reply must mention WhatsApp. A recap's
            # "yes" can then never be filed as messaging consent (the chat's
            # 2026-07-28 live defect, kept out of the call), and the LIVE
            # reply deliberately does not count: a question still being
            # spoken cannot have been answered yet.
            if not re.search(r"whats\s*app|واتس", self.last_reply_text, re.IGNORECASE):
                return {
                    "error": "consent_question_not_asked",
                    "detail": (
                        "You have not asked the customer for WhatsApp consent "
                        "yet — a yes to the booking recap is not messaging "
                        "consent. Ask about WhatsApp confirmations on its "
                        "own, wait, and record only that answer."
                    ),
                }
        return None

    def _note_outcome(self, name: str, result: dict) -> None:
        """Everything a tool RESULT teaches the gateway, in one place —
        trusted context resolved from results exactly as the graph does it,
        plus what the call achieved (for the summary row)."""
        # Every reference a tool result contains is now a fact of this call —
        # the reference guard's whitelist. Results only, NEVER the model's own
        # arguments: the invented TD-2026-0307 arrived as an argument, and
        # whitelisting arguments would let the model launder its inventions.
        try:
            dumped = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            dumped = ""
        self.known_refs |= _harvest_refs(dumped)
        # Amounts a result contained are quotable prices (the price guard's
        # whitelist). Slot ids only from SUCCESSFUL results — a failure
        # envelope echoes the model's own slot_id argument back, and
        # harvesting that would launder an invented id (see __init__).
        self.known_amounts |= _harvest_amounts(dumped)
        if "error" not in result and result.get("locked") is not False:
            self.known_slot_ids |= _harvest_slot_ids(result)
        if name in _IDENTITY_TOOLS and isinstance(result.get("customer"), dict):
            cid = result["customer"].get("customer_id")
            if cid is not None:
                self.customer_id = cid
                # The consent guard's baseline: a consent grant is only
                # believable once the customer has SPOKEN again after this
                # moment — the answer to a question they were actually asked.
                self.turns_at_identity = self.customer_turns
        if name == "forget_me" and result.get("forgotten") is True:
            self.customer_id = None
        # What the call ACHIEVED, for the summary row's booking card — makes
        # and moves only, never cancels (a cancelled ref resolves to nothing,
        # the anchor rule from the API contract).
        if name in ("create_booking", "reschedule_booking"):
            booking = result.get("booking")
            ref = booking.get("booking_ref") if isinstance(booking, dict) else None
            if ref and ref not in self.booking_refs:
                self.booking_refs.append(ref)
        if name == _CLOSE_TOOL and result.get("closed") is True:
            # Let her say goodbye; the call ends when the farewell finishes.
            self.pending_close = True

    async def _handle_function_call(self, name: str, arguments: dict, call_id: str) -> None:
        if self.ended or self.session is None:
            return
        self.last_activity_mono = time.monotonic()
        result = await asyncio.to_thread(self._run_tool_sync, name, arguments, call_id)
        self._note_outcome(name, result)
        # Name the tool on the current turn's trace (best-effort, tracing-only).
        if self._trace_customer is not None:
            self._trace_tools.append(name)
        self._send_json({"type": "tool_event", "tool": name, "input": arguments, "output": result})
        if self.ended or self.session is None:
            return
        # A CLOSE EARNS NO FURTHER REPLY (2026-09-07). Every tool result asked
        # the vendor for one, unconditionally — right for a lookup, wrong for
        # the tool that means the conversation is over. She says goodbye, calls
        # close_conversation, and the result then bought her one more turn,
        # which she filled: live, "Take care, goodbye." was followed by "The
        # conversation is now closed. You're all set." — a machine line after a
        # human farewell, and one more reply able to silence the goodbye still
        # leaving the speaker.
        #
        # THE SAME RULE CHAT HAS HAD ALL ALONG. graph._route_from_tools ENDs the
        # turn on a successful close for this exact reason: "Routing back to the
        # agent here would buy one more generation after the goodbye, which the
        # model would fill: a second sign-off, or worse, a cheerful new question."
        # This is that edge, on the channel that had no edge.
        #
        # ONLY ON A GENUINE CLOSE. A refused one (close_not_alone, or an error)
        # leaves the conversation open and she must be able to speak to it, so
        # it takes the ordinary path. `_note_outcome` has already armed
        # pending_close, and _finish_response's own close branch ends the call
        # once her farewell has drained — nothing here shortens that.
        closing = name == _CLOSE_TOOL and result.get("closed") is True
        await self.session.send_tool_result(
            call_id, result, request_reply=not closing
        )

    def _finish_response(self) -> None:
        if self.ended:
            return
        self.response_active = False
        if self.audio_open:
            self.audio_open = False
            self._send_json({"type": "pcm_end"})
        self._maybe_promote_rtc()
        text, self.reply_text = self.reply_text.strip(), ""
        if text:
            # WHAT SHE ACTUALLY SAID (2026-08-03). In realtime mode the call is
            # ephemeral — nothing spoken is stored — so a reply that invents a
            # brand we do not sell was, until now, unreconstructable after the
            # fact: the audit trail shows which TOOLS ran, never her words, and
            # a hallucination is precisely the case where no tool ran. The
            # customer's side is already logged (`heard:`); this is the other
            # half of the same conversation, and grounding complaints cannot be
            # investigated with only one side of it. Truncated, log-only — it
            # creates no new stored surface for rule 12 to chase.
            # The word count is the brevity change's ONLY honest measure
            # (2026-08-04). Her replies averaged 23 words — about ten seconds
            # spoken — and 71% of customer turns landed while she was still
            # talking, which is what makes her repeat herself: a question at
            # the end of a long reply is interrupted before it arrives.
            log.info(
                "said (%s words): %r (session=%s)",
                len(text.split()), text[:200], self.session_id[:8],
            )
            self.last_reply_text = text  # the echo guard's reference
            # Persist her reply for EVERY turn so the full call transcript
            # shows both sides after it ends (was only stored on a farewell).
            self._store_call_turn(Speaker.agent, text, self.reply_lang_pin)
            # turn_id null: her reply exists nowhere to point read-aloud at —
            # the call already read it aloud, which is the point of a call.
            self._send_json({"type": "done", "lang": _detect_lang(text), "turn_id": None})
        # Conversation-end auto-close (2026-08-05, QA: "agent says bye, call
        # still continues"). The model SPEAKS a goodbye but does not reliably
        # call close_conversation, so a natural sign-off is caught HERE and
        # armed for the same drain-then-hangup the tool would trigger. Skipped
        # when a close is already pending (the silence goodbye, or an actual
        # close_conversation, set it) and for the nudge (never a farewell). A
        # misread reopens the moment the customer speaks (_decide_turn).
        if text and not self.pending_close and not self.ended and _is_farewell(text):
            self.pending_close = True
            self._close_reason = "closed"
            log.info(
                "agent farewell — auto-closing the call (session=%s)",
                self.session_id[:8],
            )
            # agent reply persisted for EVERY turn in the `if text:` block above
            # Attach this reply to the current turn's LangSmith run. Skip the
            # NUDGE's own reply (_suppress_activity_stamp, still set here): it
            # answers no customer turn, and folding "Are you still with me?"
            # into the previous turn's output would only muddy the evaluation.
            if self._trace_customer is not None and not self._suppress_activity_stamp:
                self._trace_replies.append(text)
        if self._suppress_activity_stamp:
            # The nudge's own reply is not the customer's presence — stamping
            # it would postpone the silence hangup the nudge precedes.
            self._suppress_activity_stamp = False
        else:
            self.last_activity_mono = time.monotonic()
        if self.pending_close:
            # Not _end() directly: response_done means GENERATION finished,
            # but on the rtc path her farewell still sits in the server-paced
            # queue — ending now cut her off mid-word (live 2026-07-31, the
            # 3-second goodbye). Drain first; a person finishes the sentence.
            # The reason is "closed" for a goodbye the MODEL made, "silence"
            # for the idle ladder's goodbye (_silence_goodbye).
            asyncio.get_running_loop().create_task(
                self._end_after_drain(self._close_reason)
            )
            return
        if self.state != "listening":
            self._set_state("listening")

    async def _end_after_drain(self, reason: str) -> None:
        """Let the farewell finish LEAVING THE SPEAKER before the line drops.
        Bounded — nobody is held on a dead call for more than ~12s. Aborts
        the moment pending_close is withdrawn: the customer reopening the
        conversation during the farewell disarms the hangup (see
        _decide_turn), and this task must not fire it anyway.

        ⚠️ IT WAITS ON BOTH TRANSPORTS NOW (2026-08-04). It used to consult
        only the rtc track's queue, so on the WS-PCM fallback — where there is
        no server-side queue because the CLIENT schedules playback — `track is
        None` broke the loop on its first pass and the call dropped 0.3s later,
        mid-farewell, every time. `speaking_until_mono` is the transport-
        agnostic answer (it is derived from BYTES FED, which is why it exists),
        and `_played_ms` already uses it for exactly this fallback. Two
        estimates that can disagree is how this class of bug returns, so the
        drain now reads whichever applies and waits for the later of them.
        """
        deadline = time.monotonic() + 12.0
        while not self.ended and self.pending_close and time.monotonic() < deadline:
            track = self.rtc.track if self.rtc is not None else None
            queued = track is not None and track.queued_ms > 0
            still_speaking = self.speaking_until_mono > time.monotonic()
            if not queued and not still_speaking:
                break
            await asyncio.sleep(0.1)
        # THE TAIL COVERS WHAT THE SERVER CANNOT SEE. An empty queue means the
        # last frame was handed to the NETWORK, not that it reached an ear:
        # the browser's own jitter buffer still holds 300-500ms on a relayed
        # path, and closing the peer connection discards it. The old fixed 0.3s
        # (plus the client's 350ms grace) was under that on TURN, which cost
        # the last word or two of every goodbye. Configurable so it can be
        # tuned against a real network without a redeploy.
        await asyncio.sleep(CALL_FAREWELL_TAIL_SECONDS)
        if not self.pending_close:
            return
        self._end(reason)

    # ------------------------------------------------------------ client side
    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = msg.get("type")
        if kind == "start":
            rate = msg.get("sample_rate")
            if isinstance(rate, int) and rate in _SAMPLE_RATES:
                self.sample_rate = rate
        elif kind == "barge_in":
            # The client killed playback; stop the vendor writing more of it.
            self.last_activity_mono = time.monotonic()
            self._nudge_done = False  # a barge is presence too
            self._truncate_reply("the client's own barge detector")  # before the queue is cleared
            if self.rtc is not None and self.rtc.track is not None:
                self.rtc.track.clear()
            if self.audio_open:
                self.audio_open = False
                self._send_json({"type": "pcm_end"})
            self._maybe_promote_rtc()
            # Only cancel a response that exists — a stale barge (echo, a
            # double-tap) against an idle session is noise, not intent.
            if self.session is not None and self.response_active:
                self._start_embargo()  # its in-flight tail dies too
                await self.session.cancel_response()
            if self.state != "listening":
                self._set_state("listening")
        elif kind == "rtc_offer":
            # The audio upgrade (2026-07-30): negotiate a peer connection for
            # this call. Refusals are SPOKEN (rtc_unavailable) — the client
            # then simply keeps the WS-PCM path it was born on.
            sdp = msg.get("sdp") or ""
            if CALL_TRANSPORT != "webrtc" or not sdp or self.rtc is not None:
                self._send_json({"type": "rtc_unavailable"})
                # A refused upgrade settles the dial wait: start on WS-PCM
                # now rather than ringing out the offer window. (A duplicate
                # offer leaves the pending greet alone — rtc is not None.)
                if self.rtc is None:
                    await self._greet_now()
                return
            _turn_user, _turn_pass = turn_credentials()
            transport = make_rtc_transport(
                on_pcm=self._on_rtc_pcm,
                on_connected=self._rtc_up,
                on_failed=self._rtc_down,
                stun_url=RTC_STUN_URL,
                turn_url=RTC_TURN_URL,
                # Time-limited when RTC_TURN_SECRET is set, static otherwise.
                # OUR side and the browser must use the same mechanism —
                # coturn accepts exactly one at a time.
                turn_username=_turn_user,
                turn_credential=_turn_pass,
            )
            if transport is None:
                self._send_json({"type": "rtc_unavailable"})
                await self._greet_now()
                return
            self.rtc = transport
            try:
                answer = await transport.accept_offer(sdp)
            except Exception as exc:  # noqa: BLE001 — negotiation is best-effort
                log.warning("rtc negotiation failed: %s", exc)
                self.rtc = None
                await transport.close()
                self._send_json({"type": "rtc_unavailable"})
                await self._greet_now()
                return
            self._send_json({"type": "rtc_answer", "sdp": answer})
        elif kind == "rtc_error":
            # The browser could not apply our answer (2026-08-04). Until this
            # existed the failure was swallowed by a bare .catch() in the
            # client, and the only symptom was our own connectivity checks
            # going out to a peer that had never started ICE — indistinguishable
            # from a network dropping them, which is what we blamed three times.
            # Log-only: WS-PCM is already carrying the call and continues to.
            log.warning(
                "rtc answer rejected by the browser: %s (session=%s)",
                str(msg.get("detail") or "?")[:300], self.session_id[:8],
            )
        elif kind == "mute":
            self.muted = True
        elif kind == "unmute":
            self.muted = False
        elif kind == "hangup":
            self._end("hangup")
        # end_turn has no meaning here: the vendor's VAD owns turn-taking.

    def _rtc_up(self) -> None:
        """ICE connected: audio has MOVED — but never mid-reply. A reply
        whose audio straddles the transport flip is split across two CLOCKS:
        the client keeps playing its buffered WS-PCM head while the rtc
        track plays the tail live, and both are audible at once (the
        two-voices bug, live 2026-07-31 — the greeting starts generating
        before ICE finishes, so the greeting is exactly the straddling
        reply). If a WS segment is open, the takeover waits for the reply
        boundary; deferring the client notice is safe — until it arrives
        the call simply continues as it was born, on WS-PCM."""
        if self.audio_open:
            self.rtc_pending = True
            log.info(
                "rtc connected mid-reply — takeover deferred to the reply "
                "boundary (session=%s)", self.session_id[:8],
            )
            return
        self._rtc_activate()

    def _rtc_activate(self) -> None:
        self.rtc_pending = False
        self.rtc_active = True
        log.info("rtc transport active — session=%s", self.session_id[:8])
        self._send_json({"type": "rtc_active"})

    def _maybe_promote_rtc(self) -> None:
        """The deferred takeover, at the reply boundary it waited for."""
        if self.rtc_pending and not self.audio_open and not self.ended:
            self._rtc_activate()

    def _rtc_down(self) -> None:
        """The peer connection died MID-CALL: degrade to the WS-PCM path the
        call was born on. A transport failure costs QUALITY, never the call.
        At hangup the browser closes its side a beat before our teardown runs
        — that is a healthy end, not a loss, hence the ended guard (a live
        call logged it as 'transport lost' on every normal hangup,
        2026-07-30)."""
        if self.ended or not (self.rtc_active or self.rtc_pending):
            return
        self.rtc_active = False
        self.rtc_pending = False
        self._rtc_mic_live = False
        log.warning("rtc transport lost — ws audio fallback, session=%s", self.session_id[:8])
        self._send_json({"type": "rtc_failed"})
        # A transport that died before the greeting settles the question the
        # dial wait was asking: the call starts now, on WS-PCM.
        if not self._greeted and self.loop is not None:
            self.loop.create_task(self._greet_now())

    async def _greet_now(self) -> None:
        """The pickup: tell the client the call is ready (its ring stops on
        call_ready) and let Aisha speak first. Idempotent — every settle path
        (rtc active, refusal, negotiation failure, transport death, timeout)
        funnels here and only the first arrival greets."""
        if self._greeted or self.ended or self.session is None:
            return
        self._greeted = True
        self._send_json({"type": "call_ready"})
        greet = getattr(self.session, "greet", None)
        if greet is not None:
            try:
                await greet()
            except Exception:  # noqa: BLE001 — a failed greeting must not
                # kill the call; the customer can still speak and be answered.
                log.warning("greeting failed — call continues", exc_info=True)

    async def _greet_when_ready(self) -> None:
        """Dial-until-ready: hold the greeting (bounded) while the audio
        upgrade settles, so no reply ever straddles the WS→WebRTC flip and
        the doubled-mic window cannot exist. Polling at 50 ms keeps this a
        single trigger path — rtc_active, refusal and death need no extra
        event plumbing to wake it."""
        deadline = time.monotonic() + _RTC_OFFER_WAIT_S
        while not self.ended and self.rtc is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        deadline = time.monotonic() + _RTC_SETTLE_WAIT_S
        while (
            not self.ended and self.rtc is not None and not self.rtc_active
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        await self._greet_now()

    async def _on_pcm_24k(self, pcm: bytes) -> None:
        """The one mic path, whatever transport carried the audio here."""
        if self.muted or self.session is None:
            return
        if self._greet_task is not None and not self._greeted:
            # Still ringing: like a real phone, nothing said before the
            # pickup is part of the call — forwarding it would let the turn
            # engine answer before the greeting and collide with it.
            return
        # The Silero gate (2026-07-30): only detected speech reaches the
        # vendor. What its VAD never hears — echo residue, breaths, the idle
        # room — it can never commit as a turn, misfire on, or bill. The
        # byte count below is of FORWARDED audio for the same reason: the
        # hallucination guard's evidence should be evidence of speech.
        if self.gate is not None:
            was_open = self.gate.speaking
            pcm = self.gate.feed(pcm)
            if self.gate.speaking != was_open:
                # The gate is the FIRST thing that can swallow an answer, and
                # until 2026-08-04 it did so without a word in any log — so a
                # customer whose "yes" never arrived was indistinguishable from
                # one who never spoke.
                log.info(
                    "speech gate %s (session=%s)",
                    "OPEN — forwarding to the vendor" if self.gate.speaking else "closed",
                    self.session_id[:8],
                )
            if not pcm:
                return
        if self.capturing:
            self.utterance_bytes += len(pcm)
            # Buffer the caller's forwarded (gated, 24kHz) speech for the trace —
            # exactly the audio the vendor transcribed, bounded so a long turn
            # cannot grow memory without limit.
            if self._trace_audio and len(self._trace_cust_pcm) < _MAX_TRACE_AUDIO_BYTES:
                self._trace_cust_pcm.extend(pcm)
        await self.session.send_audio(pcm)

    async def _on_rtc_pcm(self, pcm: bytes) -> None:
        # The first frame here proves the peer connection now carries the
        # microphone: from this moment the WS uplink of the same mic is a
        # DUPLICATE (the doubled-mic bug — the vendor heard every word twice,
        # interleaved, during the takeover window), never a fallback.
        self._rtc_mic_live = True
        await self._on_pcm_24k(pcm)

    async def _on_bytes(self, data: bytes) -> None:
        if self.rtc_active or self._rtc_mic_live:
            return  # audio moved to the peer connection; stray WS frames die here
        await self._on_pcm_24k(resample_pcm16(data, self.sample_rate, VENDOR_SAMPLE_RATE))

    # -------------------------------------------------------------------- run
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        sender = self.loop.create_task(self._sender())
        watchdog = self.loop.create_task(self._watchdog())

        db = SessionLocal()
        try:
            audit_service.record(
                db, session=self.session_id, actor=Actor.customer,
                type=AuditType.voice,
                payload={"action": "call_started", "engine": "realtime"},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

        self._send_json({
            "type": "call_started", "max_minutes": CALL_MAX_MINUTES,
            # The client adapts its capture to the engine: realtime streams
            # the mic CONTINUOUSLY (the vendor's VAD owns turn-taking and
            # barge-in); pipeline gates on its local state machine.
            "engine": "realtime",
            # Invite the audio upgrade: the client may send rtc_offer. Until
            # (and unless) rtc_active arrives, WS PCM carries the call.
            "rtc": CALL_TRANSPORT == "webrtc",
            # THE BROWSER'S ICE SERVERS (2026-08-04). Without these it offers
            # only mDNS-obscured host candidates, aiortc cannot resolve them,
            # and the negotiation fails however good our own relay candidate is
            # (measured — the relay was proven working while calls still never
            # reached `connected`). Sent from the server so the relay can move
            # without a frontend rebuild; [] on localhost, where host
            # candidates already work. Shaped for RTCConfiguration.iceServers,
            # so the client passes it straight through.
            "ice_servers": client_ice_servers(),
        })
        self._set_state("listening")
        # The vendor's VAD owns turn-taking in this mode, full stop.
        self._send_json({"type": "vad", "server": True})

        vocabulary = ""
        db = SessionLocal()
        try:
            vocabulary = voice_service._vocabulary(db)
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close()

        # CALL_PERSONA (2026-07-29): "natural" swaps in the voice-native brief
        # and its warmer voice; anything else runs the classic persona with
        # the seam called EXACTLY as before the switch existed (no extra
        # kwargs), so classic behaviour cannot drift by accident.
        natural = CALL_PERSONA == "natural"
        extra: dict = (
            {"voice": CALL_NATURAL_VOICE, "greeting": NATURAL_GREETING} if natural else {}
        )
        self.session = make_agent_session(
            # Either way a COMPACT persona, not the full system prompt:
            # realtime sessions re-count instructions on EVERY response, and
            # the full prompt burned a 40k-TPM limit in two replies (measured
            # live 2026-07-27 — the she-answers-twice-then-goes-mute bug).
            instructions=natural_instructions() if natural else realtime_instructions(),
            vocabulary=vocabulary,
            on_event=self._on_agent_event,
            **extra,
        )
        if self.session is None or not await self.session.connect():
            self.session = None
            self._end("voice_not_configured")
        else:
            if CALL_SPEECH_GATE:
                # Loads the local ONNX model (~100 ms) — off the loop. None on
                # failure = forward ungated, the pre-gate behaviour.
                self.gate = await asyncio.to_thread(make_speech_gate)
            seed = await asyncio.to_thread(self._seed_turns_sync)
            if seed:
                await self.session.seed_history(seed)
                # A reference the typed thread already contains is one she may
                # legitimately repeat — the guard must not kill her for
                # continuing the conversation the call resumed.
                for _speaker, text in seed:
                    # Loosely punctuated too: the thread may hold a reference the
                    # customer typed as "td2026htxe", which is the same code.
                    self.known_refs |= _harvest_refs(text) | _harvest_spoken_refs(text)
                    # A number already typed in the thread the call resumed
                    # (or read back by Aisha there) is legitimate evidence —
                    # the customer must not be forced to re-dictate it.
                    self.heard_digits += _spoken_digits(text)
                    # Prices already quoted in the typed thread are quotable
                    # here — the call continues that conversation.
                    self.known_amounts |= _harvest_amounts(text)
                # The thread's language IS the call's language (one language
                # per call, 2026-08-03): the seed verdict locks the call
                # before anything is spoken, greeting included. No seed — or
                # nothing judgeable — leaves the call unlocked on the English
                # default until the first clear utterance (_lock_verdict).
                seed_lang = _seed_pin(seed)
                if seed_lang is not None:
                    self.reply_lang_pin = seed_lang
                    self.lang_locked = True
            # The absolute policy: the session is pinned BEFORE anything is
            # spoken, so no response — greeting follow-ups included — can
            # ever go out with the language left to the model.
            setter = getattr(self.session, "set_language", None)
            if setter is not None:
                setter(self.reply_lang_pin)
            # She answers the phone — speaks first, like a person picking up.
            # Dial-until-ready (2026-08-03): when the audio upgrade is on the
            # table, the greeting waits for the transport to settle — the
            # client keeps ringing until call_ready — so the greeting can
            # never straddle the WS→WebRTC flip. A zero offer wait greets
            # inline, exactly the old behaviour (the suite's path).
            if CALL_TRANSPORT == "webrtc" and _RTC_OFFER_WAIT_S > 0:
                self._greet_task = self.loop.create_task(self._greet_when_ready())
            else:
                await self._greet_now()

        try:
            while not self.ended:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await self._on_bytes(message["bytes"])
                elif message.get("text") is not None:
                    await self._on_text(message["text"])
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass
        finally:
            if not self.ended:
                self.ended = True
                self.end_reason = self.end_reason or "hangup"
            # The summary row is written FIRST — before the vendor session
            # and peer-connection closes, which are network teardown that can
            # take seconds. It used to come after them, so the client's
            # end-of-call refresh (even polling for 1.4 s) beat the row to
            # the database and the "Voice call" line only appeared at some
            # later repaint (live 2026-07-31). A local DB insert is
            # milliseconds; nothing in the teardown below depends on order.
            await asyncio.to_thread(self._store_summary_sync)
            # Flush the final customer turn's trace — the last turn has no
            # successor to close it out (see _begin_voice_trace). Off the loop,
            # fail-open; a no-op when tracing is off or the last turn was
            # already flushed.
            await asyncio.to_thread(self._flush_voice_trace)
            if self.session is not None:
                session, self.session = self.session, None
                try:
                    await session.close()
                except Exception:  # noqa: BLE001
                    pass
            self._cancel_transcript_fallback()
            if self._greet_task is not None:
                self._greet_task.cancel()
            if self.rtc is not None:
                rtc, self.rtc = self.rtc, None
                self.rtc_active = False
                try:
                    await rtc.close()
                except Exception:  # noqa: BLE001
                    pass
            watchdog.cancel()
            self._put(("close", None))
            try:
                await asyncio.wait_for(sender, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sender.cancel()

            db = SessionLocal()
            try:
                actor = Actor.customer if self.end_reason == "hangup" else Actor.system
                audit_service.record(
                    db, session=self.session_id, actor=actor,
                    type=AuditType.voice,
                    payload={
                        "action": "call_ended", "engine": "realtime",
                        "reason": self.end_reason or "disconnect",
                        "duration_s": int(time.monotonic() - self.started_mono),
                    },
                )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            finally:
                db.close()
