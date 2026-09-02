"""Environment configuration for the Elite Test Drive backend.

Loads settings from a `.env` file (via python-dotenv) with sensible dev
defaults. Secrets (ANTHROPIC_API_KEY, AZURE_FOUNDRY_API_KEY) live here
server-side only — never in the frontend, never committed. See CLAUDE.md "Stack".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from dotenv import load_dotenv

# Load `.env` if present. Real environment variables always take precedence.
load_dotenv()


def _get_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` when unset or blank."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float(name: str, default: float) -> float:
    """Read a float env var, falling back to `default` when unset or blank."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _get_bool(name: str, default: bool) -> bool:
    """Read a boolean env var, falling back to `default` when unset or blank.
    Accepts the usual spellings so a `.env` written by hand behaves as expected."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Model provider switch
# ---------------------------------------------------------------------------
# Two ways to reach the same Anthropic Messages API surface (AD-A): direct, or
# via Azure AI Foundry. The clients differ ONLY in construction — both expose
# `client.messages.stream(...)` identically, so nothing downstream of the switch
# changes. Default is the direct path (see graph._build_client).
#
# The model id is deliberately a SEPARATE var per provider: Foundry addresses a
# *deployment name*, the direct API addresses a *model id*. They are different
# namespaces, and sending one to the other 404s. Keeping them apart means
# flipping the provider flag alone can never produce that mismatch.
USE_DIRECT_ANTHROPIC: bool = _get_bool("USE_DIRECT_ANTHROPIC", True)

# Direct Anthropic API — server-side only. No default; empty until set.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Azure AI Foundry credentials — server-side only. The Foundry client targets the
# native Anthropic Messages API surface (AD-A); `resource` builds the endpoint
# https://<resource>.services.ai.azure.com/anthropic/. No defaults; empty until set.
AZURE_FOUNDRY_API_KEY: str = os.getenv("AZURE_FOUNDRY_API_KEY", "")
AZURE_FOUNDRY_RESOURCE: str = os.getenv("AZURE_FOUNDRY_RESOURCE", "")

# Database URL. Dev default is local SQLite; swap to Postgres via env only.
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./elite_demo.db")

# Azure Foundry DEPLOYMENT NAME for the agent (tool use). Only read on the
# Foundry path — see ANTHROPIC_MODEL above for the direct path.
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

# The model the agent node actually passes to messages.stream(...). Resolved once,
# here, so exactly one place knows which id belongs to which provider.
AGENT_MODEL: str = ANTHROPIC_MODEL if USE_DIRECT_ANTHROPIC else CLAUDE_MODEL

# Reasoning effort for the agent's model calls (2026-07-27). Sonnet 4.6 runs at
# effort "high" when the parameter is omitted — the slowest setting — and the
# vendor's own migration guidance flags that unset default as a latency and
# token cost you did not choose, recommending "medium" for tool-heavy agents.
# So every turn of this demo was paying maximum deliberation, including one-word
# answers. Tunable per environment without a code change; set empty to omit the
# parameter and fall back to the provider default.
AGENT_EFFORT: str = os.getenv("AGENT_EFFORT", "medium").strip().lower()

# HOW MANY TIMES AISHA MAY GO BACK TO THE SPECIALISTS IN ONE TURN.
#
# MEASURED, NOT GUESSED (2026-08-31, a 15-turn conversation on a clean db). One
# question about Islamic finance and the on-road price ran THREE cascades back
# to back — 5,333ms, 3,470ms, 3,220ms — and the turn took 33 seconds. Answering
# a consent chip ran two, for 22 seconds. Eleven cascades across fifteen turns.
#
# HER PROMPT ALREADY FORBIDS THIS, word for word: "Do not send a brief, read
# what comes back, and then send another: each round trip is another wait the
# customer sits through, and three of them in a row is half a minute of
# nothing." She does it anyway, which is the lesson agent/sanitise.py records
# about rule 17 and subagent.py records about the ids — asking harder is not
# the fix, enforcing is.
#
# TWO, NOT ONE, and the difference is the point. Her prompt permits going back
# "for something the answers themselves revealed", and that is a real case; a
# ceiling of one would trade seconds for thinner answers. Two kills the spiral
# and keeps the legitimate follow-up.
#
# A NUMBER, NOT A CONSTANT, because whether this costs answer quality is an
# open question that only a re-run can settle. Raise it, or set it to 0 to
# disable the ceiling entirely, without a deploy.
MAX_CASCADES_PER_TURN: int = _get_int("MAX_CASCADES_PER_TURN", 2)

# Write the cached prompt prefix once at startup, so the first customer reads
# the cache instead of paying to create it. Costs ONE live API call per boot,
# which is why it is OFF by default: a dev restarting uvicorn all day should not
# be billed for it. Turn it ON for the demo, where a QR-code wave means many
# first messages land at once and would otherwise all miss together.
PREWARM_PROMPT_CACHE: bool = _get_bool("PREWARM_PROMPT_CACHE", False)

# Multi-agent routing (S7). ON. Every turn is classified into a journey agent
# — testdrive or bookingmanagement — which then gets its own prompt body and its
# own tool allowlist.
#
# THIS IS THE KILL SWITCH, and it exists because this is the first change in the
# migration that alters what a customer experiences. Off, run_turn sets no agent,
# the graph falls through to the combined prompt and the whole 22-tool registry,
# and behaviour is byte-identical to the single-agent system. If routing accuracy
# disappoints in turn_trace, this is one env var away from the old behaviour and
# needs no deploy of different code.
#
# The realtime speech-to-speech channel ignores this entirely: it bypasses the
# graph by design (architecture doc §9) and keeps its own compact persona.
ROUTER_ENABLED: bool = _get_bool("ROUTER_ENABLED", True)

# Elite's customer helpline (rule 13, FR-ES). Aisha gives this number whenever
# she points a customer to something OUTSIDE the chat — a showroom visit, the
# sales team, a callback she has arranged — so no one is ever sent away without a
# way to reach a human. An in-chat escalation she logs herself (request_human_
# callback) does not need it; this is for anything the customer must do on their
# own. A business constant, not customer data, so it lives in config, not the DB.
ELITE_HELPLINE: str = os.getenv("ELITE_HELPLINE", "800-535483")

# --------------------------------------------------------------------------- #
# Retrieval (T1.1-T1.6)
# --------------------------------------------------------------------------- #
# Cache for the retrieval index and its chunks, and NOTHING ELSE — see
# services/cache.py. Empty or unreachable is a supported state: the index is
# rebuilt in process (~14s once) and every turn behaves identically.
REDIS_URL: str = os.getenv("REDIS_URL", "").strip()

# THE KILL SWITCH, same pattern as ROUTER_ENABLED. Off by default, deliberately:
# both halves of retrieval — the search_knowledge tool and the additive
# catalogue pass — change what a customer is shown, and the T0.1 baseline has
# not been re-measured against them. Flip it to see M2; flip it back with no
# deploy.
#
# OFF MUST MEAN OFF. With this false, catalogue.search runs byte-identically to
# how it ran before Phase 1 — not "runs the vector pass and discards it".
RETRIEVAL_ENABLED: bool = _get_bool("RETRIEVAL_ENABLED", False)

# --------------------------------------------------------------------------- #
# Multi-agent (Phase 5)
# --------------------------------------------------------------------------- #
# THE KILL SWITCH, same pattern as ROUTER_ENABLED. Off by default: the whole
# point of shipping behind a flag is that the single-agent path stays the one
# customers meet until the baseline has been re-measured against the new one.
#
# PER CHANNEL, NOT GLOBAL. app/orchestrator/turn.enabled() excludes voice even
# when this is true — `run_turn` is shared, and realtime has a 400ms first-audio
# budget that a fan-out cannot meet (the measured Haiku round trip alone is
# 1088ms p50). Voice keeps its own fast path until Phase 9.
MULTI_AGENT_ENABLED: bool = _get_bool("MULTI_AGENT_ENABLED", False)

# RETRIEVAL-FIRST (2026-09-02). Off by default, same contract as the flags
# above: flipping it changes what a customer meets and the quality run that
# earns a default lives on the test VM, not in this file.
#
# WHAT IT DOES. On the multi-agent chat path, the customer's message is run
# through the LOCAL retrieval index (~50ms, no model, no tokens) BEFORE
# Aisha's first generation, and the top passages ride into her volatile
# context as a pre-supplied search_knowledge result. A fact question —
# warranty, delivery, registration, colours, accessories copy — is then one
# Sonnet call instead of the full cascade: measured live, that cascade spent
# five model calls (~14s) to execute a document lookup the index answers in
# milliseconds. The grounding rule is intact: the passages ARE a tool result,
# delivered early. When they do not cover the question, or the turn needs an
# action, she briefs exactly as before — the DELEGATING prose says so.
RETRIEVAL_FIRST: bool = _get_bool("RETRIEVAL_FIRST", False)

# Slot lock TTL (minutes) — abandoned conversations release inventory (FR-SL).
SLOT_LOCK_TTL_MINUTES: int = _get_int("SLOT_LOCK_TTL_MINUTES", 10)

# Rolling horizon (days) over which slots are generated/offered.
SLOT_HORIZON_DAYS: int = _get_int("SLOT_HORIZON_DAYS", 14)

# Minimum lead time (minutes) before a slot may be OFFERED or LOCKED. "Not in the
# past" is not enough: a slot starting in four minutes is real inventory the
# customer cannot physically reach, and offering it is a promise we know is
# false. Enforced server-side in slots.query_available, so a too-soon slot never
# reaches the model in the first place — the same structural approach as the
# Friday-prayer window (rule 6): the agent cannot offer what it is never given.
# Deliberately NOT applied to booking.create/reschedule — see slots._lead_cutoff.
SLOT_MIN_LEAD_MINUTES: int = _get_int("SLOT_MIN_LEAD_MINUTES", 30)

# ---------------------------------------------------------------------------
# Voice notes — INPUT ONLY (product decision 2026-07-21; see CLAUDE.md scope)
# ---------------------------------------------------------------------------
# A THIRD provider, and the only one that is not Anthropic-shaped: Anthropic has
# no audio API, so transcription cannot reuse the switch above. Server-side only.
#
# Audio leaves the UAE to a US endpoint — the one place in this system that
# sends customer data out of region. Flagged on the D1 PDPL checklist, not
# silently accepted. Azure Foundry was checked first and has no transcription
# deployment (and `egapi` is East US, so it would not have helped residency).
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
STT_MODEL: str = os.getenv("STT_MODEL", "gpt-4o-transcribe")

# Recording cap. Enforced in the BROWSER (auto-stop) and again HERE, because the
# browser bound is a UI affordance and this one is the actual limit.
# 30 s as of 2026-07-21 (was 60). Keep in step with MAX_RECORDING_MS in the
# frontend's useVoice.ts — they are two halves of one rule, and a browser that
# records longer than the server accepts turns a valid recording into a rejected
# upload, which reads to the customer as the mic being broken.
VOICE_MAX_SECONDS: int = _get_int("VOICE_MAX_SECONDS", 30)

# Hard byte ceiling — the blob goes into a DB column, so an unbounded upload is
# a database problem, not merely a slow request. ~8 MB is far above 30 s of
# Opus/AAC speech; it exists to stop abuse, not to shape normal recordings.
VOICE_MAX_BYTES: int = _get_int("VOICE_MAX_BYTES", 8 * 1024 * 1024)

# Retention (hours) before a stored recording is purged. 48h survives a
# rehearsal-to-demo overnight and is short enough to defend on the PDPL
# checklist. "Forget me" (rule 12) deletes immediately and does not wait for it.
VOICE_RETENTION_HOURS: int = _get_int("VOICE_RETENTION_HOURS", 48)

# ---------------------------------------------------------------------------
# Read-aloud / TTS (product decision 2026-07-21; see CLAUDE.md scope override)
# ---------------------------------------------------------------------------
# Server-generated speech for Aisha's replies, on request. Reuses OPENAI_API_KEY
# above — same vendor, same optionality: unset, the frontend falls back to the
# browser's on-device voice and the feature degrades rather than disappearing.
#
# ⚠️ HEAVIER PDPL EXPOSURE THAN THE STT PATH, and it should not be conflated with
# it. Speech-to-text sent the CUSTOMER'S OWN AUDIO. This sends AISHA'S REPLIES,
# which quote the customer back to themselves: "confirmed for Saturday, we'll
# text +971 50 555 0103." That is a name, a mobile and a booking reference
# leaving the UAE. Its own D1 checklist entry, not a footnote to the last one.
TTS_MODEL: str = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")

# ONE VOICE PER LANGUAGE, chosen by ear (2026-07-21 listening spike).
#
# THE TRADE-OFF, stated so it is a decision and not an accident: Aisha is ONE
# persona, and two voices means her voice CHANGES when a customer switches
# language mid-conversation — which is S-19, a scenario the demo runs on
# purpose. Each language gets its best voice at the cost of sounding like two
# people to a bilingual listener. Set both to the same value to trade the other
# way; nothing else changes.
TTS_VOICE_EN: str = os.getenv("TTS_VOICE_EN", "coral")
TTS_VOICE_AR: str = os.getenv("TTS_VOICE_AR", "shimmer")

# gpt-4o-mini-tts accepts a plain-English TONE INSTRUCTION, and it is the reason
# this model was chosen over tts-1 — it was audibly warmer in BOTH languages in
# the spike, at effectively the same price. This is the only place in the system
# where rule 14's register is expressed to a model that is not Claude, so it is
# deliberately phrased as rule 14 phrases it: offer, never push.
TTS_INSTRUCTIONS: str = os.getenv(
    "TTS_INSTRUCTIONS",
    "Warm, unhurried Gulf hospitality. Speak like a welcoming showroom host in "
    "Dubai — friendly and calm, never rushed, never salesy. Let numbers land "
    "clearly.",
)

# Refuse to synthesise beyond this. A guard against cost and latency, not a
# quality rule: Aisha's replies are chat-register (rule 17) and run well under
# it, so anything longer is a bug or an abuse, and both deserve a refusal.
TTS_MAX_CHARS: int = _get_int("TTS_MAX_CHARS", 4000)

# ---------------------------------------------------------------------------
# Voice call — the WS call channel (voice phase; AD-C amendment, see CLAUDE.md)
# ---------------------------------------------------------------------------
# A hands-free "call" with the SAME agent: mic audio up a WebSocket, STT, the
# ordinary LangGraph turn (run_turn), TTS of the reply back down. WS exists for
# THIS CHANNEL ONLY — /api/chat stays SSE, untouched.
#
# EXPLICITLY FLAGGED, unlike STT/TTS above which key purely on OPENAI_API_KEY
# being set: a call streams the customer's CONTINUOUS live audio to a US
# endpoint for its whole duration (its own D1 PDPL entry, heavier than the
# voice-note one), and it spends vendor money per minute — so it must be a
# decision, never a side effect of a key existing in .env.
#
# ⚠️ docker-compose's backend.environment block is an explicit ALLOWLIST (the
# LangSmith lesson): every CALL_* var here must be NAMED there or a value in
# the root .env silently never reaches the container.
CALL_ENABLED: bool = _get_bool("CALL_ENABLED", False)

# Hard ceiling on one call's duration — the cost guard. STT + TTS run
# ~$0.02-0.03/min, so an abandoned-but-open call is a slow leak; at the cap the
# gateway hangs up gracefully rather than billing silence.
CALL_MAX_MINUTES: int = _get_int("CALL_MAX_MINUTES", 10)

# Hang up after this much continuous silence (nobody speaking, nothing
# generating). The call-channel sibling of rule 19's idle ladder: an abandoned
# call must release its resources, not sit open to the CALL_MAX_MINUTES cap.
CALL_SILENCE_HANGUP_SECONDS: int = _get_int("CALL_SILENCE_HANGUP_SECONDS", 60)
# The call's first idle rung (rule 19's ladder, spoken): after this much
# silence Aisha gently checks they are still there — once per silence stretch —
# and the hangup above stays measured from the SAME anchor, so the nudge never
# postpones it.
#
# SILENCE IS MEASURED FROM WHOEVER SPOKE LAST, hers included (2026-08-04). It
# used to run from GENERATION end, which on a long reply is many seconds before
# she stops talking — so the check-in landed on top of her own sentence. See
# RealtimeCallSession._extend_speaking. 10s of real quiet, product decision the
# same day (was 12 from the wrong anchor, i.e. sooner than it read).
CALL_NUDGE_SECONDS: int = _get_int("CALL_NUDGE_SECONDS", 10)

# How long the line stays open AFTER her farewell has finished leaving the
# server (2026-08-04, the goodbye-cutoff fix).
#
# An empty send queue means the last frame reached the NETWORK, not an ear. The
# browser's jitter buffer still holds it — 300-500ms on a TURN-relayed path,
# more on a poor mobile network — and tearing the peer connection down discards
# whatever is left. The old value was a hardcoded 0.3s, which with the client's
# own 350ms grace came to ~0.65s: under the buffer on a relayed call, so the
# last word or two of every goodbye died. A second is comfortably over it and
# is imperceptible at the end of a call that is already finished.
#
# Config rather than a constant because the right value depends on the NETWORK,
# not on us — it can be tuned against a real phone without a redeploy.
CALL_FAREWELL_TAIL_SECONDS: float = _get_float("CALL_FAREWELL_TAIL_SECONDS", 1.0)

# End-of-turn threshold for server VAD (P3+): this long below the speech
# threshold ends the customer's turn. Client-side v0 detection mirrors it. Too
# short clips slow speakers mid-sentence; too long makes every reply feel late —
# tune by rehearsal, which is why it is config and not a constant.
CALL_VAD_SILENCE_MS: int = _get_int("CALL_VAD_SILENCE_MS", 900)

# Per-utterance cap WITHIN a call. Looser than VOICE_MAX_SECONDS (a composed
# voice note is one bounded thought; call speech runs longer), bounded because
# each utterance is buffered for VoiceNote storage — the byte ceiling
# VOICE_MAX_BYTES applies to it unchanged.
CALL_UTTERANCE_MAX_SECONDS: int = _get_int("CALL_UTTERANCE_MAX_SECONDS", 60)

# The model's effort dial FOR CALL TURNS ONLY (text chat keeps AGENT_EFFORT).
# Measured 2026-07-27: at "medium" the model reasons invisibly for seconds
# before its first word — thoroughness a reader never notices and a LISTENER
# hears as dead air. "low" trades a little deliberation for a reply that
# starts now, which on a phone is the right trade. Empty string = no override.
CALL_AGENT_EFFORT: str = os.getenv("CALL_AGENT_EFFORT", "low").strip().lower()

# The call's OWN TTS tone (2026-07-27). The showroom-host instruction reads
# beautifully on a tapped message; spoken down a phone it performs — measured
# by ear as "she is reading". A call wants conversation, not narration.
CALL_TTS_INSTRUCTIONS: str = os.getenv(
    "CALL_TTS_INSTRUCTIONS",
    "You are chatting on a relaxed phone call. Natural everyday cadence, easy "
    "contractions, small warm inflections; brisk but never rushed. Sound like "
    "a friendly consultant talking, not a narrator reading text aloud.",
)

# OPTIONAL faster model for call turns only (empty = same model as chat).
# The measured floor of a call reply is the model's first token (~1.5 s steady
# on the main model); a lighter tier answers in a fraction of that at some
# cost to conversational judgement. A product trade, so it ships OFF.
CALL_AGENT_MODEL: str = os.getenv("CALL_AGENT_MODEL", "").strip()

# OPTIONAL faster model for SIMPLE chat turns (empty = off, the shipped
# default). OPEN-ISSUES #2: the agent model's fixed cost is 2,401ms a call and
# 20ms an output token, and a bare "hi" pays all of it. A turn is "simple" only
# when a deterministic rule says so — today that is router/prefilter's bare
# greeting and nothing else, because every other cheap-looking turn (an
# affirmation, a consent answer) sits inside the booking sequence where
# conversational judgement is the product. OFF BY DEFAULT for the same reason
# CALL_AGENT_MODEL is: which model greets a customer is a product trade, and
# the quality/latency numbers to decide it need the measured run the issue
# calls for. Same trusted-flag shape as CALL_AGENT_MODEL above.
SIMPLE_TURN_MODEL: str = os.getenv("SIMPLE_TURN_MODEL", "").strip()

# Which engine answers a call (2026-07-27, developer decision after latency
# testing — pros/cons recorded in the conversation and CLAUDE.md is to be
# updated when this settles):
#   "pipeline" — the original chain: STT -> the Claude agent -> TTS. One brain
#                across channels, strongest rules; ~4 s to first word.
#   "realtime" — OpenAI speech-to-speech (gpt-realtime) speaks AS Aisha,
#                sub-second replies. The TOOLS remain ours — the same REGISTRY,
#                write guards and audit choke point via function calling — but
#                the conversational judgement is GPT's, and call turns do not
#                enter the Claude checkpoint (split-memory caveat).
CALL_MODE: str = os.getenv("CALL_MODE", "pipeline").strip().lower()

# The speech-to-speech model + its voice ("marin"/"cedar" are the
# conversation-tuned pair; "gpt-realtime-mini" is ~4x cheaper and noticeably
# less careful with tools).
REALTIME_AGENT_MODEL: str = os.getenv("REALTIME_AGENT_MODEL", "gpt-realtime")
REALTIME_VOICE: str = os.getenv("REALTIME_VOICE", "marin")

# HOW the vendor decides the customer's turn has ended (2026-07-30):
#   "server_vad"   — volume + silence_duration_ms. Simple, predictable.
#   "semantic_vad" — the vendor's classifier reads the WORDS and waits out a
#                    mid-thought pause ("I'd like to book... umm"). Community
#                    reports are mixed, so it ships as a flag, not a default:
#                    one test call decides, one line flips back.
# Deciding the turn is ALL it may do either way — interrupt_response is pinned
# false in the session (a VAD blip may never cancel Aisha's reply; 48 replies
# died that way across two calls, live 2026-07-30). Interruption is OURS: the
# client's barge detector → the gateway's explicit response.cancel.
CALL_TURN_DETECTION: str = os.getenv("CALL_TURN_DETECTION", "server_vad").strip().lower()
# semantic_vad only. low = wait longest before replying, high = jump in fast;
# "auto" is the vendor's medium. Ignored under server_vad.
CALL_VAD_EAGERNESS: str = os.getenv("CALL_VAD_EAGERNESS", "auto").strip().lower()

# HOW call audio travels between browser and backend (2026-07-30, developer
# decision — the "best there is" rebuild):
#   "webrtc" — audio over an aiortc peer connection (native AEC3 echo
#              cancellation, Opus bandwidth, jitter handling); the WS keeps
#              signaling and every JSON event. Degrades to ws per-call when
#              negotiation fails.
#   "ws"     — the original raw-PCM-over-WebSocket path, kept intact.
CALL_TRANSPORT: str = os.getenv("CALL_TRANSPORT", "webrtc").strip().lower()
# Optional STUN server for the peer connection. Empty = host candidates only,
# which covers localhost dev ONLY — see the TURN note below for why a deployed
# VM needs more than this despite what the original comment here claimed.
RTC_STUN_URL: str = os.getenv("RTC_STUN_URL", "").strip()

# TURN RELAY (2026-08-04) — what actually makes the call connect off localhost.
#
# ⚠️ HOST CANDIDATES DO NOT COVER "a public-IP VM", which is what this section
# used to say. Measured on the dev VM: 13 negotiations, 13 failures, never once
# `connected`. The backend runs in a Docker container, so the only address it
# can offer is its bridge IP (172.18.0.4) behind TWO NATs — Docker's masquerade
# and Azure's. The browser, meanwhile, offers only its own LAN address. Two
# private addresses cannot pair, so ICE has nothing to work with and every call
# silently falls back to WS-PCM — the path with no native AEC3, i.e. the echo
# that makes her interrupt herself and answer her own voice.
#
# STUN ALONE DOES NOT FIX IT. Discovering our public address still leaves the
# inbound pinhole to punch through two layers of NAT whose filtering is
# endpoint-dependent, and the result would vary with whatever network the
# customer happens to be on. A relay removes the guesswork: both sides connect
# OUTBOUND to it, which every network permits.
#
# ONLY THE SERVER NEEDS THESE. Our relay candidate lives on the VM's public IP,
# so the browser can reach it from anywhere — it needs no credentials and no
# code change. Handing TURN credentials to the client would need its own
# endpoint and its own expiry story, and buys nothing until some client network
# proves it cannot reach us.
#
# Empty = no TURN, exactly the previous behaviour. The WS-PCM fallback is
# untouched either way: a relay that is missing, misconfigured or unreachable
# costs call QUALITY, never the call (AD-M).
RTC_TURN_URL: str = os.getenv("RTC_TURN_URL", "").strip()
RTC_TURN_USERNAME: str = os.getenv("RTC_TURN_USERNAME", "").strip()
# NOT `.strip()`ed of anything but whitespace, and never logged: this is a
# credential. It reaches the container from the environment's own deploy .env
# (mode 600, scp'd separately, excluded from the rsync) — never a repo, never
# an image layer.
RTC_TURN_CREDENTIAL: str = os.getenv("RTC_TURN_CREDENTIAL", "").strip()

# THE BROWSER'S OWN STUN SERVER (2026-08-04) — handed to it in `call_started`.
#
# ⚠️ THE SERVER-SIDE RELAY ALONE IS NOT ENOUGH, learned by measurement after
# the relay was proven working: our answer carried a real public relay
# candidate and the call STILL never reached `connected`. The browser creates
# its peer connection with no ICE servers, so it offers only host candidates —
# which Chrome publishes as mDNS `.local` names. aiortc cannot resolve those,
# so our side ends up with NO usable address for the browser, and a TURN relay
# only forwards from peer addresses it has been given permission for. Both
# failures have the same cure: the browser needs a STUN server so it discovers
# and offers its own PUBLIC address.
#
# Deliberately SEPARATE from RTC_STUN_URL: that one is what THIS PROCESS uses
# and can name a private address (the container reaches coturn at 10.0.0.4);
# this one is sent to a browser on the internet and must be publicly
# resolvable. Collapsing them would ship a private address to customers and
# fail silently.
#
# NO CREDENTIALS ARE INVOLVED — STUN binding is unauthenticated, so nothing
# secret reaches the client. Pointing it at our OWN coturn (rather than a
# public Google STUN server) also keeps customer IPs off a third party, which
# matters for the D1 PDPL checklist.
#
# Empty = the client gets an empty list and behaves exactly as before, which is
# correct for localhost (both ends are the same machine, host candidates work).
RTC_CLIENT_STUN_URL: str = os.getenv("RTC_CLIENT_STUN_URL", "").strip()

# ICE DIAGNOSTICS. aioice logs every connectivity check and its verdict at
# DEBUG — the only thing that can say WHICH candidate pair was tried and why it
# failed. Off by default: per-packet chatty, and the check lines carry the
# customer's public IP unredacted. Turn on to diagnose, then turn off.
RTC_DEBUG: bool = _get_bool("RTC_DEBUG", False)


# THE BROWSER'S TURN RELAY (2026-08-04) — its PUBLIC url, credentials included.
#
# ⚠️ "ONLY THE SERVER NEEDS CREDENTIALS" WAS WRONG, and it cost two rounds.
# STUN alone gets the browser a public ADDRESS but leaves its media and
# connectivity checks aimed at our relay candidate's HIGH UDP PORT (49160-49200).
# Measured on the dev VM: with STUN configured and both sides holding good public
# candidates, every packet was outbound and not one came back — a network that
# permits UDP to 3478 (the browser reached our STUN there) but drops high ports
# looks exactly like this, and VPN clients commonly do it. The customer's own
# host candidate was a VPN-shaped public address.
#
# With TURN the browser talks only to 3478 — the port it demonstrably reaches —
# and gets its OWN publicly reachable relay candidate, which we can then reach.
# This is also simply the standard configuration: both peers normally get the
# same ICE server list. Empty = STUN only, or nothing.
RTC_CLIENT_TURN_URL: str = os.getenv("RTC_CLIENT_TURN_URL", "").strip()

# TIME-LIMITED TURN CREDENTIALS (coturn --use-auth-secret, the TURN REST API
# scheme). The browser cannot allocate a relay without a credential, so one has
# to reach the client — but a STATIC password handed to every page load is a
# permanent key to our relay. These expire instead: username is the expiry
# timestamp, password is its HMAC under a secret that never leaves the server,
# and coturn validates both without storing anything.
#
# Unset = fall back to the static RTC_TURN_USERNAME/CREDENTIAL pair, so an
# environment configured the old way keeps working.
RTC_TURN_SECRET: str = os.getenv("RTC_TURN_SECRET", "").strip()
RTC_TURN_TTL_SECONDS: int = _get_int("RTC_TURN_TTL_SECONDS", 3600)


def turn_credentials() -> tuple[str, str]:
    """A (username, credential) pair for the relay, valid for TTL seconds.

    Both sides use this — our own aiortc transport and the browser — because
    coturn accepts exactly one auth mechanism at a time. Returns the static
    pair unchanged when no secret is configured.
    """
    if not RTC_TURN_SECRET:
        return RTC_TURN_USERNAME, RTC_TURN_CREDENTIAL
    username = str(int(time.time()) + RTC_TURN_TTL_SECONDS)
    digest = hmac.new(
        RTC_TURN_SECRET.encode(), username.encode(), hashlib.sha1
    ).digest()
    return username, base64.b64encode(digest).decode()


def client_ice_servers() -> list[dict[str, str]]:
    """The ICE servers handed to the BROWSER in `call_started`.

    Server-side config rather than a hardcoded frontend constant so the relay
    can move without a frontend rebuild, and so localhost gets an empty list
    instead of dialling a host that is not there.

    ⚠️ THE TURN ENTRY SHIPS A CREDENTIAL TO THE CLIENT. That is unavoidable —
    a browser cannot allocate a relay without one — but it means anyone who
    opens the page can use our relay. Tolerable on a dev VM whose password is
    throwaway and whose peers are already restricted to public addresses
    (--denied-peer-ip). BEFORE PRODUCTION this must become coturn's
    time-limited shared-secret scheme (`--use-auth-secret`), where the server
    mints an expiring username/password per call and a leaked one dies on its
    own. Tracked with the other pre-production credential work.
    """
    servers: list[dict[str, str]] = []
    if RTC_CLIENT_STUN_URL:
        servers.append({"urls": RTC_CLIENT_STUN_URL})
    if RTC_CLIENT_TURN_URL:
        username, credential = turn_credentials()
        if username and credential:
            servers.append({
                "urls": RTC_CLIENT_TURN_URL,
                "username": username,
                "credential": credential,
            })
    return servers

# The Silero speech gate (2026-07-30): only detected human speech is forwarded
# to the vendor — echo residue, breaths and room noise are dropped server-side
# before the vendor's VAD can misfire on them (and before they bill input
# tokens). Local ONNX inference, no network. Fails open: a missing model or a
# dead onnxruntime forwards everything, which is the pre-gate behaviour.
CALL_SPEECH_GATE: bool = _get_bool("CALL_SPEECH_GATE", True)

# Gate sensitivity (2026-08-03, tuned after background noise kept becoming
# "turns" — and, hallucinated by the transcriber into Arabic script, kept
# flipping the language pin). Silero's 0.5 default is the documented
# too-sensitive setting (breaths, AC, room chatter); Pipecat ships 0.7 and
# the noisy-room guidance says ~0.8. Env-tunable so the sweet spot for a real
# room/phone is a .env edit, never a code change.
CALL_GATE_START_PROB: float = _get_float("CALL_GATE_START_PROB", 0.7)
CALL_GATE_END_PROB: float = _get_float("CALL_GATE_END_PROB", 0.5)
# Sustained speech required before the gate OPENS (the min-speech-duration
# rule every production stack applies): a door slam or cough scores high for
# a block or two, a spoken word holds for longer. 0 restores open-on-first-
# block. The confirmed onset is forwarded in full, so no first syllable is
# lost to the wait.
CALL_GATE_MIN_SPEECH_MS: int = _get_int("CALL_GATE_MIN_SPEECH_MS", 200)
# The vendor VAD's activation threshold (their docs: raise it in noisy
# environments; default 0.5, ours was 0.6). Second line of defence — it only
# ever hears what the gate forwards.
CALL_VAD_THRESHOLD: float = _get_float("CALL_VAD_THRESHOLD", 0.75)

# The call PERSONA switch (2026-07-29, developer A/B request: "the call feels
# scripted"). ADDITIVE by construction — anything but "natural" runs the
# shipped persona and voice byte-for-byte, so flipping back is zero-risk.
#   "classic" — realtime_instructions() + REALTIME_VOICE (the behaviour that
#               existed before this switch).
#   "natural" — the voice-native persona (app/agent/call_persona.py) + the
#               warmer CALL_NATURAL_VOICE below. Same brain, same tools, same
#               guards — only the delivery brief and the voice differ.
CALL_PERSONA: str = os.getenv("CALL_PERSONA", "classic").strip().lower()
CALL_NATURAL_VOICE: str = os.getenv("CALL_NATURAL_VOICE", "cedar")

# The relay's transcription model. gpt-4o-MINI-transcribe by default — the
# `completed` transcript lands noticeably sooner after speech stops than the
# full model's, and on short booking utterances (with the catalogue biasing
# prompt doing the heavy lifting for the names that matter) the accuracy gap
# was not worth seconds of every single turn. Voice NOTES keep STT_MODEL:
# they are offline artifacts where accuracy beats latency.
CALL_STT_MODEL: str = os.getenv("CALL_STT_MODEL", "gpt-4o-mini-transcribe")

# Pin the CUSTOMER-side transcription LANGUAGE for the realtime call. Left to
# auto-detect, the transcriber mangles short/unclear English utterances into
# random scripts (Malayalam, Lithuanian — observed live), which the call now
# shows in the after-call thread. A language hint is the CORRECT fix and NOT a
# prompt (see the transcription note in realtime_agent): it tells the model what
# to expect without biasing the words. "en" by default for this English-first
# demo; set "ar" for an Arabic demo, or "" to restore auto-detect.
CALL_STT_LANGUAGE: str = os.getenv("CALL_STT_LANGUAGE", "en").strip()

# ---------------------------------------------------------------------------
# WhatsApp Cloud API (Meta Graph API) — real outbound messaging (2026-07-22)
# ---------------------------------------------------------------------------
# Sends the booking confirmation and the T-2h reminder over real WhatsApp via
# Meta's Cloud API. Server-side only. UNSET/disabled, the notification dispatcher
# falls back to SIMULATOR behaviour (rows still transition to `sent` with rendered
# body, the console feed works, nothing leaves the box) — so the text demo and
# the tests run unchanged with no token.
#
# ⚠️ D1 PDPL CHECKLIST: this sends customer data (name, mobile, booking ref) to a
# Meta US/global endpoint — the second path after voice that leaves the region.
# Sandbox / fictitious-data demo only; note it in the checklist.
WHATSAPP_ENABLED: bool = _get_bool("WHATSAPP_ENABLED", False)
WHATSAPP_ACCESS_TOKEN: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v21.0")

# Notification dispatcher (AD-F): the in-process APScheduler that reads
# Notification.send_at and sends due rows (confirmation = now → next tick;
# T-2h reminder = slot.start - 2h). OFF by default so unit tests and plain dev
# never auto-dispatch; the demo turns it on in .env. Real wall-clock time.
NOTIFY_DISPATCH_ENABLED: bool = _get_bool("NOTIFY_DISPATCH_ENABLED", False)
NOTIFY_POLL_SECONDS: int = _get_int("NOTIFY_POLL_SECONDS", 15)

# Which notification KINDS the dispatcher actually sends right now. Rows are
# still SCHEDULED as before (FR-CF: confirmation + t24 + t2); the day-before
# `t24` reminder is simply not SENT — product decision 2026-07-22 asked for the
# confirmation and the T-2h reminder only. A due row of an excluded kind is
# retired (not left to loop). Add "t24" here to re-enable the day-before
# reminder; nothing else changes.
#
# ⚠️ `noshow` (2026-07-23) and `feedback` (2026-07-26) are in the DEFAULT, but a
# deployment that sets NOTIFY_SEND_KINDS explicitly in its .env overrides the
# whole list — so those notices would be written, come due, and then be silently
# retired as "kind_not_enabled". Add them there too when deploying this. This has
# bitten once already; the failure is invisible except in the audit trail.
NOTIFY_SEND_KINDS: frozenset[str] = frozenset(
    k for k in (
        os.getenv("NOTIFY_SEND_KINDS")
        or "confirmation,t2,reschedule,cancellation,noshow,feedback,brochure"
    ).replace(" ", "").split(",")
    if k
)
# Bounded retry per row: a closed 24h window / transient API error is retried on
# the next tick; after this many attempts the row is retired so a permanent
# failure (bad number) cannot loop forever. No `failed` status is added — the
# reason is recorded in the row's payload and audited.
NOTIFY_MAX_ATTEMPTS: int = _get_int("NOTIFY_MAX_ATTEMPTS", 5)

# Relax the +971-only mobile rule so an international test number (e.g. a
# Pakistani +92) can be identified and receive the sandbox WhatsApp. When True
# (default), customers.normalise_mobile enforces UAE mobile prefixes as before;
# when False, a number written WITH a country code (+.. or 00..) is accepted as
# E.164, while a bare local number still falls back to the UAE rules.
STRICT_UAE_MOBILE: bool = _get_bool("STRICT_UAE_MOBILE", True)

# ---------------------------------------------------------------------------
# WhatsApp INBOUND — reply-with-a-link handoff to web (2026-07-22)
# ---------------------------------------------------------------------------
# The first time this system has an INBOUND channel. A customer WhatsApps the
# business number ("change my booking"); we identify them by the sending number,
# mint a single-use handoff token, and reply with a deep link into the web chat.
# The real conversation still happens on the web (SSE, AD-B) — WhatsApp inbound
# runs BEFORE/OUTSIDE the agent and only hands off, exactly as STT runs before
# the agent for voice notes. AD-C (no WebSockets for chat) is untouched.
#
# OFF by default: the webhook route is registered unconditionally, but with this
# false it accepts nothing and mints nothing, so plain dev and tests never open
# an inbound surface. The demo turns it on in .env alongside a public tunnel.
WHATSAPP_INBOUND_ENABLED: bool = _get_bool("WHATSAPP_INBOUND_ENABLED", False)

# The token Meta echoes back during webhook verification (GET /webhook). We set
# it in the Meta dashboard AND here; the handshake only succeeds when they match.
# Server-side only. Empty until set — verification then always fails, which is
# the safe default for an unconfigured inbound channel.
WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

# The Meta App Secret (App Settings > Basic). Used to verify the
# X-Hub-Signature-256 HMAC on every inbound POST — without it, anyone who learns
# the webhook URL could POST a forged "customer message" and be handed a link
# bound to a real customer. When set, an unsigned/mis-signed body is rejected;
# when EMPTY, signature verification is SKIPPED (dev only — log-flagged in the
# route), so the webhook can be exercised locally without the secret.
WHATSAPP_APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")

# Where the web chat lives, used to build the handoff link the WhatsApp reply
# carries: f"{PUBLIC_WEB_BASE_URL}/?h=<token>". In the demo this is the public
# HTTPS tunnel (cloudflared); locally it is the Vite dev origin. No trailing
# slash — the code appends "/?h=".
PUBLIC_WEB_BASE_URL: str = os.getenv("PUBLIC_WEB_BASE_URL", "http://localhost:5173").rstrip("/")

# How long a minted handoff token is valid, in minutes. Short by design: the
# link is identity-bearing (redeeming it binds a fresh web session to the
# customer), so a forwarded or leaked link must go stale quickly. The token is
# ALSO single-use — redeeming marks it spent — so this is the ceiling, not the
# expected lifetime.
HANDOFF_TOKEN_TTL_MINUTES: int = _get_int("HANDOFF_TOKEN_TTL_MINUTES", 30)

# ---------------------------------------------------------------------------
# Post-drive feedback survey (2026-07-26)
# ---------------------------------------------------------------------------
# Opened when an operator marks a booking `completed` in the console, conducted
# over WhatsApp as a deterministic state machine (services/feedback) — no model
# is involved at any point, so AD-C and the agent's rails are untouched.
#
# ON by default so marking Completed does the thing the console implies it does.
# Turn it off to demo attendance without messaging anyone.
FEEDBACK_ENABLED: bool = _get_bool("FEEDBACK_ENABLED", True)

# How long a survey sits idle before it is retired. Measured from the LAST
# activity (FeedbackSurvey.updated_at), not from when the opening was sent, so
# someone answering slowly is never timed out mid-question. Partial answers are
# always kept — a customer who rated the car a 2 and stopped has told us the
# most useful thing in the survey.
FEEDBACK_EXPIRY_HOURS: int = _get_int("FEEDBACK_EXPIRY_HOURS", 24)

# How long "later" defers the opening by. Honoured exactly ONCE per survey; a
# second "later" is read as a polite no and closes it. Asking a third time is
# nagging, which rule 14 forbids as plainly as urgency tactics.
FEEDBACK_LATER_HOURS: int = _get_int("FEEDBACK_LATER_HOURS", 3)

# LangGraph checkpointer store (AD-H). Its OWN sqlite file, separate from the
# business DB — persists per-session block history (thread_id = session_id) so a
# second turn resumes with tool_use/tool_result intact. WAL-mode, one connection
# per turn (see agent.new_sqlite_checkpointer) for concurrent QR-attendee phones.
AGENT_CHECKPOINT_PATH: str = os.getenv("AGENT_CHECKPOINT_PATH", "./agent_checkpoints.db")

# ---------------------------------------------------------------------------
# LangSmith tracing (observability)
# ---------------------------------------------------------------------------
# Read here, like every other env var, so exactly one file knows the
# environment. The langsmith SDK reads LANGSMITH_API_KEY / _PROJECT / _ENDPOINT
# from the environment ITSELF — those are deliberately NOT mirrored here, since
# nothing in our code passes them anywhere. This flag exists only so the agent
# knows whether to wrap the Anthropic client (see agent/graph._build_client).
#
# OFF by default and set in the ROOT .env (which deploy.sh ships to the VM),
# never in elite-group-backend/.env — that is the file pytest loads, and the
# suite is offline BY CONSTRUCTION. Traces of scripted test runs would be both a
# live vendor call and noise in the project.
#
# ⚠️ D1 PDPL CHECKLIST: a trace carries the WHOLE conversation — name, mobile,
# doorstep address, booking ref — to a US endpoint, and rule 12's erasure CANNOT
# reach it (services/erasure.py clears our DB and the checkpoint; a third-party
# trace store is beyond it). Heavier than the STT/TTS/WhatsApp entries, which
# each send one utterance or one message. LANGSMITH_HIDE_INPUTS /
# LANGSMITH_HIDE_OUTPUTS (read by the SDK, no code) keep the shape and drop the
# text.
LANGSMITH_TRACING: bool = _get_bool("LANGSMITH_TRACING", False)

# Attach the CALL AUDIO to each voice-call LangSmith run (2026-08-04) — the
# caller's utterance and Aisha's reply as WAV attachments, so LangSmith's
# audio-native voice evaluators (Audio Quality, Vocal Affect, Transcription
# Accuracy, User Interrupts) have something to judge. Text tracing (the
# transcript) needs nothing here; this is ONLY for the audio evaluators.
#
# OFF by default and gated a SECOND time by LANGSMITH_TRACING — audio is
# captured and uploaded only when BOTH are on, so a normal deployment never
# touches call audio even with tracing enabled. Intended for the isolated
# demo sandbox, flipped on in its ROOT .env, never in prod.
#
# ⚠️ D1 PDPL — HEAVIER THAN THE TEXT TRACE ABOVE: this REVERSES the call's
# no-stored-audio property. Both voices are buffered in memory mid-call and
# shipped to the LangSmith US endpoint as files, beyond rule 12's erasure
# reach — the single largest privacy surface the call has. Acceptable ONLY on
# the sandbox with test data; a prod turn-on is a deliberate sign-off, not a
# default. Nothing is written to our own DB (memory only, dropped at hangup).
CALL_TRACE_AUDIO: bool = _get_bool("CALL_TRACE_AUDIO", False)
