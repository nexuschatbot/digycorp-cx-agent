"""LangSmith tracing for the realtime voice call (2026-08-04).

WHY THIS EXISTS. The chat path traces to LangSmith because graph.py wraps the
Anthropic client (`wrap_anthropic`) and tags each run with `session_id` — so
the demo project's evaluators score every typed turn. The realtime call is a
DIFFERENT engine: OpenAI speech-to-speech, which never touches that client, so
its turns are invisible in LangSmith and cannot be evaluated at all. This
module closes that gap by emitting the call's turns as LangSmith runs directly.

WHAT IT EMITS. One run per CUSTOMER TURN — the utterance she answered, the
reply (or replies, concatenated across tool follow-ups) she spoke back, and the
tools that turn invoked — as a root run of type "chain", tagged `voice_call`,
with `metadata.session_id` set exactly as the chat does it. It lands in the
SAME project the chat uses (LANGSMITH_PROJECT, i.e. elite_group_test_drive_demo)
unless overridden, so the evaluators already configured there run over voice
turns too, and the LangSmith thread view groups a call's turns by session just
like a typed conversation.

GATING. Rides the chat's own flag, LANGSMITH_TRACING — off in the offline suite
(pytest loads the backend .env, which never sets it) and anywhere the chat is
untraced, on wherever the chat is. Enabling call tracing is therefore the same
single, deliberate act as enabling chat tracing; there is no second switch to
forget.

FAIL-OPEN, ALWAYS. A tracing error is logged and dropped — it must never
disturb a live call. The client is built once, lazily; if it cannot be built
(missing key, import failure) tracing quietly stays off for the process.

⚠️ PDPL. This is the SAME US-endpoint exposure the chat already accepted (see
config.LANGSMITH_TRACING's D1 note): the whole spoken conversation — utterance
and reply text — reaches LangSmith. It carries no more than the chat's traces
do, and rides the identical flag, so it inherits that decision rather than
making a new one. LANGSMITH_HIDE_INPUTS / _HIDE_OUTPUTS (read by the SDK) redact
the text here exactly as they do for chat.
"""

from __future__ import annotations

import io
import logging
import wave
from datetime import datetime, timezone
from uuid import uuid4

from app.config import CALL_TRACE_AUDIO, LANGSMITH_TRACING

log = logging.getLogger("app.services.call_trace")

# The vendor speaks and hears PCM16 mono at this rate; the gateway buffers both
# sides at it (see call_realtime._on_pcm_24k and the `audio` event), so a bare
# WAV header is all the encoding an attachment needs.
_AUDIO_RATE = 24_000

# One client per process, built lazily on first traced turn. `_disabled` latches
# a construction failure so a missing key or bad import costs one warning, not
# one per turn for the life of the process.
_client = None
_disabled = False


def trace_enabled() -> bool:
    """Whether voice-turn tracing is live — the gateway checks this before
    doing any per-turn accumulation, so an untraced call pays nothing."""
    return bool(LANGSMITH_TRACING) and not _disabled


def audio_enabled() -> bool:
    """Whether to buffer and attach the CALL AUDIO — gated a second time by
    CALL_TRACE_AUDIO (off by default; see config). Audio is captured only when
    text tracing is also on, so an untraced or text-only call buffers no PCM."""
    return trace_enabled() and bool(CALL_TRACE_AUDIO)


