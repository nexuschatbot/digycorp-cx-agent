"""OpenAI speech-to-speech session — the `CALL_MODE=realtime` engine.

WHAT THIS IS. One vendor model hears the customer's audio and SPEAKS the reply
directly — no STT -> Claude -> TTS chain — which is what buys sub-second
replies (the ChatGPT-voice architecture). Chosen by explicit developer
decision 2026-07-27 after the pipeline's measured ~4 s floor; the pipeline
mode remains intact and one config line flips back.

WHAT IT IS NOT: a model with our data. THE TOOLS REMAIN OURS. The session is
given the same tool schemas the Claude agent binds (converted to the vendor's
function format), and every function call it makes is executed by OUR gateway
through the SAME dispatch choke point (app/agent/tool_node._dispatch): same
services, same write guards, same trusted-context injection, same audit rows.
The model talks; it cannot touch the database, and a fact it did not get from
a tool is a fact it invented — the same rule 2, now resting on GPT's
compliance rather than Claude's, which is the honest cost of this mode.

Like realtime_stt (its transcription-only sibling), this module speaks the
vendor dialect and surfaces NORMALISED events; the gateway owns what they
mean. Both beta and GA event spellings are accepted where they differ —
tolerance costs one string compare, a vendor rename mid-engagement costs a
dead call.

⚠️ HANDSHAKE VERIFIED ONLY BY LIVE USE, like everything on this vendor: the
offline suite scripts the normalised events. The full persona prompt and both
sides of the conversation flow to a US endpoint — the heaviest PDPL surface of
any mode; CLAUDE.md's D1 section must grow an entry before this ships beyond
dev.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable

from app.config import (
    CALL_STT_LANGUAGE,
    CALL_STT_MODEL,
    CALL_TURN_DETECTION,
    CALL_VAD_EAGERNESS,
    CALL_VAD_SILENCE_MS,
    CALL_VAD_THRESHOLD,
    OPENAI_API_KEY,
    REALTIME_AGENT_MODEL,
    REALTIME_VOICE,
)
from app.tools import TOOL_SCHEMAS

log = logging.getLogger("app.services.realtime_agent")

REALTIME_AGENT_URL = "wss://api.openai.com/v1/realtime?model={model}"
VENDOR_SAMPLE_RATE = 24_000
_CONNECT_TIMEOUT_S = 8.0


def _brief(description: str, limit: int = 240) -> str:
    """A tool description's OPENING — first paragraph, hard-capped.

    The full descriptions are briefings written for Claude and are re-counted
    on EVERY realtime response (~4-5k tokens of the ~11k that was blowing a
    40k-TPM tier three replies into a call, 2026-07-28). Truncation, not a
    rewrite: the first line of each briefing already carries its essence, and
    a mechanical cut cannot drift the way a hand-maintained copy would."""
    head = description.split("\n\n", 1)[0].strip()
    return head[:limit]


def _slim_schema(node):
    """The parameter schema with every nested description cut to its opening
    clause — same rationale and same mechanics as _brief: types, enums and
    required lists (the parts that BIND the model) pass through verbatim."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                out[key] = _brief(value, 90)
            else:
                out[key] = _slim_schema(value)
        return out
    if isinstance(node, list):
        return [_slim_schema(item) for item in node]
    return node


def _openai_tools() -> list[dict]:
    """The REGISTRY's schemas in the vendor's function format — names, types,
    enums and required lists verbatim (one source of truth, AD-H); every
    description trimmed to its opening for the per-response token budget."""
    return [
        {
            "type": "function",
            "name": schema["name"],
            "description": _brief(schema.get("description", "")),
            "parameters": _slim_schema(schema.get("input_schema", {"type": "object"})),
        }
        for schema in TOOL_SCHEMAS
    ]