def _pcm16_to_wav(pcm: bytes, rate: int = _AUDIO_RATE) -> bytes:
    """Wrap raw PCM16 mono in a WAV container — the only encoding LangSmith
    (and its voice evaluators) need to play and judge the clip."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _client_or_none():
    global _client, _disabled
    if _disabled:
        return None
    if _client is None:
        try:
            from langsmith import Client

            _client = Client()
        except Exception:  # noqa: BLE001 — no key, no network, no import: stay off
            log.warning(
                "langsmith client unavailable — voice-call tracing disabled",
                exc_info=True,
            )
            _disabled = True
            return None
    return _client


def log_voice_turn(
    *,
    session_id: str,
    customer: str,
    reply: str,
    language: str,
    tools: list[str] | None = None,
    customer_pcm: bytes | None = None,
    agent_pcm: bytes | None = None,
    project: str | None = None,
) -> None:
    """Emit ONE completed LangSmith run for a spoken call turn.

    `customer` is what the caller said; `reply` is everything Aisha spoke in
    answer (tool follow-ups already concatenated by the gateway). Grouped with
    the chat's turns by `metadata.session_id`; tagged `voice_call` and marked
    with `channel`/`language` metadata so voice turns are filterable from typed
    ones in the same project. `project` overrides the destination; None uses the
    ambient LANGSMITH_PROJECT (the chat's project), which is the intended home.

    `customer_pcm` / `agent_pcm` are the turn's raw PCM16 mono @ 24kHz (both
    sides). When present — only under audio_enabled() — they are WAV-wrapped and
    attached as `customer_audio` / `agent_audio` so LangSmith's audio-native
    voice evaluators can judge them. Attachments need the multipart ingestion
    path, which the low-level create_run does NOT drive (it silently drops
    them); RunTree does, so audio turns go through RunTree and text-only turns
    keep the simpler create_run.

    A no-op when tracing is off or there is nothing to score. Never raises."""
    if not LANGSMITH_TRACING:
        return
    customer = (customer or "").strip()
    reply = (reply or "").strip()
    if not customer and not reply:
        return
    client = _client_or_none()
    if client is None:
        return

    attachments: dict = {}
    try:
        if customer_pcm:
            attachments["customer_audio"] = ("audio/wav", _pcm16_to_wav(customer_pcm))
        if agent_pcm:
            attachments["agent_audio"] = ("audio/wav", _pcm16_to_wav(agent_pcm))
    except Exception:  # noqa: BLE001 — bad audio must not lose the text run
        log.warning("wav encode failed — tracing turn without audio", exc_info=True)
        attachments = {}

    try:
        now = datetime.now(timezone.utc)
        metadata = {
            "session_id": session_id,
            "channel": "voice_call",
            "language": language,
        }
        if tools:
            metadata["tools"] = tools

        if attachments:
            # RunTree is the only path that routes attachments to the multipart
            # ingestion endpoint. start==end: the turn is already over.
            from langsmith.run_trees import RunTree

            rt = RunTree(
                name="voice_call_turn",
                run_type="chain",
                id=uuid4(),
                start_time=now,
                inputs={"customer": customer},
                tags=["voice_call"],
                extra={"metadata": metadata},
                attachments=attachments,
                client=client,
                # project_name is OMITTED when None: RunTree validates it as a
                # string and raises a pydantic string_type error on None — which
                # killed every audio-path trace live (2026-08-05). Omitting it
                # lets RunTree read LANGSMITH_PROJECT from the env, i.e. the same
                # ambient project we wanted anyway.
                **({"project_name": project} if project else {}),
            )
            rt.end(outputs={"aisha": reply}, end_time=now)
            rt.post()
        else:
            # Text-only: a single create_run with start==end posts a COMPLETED
            # run. The SDK's background sender batches the actual HTTP.
            client.create_run(
                name="voice_call_turn",
                run_type="chain",
                inputs={"customer": customer},
                outputs={"aisha": reply},
                start_time=now,
                end_time=now,
                id=uuid4(),
                extra={"metadata": metadata},
                tags=["voice_call"],
                # Match the RunTree path: pass project_name only when set,
                # otherwise fall back to the ambient LANGSMITH_PROJECT.
                **({"project_name": project} if project else {}),
            )
    except Exception:  # noqa: BLE001 — tracing never disturbs a live call
        log.warning(
            "langsmith voice-turn trace failed — session=%s",
            (session_id or "")[:8],
            exc_info=True,
        )