class RealtimeAgentSession:
    """One speech-to-speech call session, normalised.

    Events delivered to `on_event` (always a dict with `type`), on the loop:
        {"type": "user_speech_started"}
        {"type": "user_speech_stopped"}
        {"type": "user_transcript", "text": str}      # what the customer said
        {"type": "assistant_delta", "text": str}      # her words, as spoken
        {"type": "audio", "data": bytes}              # her voice, PCM16 24k
        {"type": "response_started"}                  # a reply is being GENERATED
        {"type": "response_done"}                     # one reply finished
        {"type": "function_call", "name", "call_id", "arguments": dict}
        {"type": "agent_error", "detail": str}        # session unusable
        {"type": "agent_closed"}
    """

    def __init__(
        self,
        *,
        instructions: str,
        vocabulary: str,
        on_event: Callable[[dict], None],
        # ADDITIVE (CALL_PERSONA, 2026-07-29): None means the shipped defaults
        # (REALTIME_VOICE, the classic greeting text) — the pre-switch
        # behaviour, byte-for-byte. Only the natural persona passes these.
        voice: str | None = None,
        greeting: str | None = None,
    ) -> None:
        self._instructions = instructions
        self._vocabulary = vocabulary
        self._on_event = on_event
        self._voice = voice
        self._greeting = greeting
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._closed = False
        self._seen_kinds: set[str] = set()  # one unhandled-event log per kind
        self._rate_retries = 0  # per-response budget; reset when one starts
        # Whether the vendor currently has a response open. The rate-limit
        # retry consults it: if the VAD opened a new response while the retry
        # slept, re-asking would collide (live 2026-07-29 — the collision was
        # then misread as fatal and killed the call).
        self._response_active = False
        # The call's language pin (B of the language design, 2026-07-31):
        # applied to EVERY response this session creates — the turn reply,
        # its tool follow-ups, its rate-limit retries. Since 2026-08-03 the
        # gateway locks it once per call (at bootstrap, or on the first
        # clear utterance) and it changes at most that one time — the
        # re-pin plumbing below stays because it IS the locking mechanism.
        self._pinned_lang: str | None = None
        # WHICH response is open — so a cancel can name its target. A bare
        # response.cancel kills "the active response", and in the
        # cancel-then-reask sequence that races the vendor's own auto-create:
        # a stale cancel arriving after the NEXT reply opened would kill the
        # reply it was supposed to make room for. Named, it can only ever
        # kill the reply it meant (a done target is the benign
        # response_cancel_not_active).
        self._active_response_id: str | None = None
        # WHICH ITEM her voice belongs to — the handle conversation.item.truncate
        # needs. Deliberately NOT cleared on response.created or response.done:
        # the case that matters most is generation FINISHED while her audio is
        # still playing out (she writes several times faster than she speaks), and
        # a new response's `created` arrives while the PREVIOUS item is the one
        # being cut off. It is replaced when the next reply's own item is
        # announced, and cleared once truncated.
        self._active_item_id: str | None = None

    async def connect(self) -> bool:
        if not OPENAI_API_KEY:
            return False
        try:
            import websockets

            self._ws = await asyncio.wait_for(
                websockets.connect(
                    REALTIME_AGENT_URL.format(model=REALTIME_AGENT_MODEL),
                    additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    max_size=None,
                ),
                timeout=_CONNECT_TIMEOUT_S,
            )
            await self._send(self._session_update())
        except Exception as exc:  # noqa: BLE001 — a dead engine is a stated refusal upstream
            log.warning("realtime agent connect failed: %r", exc)
            await self.close()
            return False
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        return True

    def _turn_detection(self) -> dict:
        """The vendor's VAD decides WHEN THE CUSTOMER'S TURN ENDS — and only
        that. interrupt_response is pinned FALSE in both modes: with it true
        (the default), any speech_started — echo residue, a breath, a chair —
        cancelled whatever Aisha was saying, and two live calls on 2026-07-30
        lost 48 replies to exactly that. She is now interrupted only by OUR
        explicit response.cancel, which fires when the client CONFIRMS a real
        barge-in. A VAD misfire costs at most one spurious turn, never the
        reply in flight."""
        # create_response false too (2026-07-30): the vendor's VAD is demoted
        # to a SEGMENTER — it may end the customer's turn and commit the
        # audio, nothing more. Every reply is created by the GATEWAY, at
        # transcript time, once the words prove the turn deserves one: sounds
        # that transcribe to nothing, her own echo, and mid-reply "yeah"
        # backchannels used to auto-trigger replies and supersedes, which is
        # what kept chopping her speech (live, same day).
        if CALL_TURN_DETECTION == "semantic_vad":
            return {
                "type": "semantic_vad",
                "eagerness": CALL_VAD_EAGERNESS,
                "interrupt_response": False,
                "create_response": False,
            }
        return {
            "type": "server_vad",
            "silence_duration_ms": CALL_VAD_SILENCE_MS,
            "prefix_padding_ms": 300,
            # Above default (0.5): breaths and echo tails were becoming
            # committed "turns" of near-silence. Raised again 2026-08-03
            # (0.6 -> 0.75, per the vendor's own noisy-environment guidance)
            # and made config: CALL_VAD_THRESHOLD in .env tunes it.
            "threshold": CALL_VAD_THRESHOLD,
            "interrupt_response": False,
            "create_response": False,
        }

    def _session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                # The persona (+ the language pin, when one is already set).
                # Written for Claude, spoken by GPT — the known cost of this
                # mode; see the module docstring.
                "instructions": self._instructions + self._language_line(),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": VENDOR_SAMPLE_RATE},
                        # Transcription of the CUSTOMER's audio — the text the
                        # LANGUAGE POLICY reads. NO PROMPT, EVER, and this is
                        # now twice-measured law: the vocabulary catalogue
                        # hallucinated itself into the thread (2026-07-28),
                        # and a one-line "write Arabic in Arabic script"
                        # directive biased the transcriber so hard it wrote
                        # ENGLISH speech in Arabic script — flipping the pin
                        # and answering an English caller in Arabic (live
                        # 2026-08-01). Any prompt here corrupts more than it
                        # fixes. The romanized-Gulf-Arabic gap this leaves is
                        # accepted, documented, and belongs to a REAL Arabic
                        # transcriber (Munsit) — never to prompt nudging.
                        "transcription": {
                            "model": CALL_STT_MODEL,
                            # A language HINT (not a prompt): pins what the
                            # transcriber expects so short English utterances stop
                            # being mis-detected into random scripts. Omitted when
                            # blank, restoring auto-detect. See CALL_STT_LANGUAGE.
                            **({"language": CALL_STT_LANGUAGE} if CALL_STT_LANGUAGE else {}),
                        },
                        "turn_detection": self._turn_detection(),
                        # near_field, not far_field: attendees hold their OWN
                        # phone (the QR flow), which is a close-talking mic by
                        # definition — the vendor's own guidance for handsets.
                        "noise_reduction": {"type": "near_field"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": VENDOR_SAMPLE_RATE},
                        "voice": self._voice or REALTIME_VOICE,
                    },
                },
                "tools": _openai_tools(),
                "tool_choice": "auto",
            },
        }

    # ------------------------------------------------------------------ sends
    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None or self._closed:
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001
            self._fail("send_failed")

    async def send_audio(self, pcm: bytes) -> None:
        if pcm:
            await self._send({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            })

    async def send_tool_result(self, call_id: str, result: dict) -> None:
        """Hand a function call's result back and let her keep speaking —
        with the turn's language pin still attached: the follow-up after a
        tool call is the reply the customer actually HEARS, and it was the
        one path that used to go out unpinned (Arabic question -> English
        answer whenever a tool ran, live 2026-07-30)."""
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False, default=str),
            },
        })
        await self._send(self._response_payload())

    async def greet(self) -> None:
        """Aisha answers the call — she speaks FIRST, like a person picking up
        the phone. Dead air until the customer works out they must open is the
        single most bot-like thing a call can do. Response-level instructions
        REPLACE the session persona for this one response, so the directive is
        self-contained: who she is, and one short sentence."""
        await self._send({
            "type": "response.create",
            "response": {
                # The greeting obeys the call's locked language like every
                # other response: response instructions REPLACE the session
                # persona, so the language line must ride along with the
                # brief (no pin yet → empty line, English by default).
                "instructions": (self._greeting or (
                    "You are Aisha, Elite Group's assistant, answering a phone "
                    "call. Greet the caller warmly and briefly: say who you "
                    "are, mention in passing that the call is recorded "
                    "for quality purposes, and ask how you can help — one or "
                    "two short sentences. Do not list services or "
                    "vehicles."
                )) + self._language_line(),
            },
        })

    def _language_line(self) -> str:
        if not self._pinned_lang:
            return ""
        return (
            "\n\nLANGUAGE: Arabic ONLY — every word, in warm, spoken Gulf "
            "Arabic. Brand and model names may stay as they are. Never mix "
            "English into a reply, even if the customer mixed languages. "
            "This call stays in Arabic for its whole length: if the customer "
            "switches to English, understand them and answer what they said "
            "— in Arabic, without remarking on it."
            if self._pinned_lang == "ar"
            else "\n\nLANGUAGE: English ONLY — every word. Never mix Arabic "
            "into a reply, even if the customer mixed languages. This call "
            "stays in English for its whole length: if the customer switches "
            "to Arabic, understand them and answer what they said — in "
            "English, without remarking on it."
        )

    def _instructions_update(self) -> dict:
        """The pin at SESSION level (2026-08-01, the prompt-speaking fix):
        persona + language line become the session instructions, refreshed
        only when the pin CHANGES — so ordinary replies are BARE creates
        with no response-level instructions at all. A 6k-char instruction
        payload replacing the persona on every reply is what the model
        sometimes read ALOUD (live 2026-08-01); a bare create leaves it
        nothing to read but the conversation."""
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self._instructions + self._language_line(),
            },
        }

    def set_language(self, lang: str) -> None:
        """Re-pin WITHOUT requesting a reply — the gateway pins the session
        before anything is spoken (the absolute policy). Refreshes the
        session instructions when the pin actually changes."""
        if lang == self._pinned_lang:
            return
        self._pinned_lang = lang
        try:
            asyncio.get_running_loop().create_task(
                self._send(self._instructions_update())
            )
        except RuntimeError:  # no loop — connect() will send the config
            pass

    async def cancel_response(self) -> None:
        """Interrupt: stop the reply being generated — NAMED, so a cancel in
        flight can never hit the wrong reply (see _active_response_id)."""
        payload: dict = {"type": "response.cancel"}
        if self._active_response_id:
            payload["response_id"] = self._active_response_id
        await self._send(payload)

    async def truncate_reply(self, audio_end_ms: int) -> bool:
        """Tell the vendor HOW MUCH OF HER LAST REPLY THE CUSTOMER ACTUALLY
        HEARD, and let it drop the rest from the conversation.

        THE MISSING HALF OF EVERY INTERRUPTION (2026-08-04). Cutting her off
        silenced the audio on OUR side only: the vendor's copy of the
        conversation kept the WHOLE reply, so the model believed it had said
        everything the customer never heard. Measured on the dev VM: 68 of 106
        turns superseded a reply, and exactly ONE reached response.cancel —
        because she generates several times faster than she speaks, so by
        transcript time generation had finished and only the audio was still
        playing. Sixty-seven replies were cut mid-sentence with the vendor
        never told. Everything after such a turn is reasoning from a
        conversation that did not happen: she skips what she thinks she
        covered, or re-runs it when asked again.

        `audio_end_ms` MUST NOT EXCEED the item's real audio duration — the
        vendor errors if it does. The caller derives it from bytes the vendor
        itself sent us, so it cannot, by construction.

        Returns False when there is nothing to truncate: no item means she
        never got a word out, and a customer who heard nothing needs no
        correction — the reply is simply replaced."""
        item_id = self._active_item_id
        if not item_id or audio_end_ms <= 0:
            return False
        self._active_item_id = None  # one correction per reply
        await self._send({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": int(audio_end_ms),
        })
        return True

    def _response_payload(
        self, note: str | None = None, *, tools_off: bool = False
    ) -> dict:
        """Every reply request carries the language pin IN ITS OWN
        INSTRUCTIONS — the system slot for that one response, the most
        binding position the API offers (the greet mechanism, generalised).

        Why not a conversation note: tried first (2026-07-30) and defeated
        the same day — the note sat three items back while an English tool
        result sat adjacent, and the tool FOLLOW-UP reply came out English
        against an Arabic pin. Response instructions REPLACE the session
        persona for that response, so the persona rides along with the
        language line appended. The pin PERSISTS across the turn's tool
        follow-ups and retries until the next turn re-pins; None (never
        pinned) sends a bare create and the session persona applies.

        `note` is a ONE-SHOT corrective appended for this response only (the
        reference guard's redo speaks through it) — it never persists the way
        the pin does.

        REWRITTEN 2026-08-01 (the prompt-speaking fix): the pin lives at
        SESSION level now (_instructions_update), so the COMMON case is a
        BARE create — no response-level instructions to read aloud. Only a
        note or tools_off builds response instructions, and those must carry
        the full stack (persona + line + note) because response instructions
        REPLACE the session's for that reply."""
        if not note and not tools_off:
            return {"type": "response.create"}
        extra = ("\n\n" + note) if note else ""
        payload: dict = {
            "type": "response.create",
            "response": {
                "instructions": self._instructions + self._language_line() + extra
            },
        }
        if tools_off:
            # Response-level tool override (the silence nudge): a check-in
            # that CANNOT call a tool cannot say "let me check" and mean it —
            # the nudge once ran get_showrooms and resumed a conversation
            # nobody was having (live 2026-08-01). BOTH switches: `tools: []`
            # alone was measured live the same day being ignored — the nudge
            # called a tool anyway — so tool_choice "none" rides with it.
            payload["response"]["tools"] = []
            payload["response"]["tool_choice"] = "none"
        return payload

    async def request_response(
        self,
        lang: str | None = None,
        *,
        note: str | None = None,
        tools_off: bool = False,
    ) -> None:
        """Ask for a reply to the conversation as it now stands — the ONLY
        way a customer turn gets answered (create_response is false).

        `lang` pins the reply's language DETERMINISTICALLY: script detection
        decides, never model judgment — left to its own the model flipped to
        Arabic on an English conversation because the customer's NAME
        sounded Arabic (live 2026-07-30). Since 2026-08-03 the gateway only
        ever passes the call's LOCKED pin, so a change arrives here at most
        once per call (the unlocked-default → first-clear-utterance
        transition); None inherits the previous pin. `note` rides this one
        response's instructions and no further — see _response_payload.
        A pin CHANGE refreshes the session instructions first, so the bare
        create that follows already speaks under the new language."""
        if lang and lang != self._pinned_lang:
            self._pinned_lang = lang
            await self._send(self._instructions_update())
        await self._send(self._response_payload(note, tools_off=tools_off))

    async def seed_history(self, turns: list[tuple[str, str]]) -> None:
        """Prior conversation (speaker, text) pairs, so the call continues the
        chat instead of starting a stranger. Text only — the cheap half of the
        split-memory problem; the other half (call turns reaching the Claude
        checkpoint) is a known gap, documented at the gateway."""
        for speaker, text in turns:
            if not text:
                continue
            role = "assistant" if speaker == "agent" else "user"
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [{
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }],
                },
            })

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- reader
    def _emit(self, event: dict) -> None:
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 — a bad handler must not kill the reader
            log.exception("realtime agent event handler failed")

    def _fail(self, detail: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit({"type": "agent_error", "detail": detail})

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                kind = msg.get("type", "")
                if kind == "input_audio_buffer.speech_started":
                    # audio_start_ms/audio_end_ms: the VENDOR's own measure of
                    # how much speech it heard — evidence the gateway's byte
                    # counter cannot lose to timing (live 2026-08-01: a real
                    # Arabic utterance was silently dropped as 'hallucination'
                    # because our count missed the capture window).
                    self._emit({
                        "type": "user_speech_started",
                        "at_ms": msg.get("audio_start_ms"),
                    })
                elif kind == "input_audio_buffer.speech_stopped":
                    self._emit({
                        "type": "user_speech_stopped",
                        "at_ms": msg.get("audio_end_ms"),
                    })
                elif kind.endswith("input_audio_transcription.delta"):
                    # The transcript as it grows. Not decorative: the vendor
                    # sometimes never sends the COMPLETED event (live twice,
                    # 2026-08-01 — delta arrived, final never did, the
                    # customer got no reply). The gateway accumulates these
                    # so its fallback can answer from what was heard.
                    self._emit({
                        "type": "user_transcript_delta",
                        "text": msg.get("delta") or "",
                    })
                elif kind.endswith("input_audio_transcription.completed"):
                    self._emit({
                        "type": "user_transcript",
                        "text": (msg.get("transcript") or "").strip(),
                    })
                elif kind.endswith("input_audio_transcription.failed"):
                    # The gateway's turn engine waits on the transcript; a
                    # failed transcription must arrive as an EMPTY one, not
                    # as silence — its fallback timer is for the vendor
                    # never answering at all.
                    self._emit({"type": "user_transcript", "text": ""})
                elif kind in ("response.output_audio.delta", "response.audio.delta"):
                    try:
                        data = base64.b64decode(msg.get("delta") or "")
                    except Exception:  # noqa: BLE001
                        continue
                    if data:
                        self._emit({"type": "audio", "data": data})
                elif kind in (
                    "response.output_audio_transcript.delta",
                    "response.audio_transcript.delta",
                ):
                    self._emit({"type": "assistant_delta", "text": msg.get("delta") or ""})
                elif kind == "response.output_item.added":
                    # HER VOICE HAS A HANDLE NOW. Only a message item can be
                    # truncated, and only by id — without this the gateway can
                    # cut her audio off but cannot tell the vendor where, which
                    # is the whole desync (see truncate_reply).
                    item = msg.get("item") or {}
                    if item.get("type") == "message" and item.get("id"):
                        self._active_item_id = item["id"]
                        self._emit({"type": "reply_item", "item_id": item["id"]})
                elif kind == "response.output_item.done":
                    item = msg.get("item") or {}
                    if item.get("type") == "function_call":
                        try:
                            arguments = json.loads(item.get("arguments") or "{}")
                        except (ValueError, TypeError):
                            arguments = {}
                        self._emit({
                            "type": "function_call",
                            "name": item.get("name") or "",
                            "call_id": item.get("call_id") or "",
                            "arguments": arguments,
                        })
                elif kind == "response.created":
                    self._response_active = True
                    self._active_response_id = (
                        (msg.get("response") or {}).get("id") or None
                    )
                    # Surfaced to the gateway (2026-07-30): a reply exists
                    # from CREATION, seconds before its first audio. The turn
                    # engine judged "is a reply in flight?" by audio alone,
                    # so an interruption during the generation phase went
                    # unrecognised — nothing was cancelled, and the customer
                    # heard the whole superseded reply before their words
                    # were addressed (live, same day).
                    self._emit({"type": "response_started"})
                elif kind == "response.done":
                    self._response_active = False
                    self._active_response_id = None
                    # A response can END WITHOUT EVER SPEAKING — cancelled, or
                    # FAILED (the silent-death mode a live call hit on
                    # 2026-07-27: turns transcribed, nothing answered). Surface
                    # the vendor's reason instead of swallowing it.
                    response = msg.get("response") or {}
                    status = response.get("status")
                    if status in (None, "completed"):
                        # Only a COMPLETED reply resets the retry budget. It
                        # used to reset on response.created — but the retry
                        # itself creates a response, so every failure was
                        # "attempt 1/3" forever: an unbounded spiral whose own
                        # ~6k-token re-asks kept the TPM window saturated and
                        # made the next failure certain (live 2026-07-29,
                        # minutes of it). Consecutive failures now genuinely
                        # accumulate to the cap.
                        self._rate_retries = 0
                    else:
                        details = response.get("status_details") or {}
                        log.warning(
                            "realtime agent response %s: %s",
                            status, json.dumps(details)[:600],
                        )
                        error = details.get("error") or {}
                        # RATE-LIMITED IS NOT DEAD (live 2026-07-28, thrice):
                        # the vendor names the exact wait. Waiting it out and
                        # re-asking turns "she ignores you" into "she takes a
                        # breath" — bounded, so a hard-capped account still
                        # gives up honestly after three tries.
                        if (
                            error.get("code") == "rate_limit_exceeded"
                            and self._rate_retries < 3
                        ):
                            import re as _re

                            match = _re.search(
                                r"try again in ([0-9.]+)s", error.get("message") or ""
                            )
                            delay = min(float(match.group(1)) if match else 4.0, 15.0) + 0.4
                            self._rate_retries += 1
                            log.info(
                                "realtime agent rate-limited; retrying in %.1fs (attempt %d/3)",
                                delay, self._rate_retries,
                            )

                            async def _retry(d: float = delay) -> None:
                                await asyncio.sleep(d)
                                # Stand down if the VAD opened a response while
                                # we slept — the customer spoke again and THAT
                                # is the conversation now; re-asking would
                                # collide (conversation_already_has_active_
                                # response, live 2026-07-29).
                                if not self._closed and not self._response_active:
                                    await self._send(self._response_payload())

                            asyncio.get_running_loop().create_task(_retry())
                            # No response_done: the gateway stays in "thinking"
                            # — which is the truth — until the retry speaks.
                            continue
                        if error.get("code") == "rate_limit_exceeded":
                            # Three consecutive waits did not clear it: the
                            # org's TPM window is saturated beyond this call's
                            # reach. END, with the honest reason — a call that
                            # sits silently absorbing rate limits is the worst
                            # of both worlds (and its own retries were feeding
                            # the saturation, live 2026-07-29).
                            self._fail("rate_limited")
                            return
                    self._emit({"type": "response_done"})
                elif kind == "conversation.item.create.error":
                    log.warning("realtime agent item rejected: %s", str(msg)[:400])
                elif kind == "error":
                    err = msg.get("error") or {}
                    code = str(err.get("code") or "")
                    # Benign RACES, matched by their exact vendor codes — a
                    # fuzzy substring match here killed a live call on
                    # 2026-07-29 ("active response" with a space missed the
                    # code's underscores). Both mean "the conversation moved
                    # on without you", which is a shrug, not a death:
                    #   response_cancel_not_active — we cancelled a reply that
                    #     had already finished (double barge / late barge, or
                    #     a named supersede cancel whose target completed);
                    #   conversation_already_has_active_response — our re-ask
                    #     (supersede or rate-limit retry) raced a response
                    #     that was still active — with create_response pinned
                    #     false that is one of OUR OWN creates, typically the
                    #     very reply the supersede just tried to cancel.
                    # INFO, not WARNING: expected traffic, not a fault. The
                    # already-active refusal is also SURFACED to the gateway
                    # (response_rejected): a refused supersede re-ask means no
                    # response_started is coming to lift the stale-audio
                    # embargo, and swallowing it silently muted a call for
                    # good (the mute-forever fix, 2026-08-03).
                    if code in (
                        "response_cancel_not_active",
                        "conversation_already_has_active_response",
                    ):
                        log.info("realtime agent benign race: %s", code)
                        if code == "conversation_already_has_active_response":
                            self._emit({"type": "response_rejected", "code": code})
                        continue
                    log.warning("realtime agent vendor error: %s", err)
                    self._fail(code or str(err.get("message") or "vendor_error"))
                    return
                else:
                    if kind not in self._seen_kinds:
                        self._seen_kinds.add(kind)
                        log.info("realtime agent event (unhandled): %s", kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime agent reader died: %s", exc)
            self._fail("reader_died")
            return
        if not self._closed:
            self._closed = True
            self._emit({"type": "agent_closed"})


def make_agent_session(
    *,
    instructions: str,
    vocabulary: str,
    on_event: Callable[[dict], None],
    voice: str | None = None,
    greeting: str | None = None,
) -> RealtimeAgentSession | None:
    """The gateway's seam, and the tests': monkeypatch THIS to script a call.

    voice/greeting are the CALL_PERSONA extras — the gateway omits them
    entirely in classic mode, so a classic call is the exact pre-switch call
    (and the classic-path test fakes, which accept only the original three
    kwargs, prove that structurally)."""
    if not OPENAI_API_KEY:
        return None
    return RealtimeAgentSession(
        instructions=instructions,
        vocabulary=vocabulary,
        on_event=on_event,
        voice=voice,
        greeting=greeting,
    )
