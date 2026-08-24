# CLAUDE.md — Elite Test Drive Booking AI Agent (Chat Demo)

Shared context for both repos. This file is the single source of truth for scope,
binding behaviour rules, architecture decisions, and the API contract between
backend and frontend. Derived from BRD v1.6 (requirements source of truth),
HLD v1.0, SOW v1.6.

## Repository layout

```
Elite-group/
  CLAUDE.md                 # this file — shared by both repos (read from parent dir)
  elite-group-backend/      # FastAPI + SQLAlchemy + Claude agent
  elite-group-frontend/     # Vite + React + TypeScript chat widget
  docker-compose.yml        # local orchestration (backend, frontend, postgres)
```

## Project

DigyCorp is building a demo for Elite Group Holding (Dubai): a bilingual (English/
Gulf Arabic) AI chat agent that books, reschedules, and cancels test drives
against simulated business systems. Delivery deadline: **30 July 2026**. This is a
live, interactive client demo (attendees use it hands-on via QR on their own
phones), not a video.

**Brands: the demo ships Jetour only.** Elite Group represents SOUEAST and The
Elite Cars in reality, and the architecture is multi-brand throughout (every
vehicle and showroom carries a `brand`), but no SOUEAST or Elite Cars row exists
in `seed/*.json`. Treat those two as production roadmap, never as demo data —
see the seed section for why this distinction earned its own paragraph.

## Scope (chat-only cut, agreed 16 Jul 2026)

**In:** web chat agent (text only — no mic, no voice channel) · all five journeys
(book, slot selection, confirmations/reminders, reschedule, cancel) · needs-based
vehicle discovery · doorstep test-drive option · visit personalisation · bilingual
EN/AR with per-turn detection, code-switching, RTL · WhatsApp-style confirmations
and reminders (simulator, accelerated clock) · consent-based customer memory +
live "forget me" · analytics/audit console with agent-registry framing · minimal
escalation stub (agent offers a human callback and logs it — no supervisor UI).

**Out (do not build):** voice channel (~~in-app voice call~~ — added 2026-07-27,
see the voice-call override below; telephony stays out), ~~STT~~/~~TTS~~,
~~mic input~~/modality mirroring (exists only INSIDE a live call — a voice note
still gets a text reply),
WhatsApp voice notes, Service Agent cameo, omnichannel chat→voice switch,
supervisor handoff screen, real telephony/WhatsApp Business API, payments/finance,
languages beyond EN/AR (**and the CALL is English-only**, decision 2026-07-27),
production hardening.

> ⚠️ **VOICE NOTES (input only) ADDED by product decision 2026-07-21.** The
> customer may record a voice note, WhatsApp-style; it is transcribed and enters
> the conversation as an ordinary customer turn. **Aisha still replies in TEXT
> ONLY, in the language that was spoken.**
>
> **In, as of this decision:** mic capture in the chat composer · speech-to-text
> (EN + AR, auto-detected) · the audio blob stored and replayable by the customer
> · the transcript shown beneath the player.
>
> **Still OUT, and not softened by this:** TTS and any spoken reply · a voice
> *channel* (telephony, WhatsApp voice notes) · modality mirroring (Aisha does
> not answer in the modality she was addressed in — she always answers in text) ·
> the omnichannel chat→voice switch. AD-C is untouched: **this introduces no
> WebSockets.** Transcription is a pre-processor in front of the existing
> `/api/chat`, not a second conversational channel.
>
> **Why it does not disturb the architecture:** STT runs BEFORE the agent. The
> transcript is posted to `/api/chat` as `message`, exactly as typed text is, so
> the prompt, the LangGraph loop, every tool, the SSE contract and the audit
> choke point are all unchanged. Per-turn language detection needs no change
> either — `_detect_lang` reads Arabic script, and the transcript comes back in
> Arabic script (verified 2026-07-21, see below).
>
> **Vendor: OpenAI `gpt-4o-transcribe`** — Anthropic has no audio API, so a
> second vendor is unavoidable. `AZURE_FOUNDRY_RESOURCE` was evaluated first and
> rejected: it is in **East US** (so no residency gain) and has no transcription
> deployment.
>
> **⚠️ FLAGGED FOR THE D1 PDPL CHECKLIST — two entries, neither resolved:**
> (a) customer **audio leaves the UAE** to a US endpoint, which is new — no other
> part of this system sends customer data outside the region; (b) voice
> recordings are personal data and arguably biometric-adjacent, so "forget me"
> (rule 12) MUST hard-delete the blobs, and blobs expire on a 48h retention.
>
> **Accuracy findings (V0 spike, 2026-07-21) — the vocabulary prompt is load-
> bearing, not a nicety.** Unbiased, `gpt-4o-transcribe` returned "**Jetter** T2 …
> in **Dera**" for English audio — i.e. it corrupted the brand and the showroom,
> the two entities a booking actually depends on, and `search_vehicles(name=
> "Jetter T2")` finds nothing. Passing the catalogue as the `prompt` parameter
> fixed that plus two Arabic verb corruptions. So the vocabulary prompt is
> **composed from the live `VehicleModel` and `Showroom` rows at call time**,
> never hardcoded — same reason no display text is stored in data fields: a
> hand-typed list drifts from the catalogue it is supposed to describe.
>
> **KNOWN GAP, deliberately accepted (2026-07-21): Gulf dialect accuracy is
> UNTESTED.** The V0 spike used macOS `say -v Majed`, a synthetic MSA voice — it
> proves the output is Arabic *script* and that the iOS `audio/mp4` container is
> accepted, and it proves NOTHING about real Emirati speech. No Gulf speaker was
> available. Do not cite the spike as dialect validation; it is not.
>
> **⚠️ DEMO-DAY BLOCKER, unresolved: `getUserMedia` requires HTTPS.** It is
> blocked on plain HTTP everywhere except `localhost`. Local dev is therefore
> fine, but **QR-code attendees on `http://<ip>:5173` will find the mic button
> dead on every phone** — no code change fixes this. Cheapest fix is a tunnel
> (`cloudflared tunnel --url http://localhost:5173`). Deferred by the developer
> 2026-07-21 with HTTP accepted "for now"; it must be closed before demo day.

> ⚠️ **READ-ALOUD (opt-in TTS) ADDED by product decision 2026-07-21.** A speaker
> icon on each of Aisha's text bubbles; tapping it reads that message aloud in
> the language the message is written in. Tapping another stops the first.
>
> **This is PLAYBACK, NOT A SPOKEN AGENT.** Aisha still WRITES her replies —
> nothing is generated as audio, nothing is spoken unless a customer asks for it,
> and no reply is ever automatic. Modality mirroring stays out: she does not
> answer in the modality she was addressed in, and a voice note still gets a text
> reply. A voice *channel* (telephony, WhatsApp voice notes) and the omnichannel
> chat→voice switch stay out. AD-C is still untouched — **no WebSockets.**
>
> **SERVER-GENERATED (OpenAI `gpt-4o-mini-tts`), WITH THE ON-DEVICE ENGINE AS A
> FALLBACK.** It shipped on-device first and was changed on 2026-07-21 after
> listening: the browser's voices are the DEVICE'S, and on the demo hardware the
> English ones were 1990s novelty voices (Boing, Zarvox, Fred — macOS ships ~180,
> of which two are real) while Arabic had exactly one. Server synthesis is the
> only way to get the same warm Gulf register on every attendee's phone.
> `gpt-4o-mini-tts` over `tts-1` because it takes a plain-English TONE
> INSTRUCTION — the one place rule 14's register is expressed to a model that is
> not Claude — and it was audibly warmer in BOTH languages at the same price
> (~$0.015/min; a 60-second reply costs about 1.4 cents, and the cache means a
> replay costs nothing).
>
> **THE BROWSER ENGINE IS DEMOTED, NOT DELETED, and that is deliberate.** A 404,
> a 503, an unset key or a flat conference network falls back to
> `speechSynthesis` instead of a dead control. A vendor outage must cost QUALITY,
> never the feature — an icon that stops working mid-demo is worse than one that
> sounds worse. `useSpeech.pickVoice` therefore still matters: it ranks by name
> and REFUSES the joke voices, which is what made the original sound broken.
>
> **⚠️ ITS PDPL EXPOSURE IS HEAVIER THAN THE STT PATH'S AND MUST NOT BE FILED
> WITH IT — a THIRD D1 entry, not a footnote to the second.** Transcription sent
> the customer's own audio. This sends AISHA'S REPLIES, which quote the customer
> back to themselves: "Perfect, Mr Sharma, confirmed for Saturday at 7pm, we'll
> text +971 50 555 0103" is a name, a mobile and a booking reference leaving the
> UAE. Mitigated by SESSION-SCOPED clips (a global cache would put a phone number
> in a row belonging to nobody, which makes retention and rule 12 meaningless for
> it), the 48h expiry, and hard deletion on "forget me" — never by pretending it
> is the same exposure as the last one.
>
> **THE CLIENT NEVER SENDS AISHA'S WORDS BACK.** `GET /api/speech/{turn_id}` —
> the client names a TURN and the server reads the transcript itself. So
> everything spoken in her voice is provably something she said (rule 2 extended
> to audio, which sounds more authoritative than text and so deserves it more),
> and the endpoint cannot be used as a free text-to-speech proxy billed to us.
>
> **KNOWN LIMIT — TWO VOICES FOR ONE PERSONA.** `coral` (en) and `shimmer` (ar)
> were each chosen by ear, so **Aisha's voice CHANGES when a customer switches
> language mid-conversation** — which is S-19, a scenario the demo runs on
> purpose. Each language gets its best voice at the cost of sounding like two
> people to a bilingual listener. Set `TTS_VOICE_EN` and `TTS_VOICE_AR` to the
> same value to trade the other way; nothing else changes.
>
> **When neither engine can speak a message's language the icon is NOT RENDERED**
> on it. An absent control is honest; one that does nothing reads as broken, and
> one that reads Arabic script with an English voice produces confident nonsense
> in front of an Arabic-speaking client.
>
> **Code-switching is mangled, accepted.** One voice per message, chosen from the
> turn's stored `lang`, so "الـ Jetour T2" inside an Arabic sentence is read by
> the Arabic voice. Splitting a string by script and alternating voices queues
> badly in browser TTS and sounds worse, not better.
>
> **Spoken text is NORMALISED first** (`src/speech-text.ts`). `TD-2026-0142` read
> literally is gibberish, and it appears in the booking confirmation — the climax
> of the demo. References, +971 numbers and clock times are rewritten for speech
> only; **the bubble's text is never altered**, so what is read and what is on
> screen stay the same message.

> ⚠️ **POST-DRIVE FEEDBACK SURVEY ADDED by product decision 2026-07-26.** When an
> operator marks a booking **`completed`** in the console, the customer gets a
> WhatsApp asking how the drive went: an opening + re-consent, then **four 1–5
> questions answered by TAPPING**, then an open comment, then a thank-you. Scores
> and comment are stored and shown on the Bookings tab.
>
> **THIS IS THE ONE PLACE WHATSAPP BECAME A CONVERSATIONAL CHANNEL, and it
> reverses what the inbound section below used to claim.** Until now inbound
> WhatsApp only ever handed off to the web — mint a token, reply with a link — so
> nothing on that channel had a "where were we". A survey does: a bare "5" is
> meaningless without knowing which question is outstanding.
>
> **IT IS STILL NOT A SECOND AISHA, and that is what makes it safe.** No model is
> involved at any point: the questions are fixed strings, the answers arrive as
> list-reply ids we minted ourselves, and the step counter is advanced by ordinary
> Python in `services/feedback.py`. So **AD-C is untouched** (no WebSockets, no
> streaming, no agent loop), rule 2 holds trivially (nothing here generates
> language, so nothing here can invent a fact), and **the web agent cannot see,
> mention, or request any of it** — there is deliberately no tool and no prompt
> change. A satisfaction score the model could READ is a score it could
> VOLUNTEER, which is exactly the behaviour rule 12 restrains for memory.
>
> **Q5 (recommendation / NPS) WAS DROPPED** before this shipped, so the copy says
> "four quick taps", not five. Q3 inserts the vehicle name; **Q4 branches on
> `booking.location`** ("our team who welcomed you" vs "our specialist who brought
> the car to you") — asking a doorstep customer about the team who welcomed them
> is plainly wrong, and both variants are phrased about a ROLE, never a named
> individual, so an answer can never read as a complaint about one identifiable
> person.
>
> **TAPPABLE 1–5 IS A LIST MESSAGE, NOT BUTTONS — a platform limit, not a
> preference.** Meta caps interactive REPLY BUTTONS at three, so a five-point
> scale cannot be buttons at all; a list message allows ten rows. `whatsapp.
> send_interactive_list` is the new sender. **The reply id carries BOTH the
> question and the score** (`"q3:4"`), which is what makes a late tap harmless: a
> customer scrolling back to an older question and tapping it is filed against
> the question they were looking at, never against whichever one is current.
>
> **THE OPENING RIDES THE ORDINARY NOTIFICATION RAILS** (`NotificationKind.
> feedback`, `send_at = now`), and that is the whole reason it does: it inherits
> the **consent gate checked at SEND time** (so consent withdrawn between the
> drive and the survey still stops the message), the bounded retry for a closed
> 24h window, simulator mode when WhatsApp is unconfigured, and the notification
> audit trail. Q1–Q4 and the close are sent directly — they are replies inside a
> live conversation, not scheduled notices.
>
> **A REAL REQUEST ALWAYS BEATS A SURVEY.** A mid-survey message that parses as a
> booking intent ("cancel my booking") **abandons** the survey and falls through
> to the normal handoff. Trapping someone in a satisfaction questionnaire while
> they are trying to cancel is the single worst thing this feature could do.
> `abandoned` is its own status precisely so that never reads as a refusal to
> answer. A tapped rating is exempt from the keyword check — its text is a row
> title we wrote, so matching it could only ever be a false positive.
>
> **ONE OPEN SURVEY PER CUSTOMER.** A second completed booking is `queued` and
> released only when the first closes. Without it a bare "5" from someone with two
> live surveys is unattributable, and guessing files real feedback against the
> wrong drive. **`queued` and `pending` are DISTINCT STATUSES and must stay so** —
> collapsing them makes the promotion check see each queued survey as the thing
> blocking its own release, and nothing ever comes off the queue (found in test).
>
> **PARTIAL ANSWERS ARE ALWAYS KEPT** — on skip, on expiry, on abandonment.
> Someone who rated the car a 2 and stopped has told us the most useful thing in
> the survey. Unanswered scores stay **NULL and render as an em-dash**: a zero is a
> score nobody gave and would drag every average down with it (rule 16).
> `"later"` defers once (`FEEDBACK_LATER_HOURS`); a second is read as a polite no.
> `"skip"` closes it and **does NOT touch `whatsapp_consent`** — declining one
> survey must never cost a customer their booking reminders. Idle surveys expire
> after `FEEDBACK_EXPIRY_HOURS`, measured from the LAST activity, with **no nudge**
> (chasing a satisfaction score is the pressure rule 14 rules out).
>
> **AN UNDELIVERABLE OPENING MUST CLOSE ITS SURVEY** (`feedback.abort_opening`).
> A survey left at `pending` blocks promotion, so one retired opening — no
> consent, kind disabled, five failed sends — would silently stop that customer
> ever being surveyed again, with nothing on screen explaining why.
>
> **VOICE NOTES ARE OUT OF SCOPE for the open comment** (product decision), so the
> copy invites typed text and there is no inbound-media download path. **No emoji**
> either, per rule 17 — the source screenshot has them; every other WhatsApp
> message in this system does not, and consistency won.
>
> **The screenshot's "yesterday" is DERIVED, not hardcoded.** The survey is sent
> minutes after the drive is marked complete, so the word comes from `slot.start`
> ("today" / "yesterday" / "on Saturday"). Saying "yesterday" about something that
> happened at lunchtime is a false claim about a fact we hold.
>
> **⚠️ A FIFTH D1 PDPL ENTRY.** The survey stores the customer's own prose about a
> visit, which can identify them AND a member of staff, and it messages them
> outside any conversation they initiated. Rule 12 erasure **deletes the survey
> rows and counts them in `would_delete`** (`services/erasure.py`) — the scores
> alone are arguably anonymous, but "the salesman in Deira was rude" is not.
> Feedback surveys are deleted **BEFORE bookings** in the sweep: `FeedbackSurvey.
> booking_ref` is a real FK to `bookings.ref`, unlike `Notification.ref`.
>
> **The Arabic copy is written natively in the same register as the existing
> notification copy, and is PENDING LAYLA'S REVIEW.** Flagged in the module; do not
> record it as signed off.
>
> **Config:** `FEEDBACK_ENABLED` (default on), `FEEDBACK_EXPIRY_HOURS` (24),
> `FEEDBACK_LATER_HOURS` (3). **⚠️ `feedback` IS IN THE DEFAULT
> `NOTIFY_SEND_KINDS`, BUT ANY .env THAT SETS THAT VAR EXPLICITLY OVERRIDES THE
> WHOLE LIST** — the opening would then be written, come due, and be silently
> retired as `kind_not_enabled`. This has already bitten `noshow` once. Check the
> deployed VM's root `.env` before demo day; the only symptom is in the audit trail.

> ⚠️ **VOICE CALL ADDED by product decision 2026-07-27** (built on branch
> `voice-call` in both repos; **NOT deployed, by explicit instruction the same
> day**). A ChatGPT-style call: a phone button in the composer, the customer
> talks hands-free, Aisha answers OUT LOUD — and every exchange lands in the
> chat thread as ordinary turns (player + transcript for the customer, her
> normal bubble with cards and receipts for the reply).
>
> **THE SAME AGENT, WITH EARS AND A MOUTH — never a second Aisha.** The call is
> a WS gateway (`/api/call/{session_id}`) around the SAME turn the SSE route
> runs: streaming STT in front, sentence-streamed TTS behind, and in the middle
> `run_turn()` (`app/agent/runner.py`, extracted from the chat route so the two
> channels share one implementation and cannot drift). Same graph, same tools,
> same write guards, same checkpoint, same audit choke point. OpenAI's
> speech-to-speech Realtime agent was evaluated and REJECTED for exactly this:
> it is its own model improvising as Aisha, with no tools and no grounding —
> sub-second latency at the price of everything rule 2 protects.
>
> **AD-C IS AMENDED, NOT VIOLATED (AD-L).** AD-C deferred WebSockets "to the
> voice phase" because only bidirectional audio justifies them. This is that
> phase. WS exists for THIS CHANNEL ONLY; `/api/chat` stays SSE, untouched.
>
> **ENGLISH-ONLY FOR NOW (product decision 2026-07-27).** Nothing FORCES
> English — rule 3's per-turn detection stays, so an Arabic utterance still
> gets an Arabic reply in the ar voice — but Arabic on the call is UNTUNED AND
> UNTESTED and must not be claimed: the chunker is English-tuned, the Gulf
> dialect gap from the voice-note spike applies doubly to live speech, and the
> Arabic call-chrome strings ship unused pending Layla's review.
>
> **TURN-TAKING: server VAD first, client fallback always.** The gateway
> relays mic audio to OpenAI's Realtime TRANSCRIPTION session (transcription
> only — the agent stays ours): its server VAD ends the turn ~0.9 s after
> silence with the transcript already in flight. The client's own RMS detector
> remains armed as the fallback — a `vad` event says who owns turn-taking, and
> a relay death mid-call hands it back to the client and the call continues in
> batch mode (upload-per-utterance). A vendor outage costs LATENCY, never the
> call. ⚠️ **THE VENDOR HANDSHAKE IS UNVERIFIED OFFLINE** — the offline suite
> scripts the relay's normalised events (no live calls, per the standing rule),
> so one live smoke test on a dev box is REQUIRED before the streaming path is
> believed (`services/realtime_stt.py` carries the same warning).
>
> **AISHA SPEAKS WHILE SHE WRITES.** A sentence chunker watches the token
> stream and each completed sentence is synthesised and shipped as its own
> audio segment while later sentences are still generating — first words land
> ~2.5–4 s after the customer stops. Call TTS is UNCACHED AND UNSTORED
> (`synthesize_for_call`): a call reply is spoken once at generation time, so
> nothing of her audio persists anywhere; replaying it later IS read-aloud,
> which has its own cached path and works on call turns because they are
> ordinary turns.
>
> **BARGE-IN: the customer can talk over her.** Detection is CLIENT-side (it
> owns playback): sustained loud speech kills the audio within a frame and
> tells the server, which abandons the reply's remaining speech — and **THE
> TEXT IS NEVER TAKEN BACK**: the turn persists in full (the `text_break` rule
> extended to audio; what she wrote stays written whether or not it was heard
> out loud). The interrupting utterance is captured while the old turn drains
> and starts the next turn immediately. ⚠️ The barge thresholds lean on the
> device's echo cancellation and are FLAGGED FOR TUNING ON REAL HARDWARE —
> too lax and Aisha interrupts herself.
>
> **PERSISTENCE RIDES THE VOICE-NOTE RAILS.** Each utterance is stored as a
> `VoiceNote` (WAV of the buffered PCM, transcript attached — no second vendor
> round-trip in relay mode), so the session gate, 48 h expiry, rule-12 erasure,
> customer backfill and thread replay are all inherited rather than rebuilt.
> Call start/end are audited (`actor=customer` for the button and the hangup;
> `system` for the watchdogs). Two watchdogs: `CALL_SILENCE_HANGUP_SECONDS` of
> nothing (the call channel's idle ladder) and `CALL_MAX_MINUTES` (the cost
> ceiling, ~$0.02–0.03/min of vendor audio) — both hang up gracefully with a
> stated reason, as does every refusal (flag off, no key, closed session): an
> absent call must say why, the mic-button rule.
>
> **⚠️ A SIXTH D1 PDPL ENTRY, the heaviest of the audio three.** A call streams
> the customer's CONTINUOUS live microphone to a US endpoint for its whole
> duration — not one bounded utterance — and Aisha's replies (quoting names,
> mobiles, booking refs) transit the same vendor as TTS input, sentence by
> sentence. Mitigations: `CALL_ENABLED` is an EXPLICIT opt-in (default off —
> unlike STT/TTS it must never activate as a side effect of a key existing),
> per-utterance storage under the existing 48 h + erasure regime, none of her
> audio stored, and the minutes cap. The live stream, once transited, cannot
> be recalled — the same honest caveat as delivered WhatsApp messages.
>
> **Still OUT, and not softened by this:** telephony and WhatsApp voice notes
> as a channel · TTS on the TEXT chat beyond opt-in read-aloud (a typed
> message still gets a written reply; a voice NOTE still gets a text reply —
> modality mirroring exists only INSIDE a live call) · languages beyond EN on
> the call, per the decision above.
>
> **HTTPS applies twice over:** `getUserMedia` AND `wss://` both need a secure
> origin, so the voice-note demo-day blocker covers the call unchanged. The
> nginx config now upgrades WS on `/api/call/` and the Vite dev proxy has
> `ws: true`; local dev on localhost works out of the box.

## Stack

- **Backend repo:** Python 3.11+, FastAPI (async), SQLAlchemy 2, SQLite for dev /
  `DATABASE_URL` env swap to Postgres, Anthropic Claude API (`claude-sonnet-4-6`)
  with tool use, APScheduler for the accelerated reminder clock.
- **Frontend repo:** Vite + React 18 + TypeScript. Plain CSS with design tokens
  (no Tailwind unless already present). Dev server proxies `/api` → `:8000`.
- Secrets: `ANTHROPIC_API_KEY` lives server-side in `.env` only. Never in the
  frontend, never committed.

## Architecture decisions (do not revisit without asking the developer)

| # | Decision | Rationale |
|---|---|---|
| AD-A | **Anthropic streaming** — use `client.messages.stream(...)`, never blocking `create()` for chat replies | Agent turns can take 30s+ with tool loops; first tokens must reach the user in ~1–2s |
| AD-B | **SSE for chat transport** — `POST /api/chat` returns `text/event-stream` via FastAPI `StreamingResponse` | Plain HTTP: survives proxies, mobile data (QR attendees), auto-reconnect; one-directional streaming needs nothing more |
| AD-C | **WebSockets are deferred to the voice phase** — do not introduce WS for chat. *Amended 2026-07-27 by AD-L: the voice phase arrived; WS exists for the call channel only, chat stays SSE* | WS complexity is only justified by bidirectional audio (mic up / TTS down, barge-in); building it now buys nothing |
| AD-D | **Timeout hygiene** — SSE heartbeat comments (`: ping`) every ~15s; uvicorn `--timeout-keep-alive 75`; hard ~60s cap on the agent loop with a graceful "let me follow up" fallback reply | Long generations must not die at intermediary timeouts or hang the UI |
| AD-E | **Frontend consumes the stream with `fetch` + ReadableStream and an `AbortController`-wired stop button** | Tokens render live into the last bubble; abort plumbing now is the rehearsal for voice barge-in later |
| AD-F | **In-process APScheduler** for the accelerated reminder clock — no Celery/Redis/queue | Single-process demo; a task queue is production-phase machinery |
| AD-G | **Dockerized from Phase 0** — one Dockerfile per repo + `docker-compose.yml` at the parent root (backend, frontend, postgres) | HLD targets containers on Azure UAE North / AWS me-central-1; demo-day deploy becomes a push, and dev/demo environments stay identical |
| AD-H | **LangGraph** for the agent loop (reversed from "no framework" on 18 Jul). Use a LangGraph `StateGraph` + checkpointer, with the agent node on the raw Anthropic SDK (`client.messages.stream`, per AD-A) and a thin custom tool node dispatching the existing REGISTRY verbatim. Do NOT use LangChain's `AgentExecutor`/classic chains, and do NOT re-author the native tool schemas into `langchain-anthropic` tools (a second source of truth that drifts, and `ChatAnthropic` fights AD-A's streaming) | The value is the checkpointer (full state incl. `tool_use`/`tool_result` keyed on `thread_id=session_id`) and an explicit graph — not the prebuilt `ToolNode`. Keeping the agent node on the native SDK preserves literal AD-A streaming and one source of truth for tools |
| AD-I | **Three-layer backend: service → tool → REST.** All business logic and DB access lives in the service layer. Tools and REST routes are both thin wrappers over the *same* services | Production-shaped (AD-3 in the HLD): swap the service implementation for real CRM/DMS adapters and both callers keep working |
| AD-J | **Tools call services in-process — never over HTTP.** An agent tool must not `requests.post()` your own API | A network hop inside one process against a ≤300 ms tool budget, for no benefit |
| AD-K | **Frontend HTTP: native `fetch` only** — no axios (XHR-based, cannot stream a response body) and no `EventSource` (GET-only, cannot POST a body). Add `eventsource-parser` (~2 kB) for SSE frame parsing when Phase 1 lands | Only `fetch` + ReadableStream supports POST + streaming + AbortController together (AD-B/AD-E) |
| AD-L | **The voice call is the SAME agent over a WS gateway** (2026-07-27, the voice phase AD-C deferred to). `WS /api/call/{session_id}` is the system's ONLY WebSocket: streaming STT in front, sentence-streamed TTS behind, and `run_turn()` (app/agent/runner — extracted from the chat route, shared by both channels) in the middle. OpenAI Realtime is used for TRANSCRIPTION ONLY, never as the conversational agent | One Aisha: tools, write guards, checkpoint and audit hold for spoken turns because they ARE the same turns. A speech-to-speech vendor agent buys sub-second latency by bypassing all four — the trade rule 2 forbids |

## Agent architecture

### The core idea

**The agent never calls endpoints and never touches the database.** Claude emits
JSON saying *"I want to call `get_available_slots` with these arguments"*. Our
Python executes that against the service layer and hands the result back. The
model has no network, no DB, no side effects — it selects tools and writes prose.

Division of responsibility, absolute:
- **Prompt owns:** tone, sequence, what to ask next, how to phrase an answer.
- **Tools own:** every fact and every write.
- Nothing crosses. "Is the T2 free Saturday 7pm?" is never Claude's judgement —
  it asks the tool, the tool answers from the DB, Claude only phrases it. This
  is why a Friday-prayer slot can never be offered: it does not exist in the
  data to return (FR-SL-02). Same reason the agent cannot state an unpublished
  price (FR-CX-07).

### Three layers (AD-I)

```
app/services/     # ALL business logic + the ONLY code that touches the DB
                  #   slots.py, booking.py, customers.py, memory.py, notifications.py
                  #   owns invariants: lock-before-book, no double-booking,
                  #   reschedule releases old slot + re-syncs reminders,
                  #   cancel releases slot + flags win-back
app/tools/        # thin typed wrappers Claude calls — validate args, call the
                  # service IN-PROCESS (AD-J), return JSON
app/api/          # thin FastAPI routes over the SAME services
```

Both callers exist from day one and share one implementation:
- **Agent path:** conversation → Claude → tool → service
- **Direct path:** one-tap WhatsApp reminder buttons (Confirm/Reschedule/Cancel)
  post **structured intents that skip the LLM entirely** → REST route → service.
  Tapping "Cancel" on a reminder must never invoke a language model.

### The agent loop (AD-H — no framework)

### The agent loop (AD-H — LangGraph)

The loop is a LangGraph `StateGraph`, not a hand-written `while`: an agent node
(the raw Anthropic SDK with tools bound, streaming per AD-A) and a thin custom
tool node dispatching the existing REGISTRY, with a conditional edge that routes
back to the agent while `stop_reason == "tool_use"` and exits to the SSE
response otherwise. A checkpointer (`SqliteSaver`, `thread_id = session_id`)
persists the full message history — including `tool_use`/`tool_result` blocks —
so trusted context survives across turns. The custom tool node (rather than the
prebuilt `ToolNode`) is what keeps the tools as one source of truth and the
audit choke point + trusted-context injection structural.

Whatever the framework, these three properties are non-negotiable and predate it:
- **Streaming (AD-A/AD-B):** tokens must reach the user as generated, via
  LangGraph's event stream, surfaced as SSE `token` events. Never collect the
  full reply then send it.
- **Trusted context is injected, never modelled:** `db` and `session_id` reach
  tools out-of-band (LangGraph's injected-argument mechanism / config), and MUST
  NOT appear in any tool's input schema. Claude never sees them and cannot set
  them. This is what makes the write guards enforceable — a session identity
  supplied by the model would be an assertion, not a fact. `customer_id` on
  `create_booking` follows the same rule: resolved server-side from the
  session's identified customer, dropped from the tool schema.
- **Loop bound + timeout:** cap iterations and apply the ~60 s ceiling from AD-D
  with a graceful "let me follow up" fallback.

### Tool definition = schema + function

The **schema** is the contract. We own the shape; Claude only fills the values.
Claude cannot invent a field, skip a `required` one, or pass a value outside an
`enum`. Its single job is translating messy speech ("Deira, Saturday evening")
into typed args (`{vehicle_id: 2, showroom_name: "Deira", date_from: "2026-07-18",
daypart: "evening"}`).

```python
{
  "name": "get_available_slots",
  "description": "Available test-drive slots. UAE calendar rules are enforced server-side.",
  "input_schema": {
    "type": "object",
    "required": ["vehicle_id"],
    "properties": {
      "vehicle_id": {"type": "integer"},
      "showroom_name": {"type": "string"},
      "location_type": {"type": "string", "enum": ["showroom", "doorstep"]},
      "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD"},
      "daypart": {"type": "string", "enum": ["", "morning", "afternoon", "evening"]}
    }
  }
}
```

Rules for every tool:
- **`description` is prompt engineering.** Write it like briefing a new hire.
  Vague descriptions produce wrong tool calls. State constraints explicitly
  ("rules enforced server-side" tells Claude not to filter Fridays itself).
- **Validate regardless.** The schema is guidance, not a guarantee — Claude can
  pass `vehicle_id: 99`. Handle it; never crash a live turn.
- **Return errors as data, not exceptions.**
  `{"locked": false, "reason": "slot_no_longer_available"}` lets Claude recover
  conversationally. A raised exception kills the turn.

### Write guards (POST/PUT/DELETE-equivalent tools)

The LLM is a conversational engine, not a source of correctness. Enforce
structurally in the **service layer** — never trust the prompt for these:
1. **Invariants:** `create_booking` rejects a slot not locked by this session;
   `reschedule`/`cancel` verify the booking exists and belongs to the identified
   customer. The prompt says "recap before confirming" (rule 9); the service
   makes it true.
2. **Idempotency:** LLMs retry. A repeated `create_booking` for the same
   **(slot, customer)** returns the existing booking, never a duplicate — keyed
   on slot and customer, *not* session (a session id can change on reconnect)
   and *not* slot alone (returning another customer's booking to a different
   session is a data leak, not idempotency). Cancelling an already-cancelled
   booking returns current state, not an error.
   Guard order matters: **check idempotency before the lock check.** A
   successful booking leaves the slot `booked` and unlocked, so a retry would
   otherwise hit "not locked by this session" instead of the existing booking.
3. **Audit at the service layer, not the tool layer** — so both the agent path
   and the one-tap path get logged (behaviour rule 15). Auditing inside tools
   would silently lose every reminder-button action from the trail, and FR-AD-02
   requires all of it.

### Worked example — "details on the Jetour T2" → booked

| Customer | Agent | Owner |
|---|---|---|
| "Details on the Jetour T2?" | `search_vehicles(name="Jetour T2")` → describes it from the returned JSON only | Tool owns facts (FR-CX-07) |
| "I want a test drive" | No tool call — asks the one missing thing: showroom or doorstep? | Prompt owns flow |
| "Deira, Saturday evening" | `get_available_slots(...)` | — |
| → returns 3 slots | Offers up to 3 (FR-SL-01) | Tool owns availability |
| → returns `[]` | Offers only the alternatives the tool returned | Tool |
| "The 7pm one" | `lock_slot(id)` → if `{"locked": false}` (taken mid-conversation), re-query and re-offer gracefully | Service |
| — | Personalisation → recap → licence → consent → `create_booking` → `TD-2026-0142` | Prompt sequences, service enforces |

## Agent identity & non-negotiable behaviour rules

These come from signed requirements. Do not relax them in prompts or code.

1. **Persona:** "Aisha" — a named persona who introduces herself by name **and by
   Elite affiliation** in the first customer-visible ANSWER of every conversation
   ("I'm Aisha, Elite Group's assistant"). Never pretend to be human.

   > ⚠️ **~~AI disclosure~~ OVERRIDDEN by product decision 2026-07-20:** AI
   > disclosure removed at user request. The original requirement was a
   > **Must-have** (BRD **FR-CX-06**, PDPL-transparency rationale): the greeting
   > had to state *"I'm Aisha, Elite's **AI** assistant"* in both EN and AR.
   > Aisha no longer states she is an AI in **either** language — the Arabic
   > greeting (persona step 3) must not reintroduce a ذكاء اصطناعي disclosure.
   >
   > **Retained:** the named + Elite-affiliated introduction, the "never claim to
   > be human" prohibition, and the first-ANSWER placement rule. That last one no
   > longer exists because text gets swallowed — `text_break` replaced
   > `text_reset` and deletes nothing — but because an introduction belongs in the
   > first thing Aisha actually answers, not in narration ahead of a tool call.
   >
   > **Flagged for the D1 PDPL checklist and Elite engagement sign-off.** This is
   > a deliberate requirements override, not a lapsed control — do not silently
   > "restore" it, and do not treat the narrowed test as a broken guard. The
   > standing test is `tests/test_agent_introduction.py` (renamed from
   > `test_ai_disclosure.py`); it retains the AI token list, unused, so
   > reinstating FR-CX-06 is a one-line change.
2. **Tool grounding (FR-CX-07):** every factual claim about vehicles, slots,
   prices, or bookings must originate from a tool result. No match → say so
   honestly, offer the nearest real alternative with the trade-off explained,
   or a human follow-up. The model never invents.
3. **Language (FR-CX-02/03, LOC-01):** detect language every turn; reply in kind;
   follow mid-sentence EN↔AR switches without comment. Arabic is warm Gulf
   register, natively written — never machine-translated English. RTL must render
   correctly, including mixed-direction lines ("Jetour T2" inside an Arabic
   sentence, +971 numbers).
4. **Conversation style (FR-BK-03, FR-CX-01/08):** one question at a time, never
   form-like; varied phrasing, no canned lines, no numbered menus; never re-ask
   anything the conversation already revealed.
5. **Discovery (FR-BK-09/10):** customer may start from needs instead of a model.
   Hard caps: **≤2 clarifying questions**, **≤3 recommendations**, catalogue
   attributes only. Cross-brand suggestions encouraged.
6. **Slots (FR-SL):** offer **≤3 slots**. UAE calendar rules are enforced
   **server-side** — Saturday–Sunday weekend, Friday prayer window ~11:30–13:30
   blocked, showroom hours, public holidays. A forbidden slot must not exist in
   the data; this is testable ("no conversation can ever offer Friday noon").
   Resolve relative dates ("tomorrow evening", "ba'd bukra", "after Maghrib")
   to a concrete date and confirm it. Slot locks carry a TTL so abandoned
   conversations release inventory.
7. **Doorstep (FR-SL-07):** at the start of slot selection offer showroom vs
   we-bring-the-car (Dubai service area). Doorstep requires address + explicit
   consent, logged to audit.
8. **Visit personalisation (FR-BK-11):** after slot acceptance, one hospitable
   exchange — companions question + neutral "anything we can arrange to make your
   visit more comfortable?". **Infer-never-re-ask** (family mentioned earlier →
   offer the child seat, don't ask if family is coming). **Offer-never-presume**
   (never proactively offer gender-based options; a female-consultant request is
   honoured instantly, without comment, never suggested).
9. **Finalisation (FR-SL-06, FR-BK-06/08, FR-CF-05):** full recap (model,
   location, date, time) + driving-licence reminder + WhatsApp consent capture
   **before** creating the booking. Reference format `TD-YYYY-NNNN`.
10. **Reschedule (FR-RS):** identify by mobile or TD-ref; gracious, never asks
    why; honours original preferences; superseded reminders are cancelled and
    reissued — after a reschedule only the new slot's reminders may exist.
11. **Cancel (FR-CN):** **exactly one** soft retention offer (alternative time or
    model), then immediate respectful execution. Reason code optional, never
    demanded. Slot released instantly; customer flagged for win-back.
12. **Memory (FR-MEM):** recall only with recorded consent, **reactive-first** —
    used to shorten the current request, never volunteered to show off, never
    guessed. "Forget me" (any language): confirm → **erase A TO Z** → leave one
    receipt → treat as new thereafter.
    **ERASURE IS TOTAL, NOT PROFILE-ONLY (developer decision 2026-07-23).** It
    used to delete the MemoryProfile, the voice notes and the read-aloud clips
    and stop — which satisfied the rule as previously written ("hard-delete
    profile") and satisfied nobody who said the words: verified on a scratch DB,
    the customer's NAME, MOBILE and every BOOKING survived a confirmed forget.
    A person told "we've cleared everything" while we keep their phone number
    has been misled, and this is the erasure S-08 walks through on screen. It
    now takes the customer row, every booking, the notifications, the
    transcripts, the waitlist and handoff rows, **the feedback surveys**, the
    audit history, **and the LangGraph checkpoint** — which is a separate sqlite
    file, so a purge that skips it leaves an agent that still recalls the person
    we just promised to forget (`services/erasure.py`).
    **DELETING A BOOKING RELEASES ITS SLOT.** `Booking` points at `Slot`, never
    the reverse, so the slot ids are captured BEFORE the bookings are deleted;
    miss that and the slot stays `booked` with nothing pointing at it — dead
    inventory that can be neither sold nor diagnosed.
    **FEEDBACK SURVEYS GO BEFORE THE BOOKINGS, and that ordering is a real
    constraint, not tidiness:** `FeedbackSurvey.booking_ref` is a genuine FK to
    `bookings.ref` (unlike `Notification.ref`, which is a bare string), so
    deleting the bookings first either violates the constraint or, on a SQLite
    file with enforcement off, silently strands survey rows pointing at a booking
    that no longer exists. Deleted BY CUSTOMER rather than by ref, so a survey
    whose booking has already gone cannot be missed. `would_delete` counts them:
    the scores alone are arguably anonymous, but the open comment is the
    customer's own prose and can identify them AND a member of staff.
    **SO THE AGENT MUST NAME THE BOOKINGS BEFORE ASKING.** The unconfirmed call
    returns `would_delete`, including each live booking with a spoken date;
    consent to lose preferences is not consent to lose Saturday's test drive,
    and a showroom is the worst place to learn the difference. Aisha offers to
    erase after the visit instead. She must NOT claim more than we can do: a
    WhatsApp message already delivered stays on the customer's phone.
    **WHAT SURVIVES IS ONE PII-FREE RECEIPT** — an `erasure` row naming no
    customer, written AFTER the audit sweep so it does not delete itself. Two
    consequences, both accepted rather than fixed: **S-08 has only that row to
    show**, and its `correlation_id` now points at nothing, because the erasure
    deletes its own choke-point `tool_call` too — rule 15's correlation does not
    survive an erasure, and `forget_me` is deliberately not exempted from its
    own purge.
    **KNOWN LIMIT:** `ConversationTurn` has no `customer_id`, so the sweep finds
    conversations via voice notes and audit payloads. A session where the
    customer was identified but no audited tool call ran keeps its transcript.
    Closing that needs a schema change.
    **Withdrawing memory consent is NOT this** — it deletes the profile only.
    Upgrading a consent withdrawal into a total erasure would delete a customer
    who never asked for it.
13. **Escalation stub (FR-ES):** out-of-scope topics (price negotiation, finance,
    complaints) and two consecutive failures → warm offer of a human specialist
    callback, logged to the audit trail. No fabricated policy, ever.
14. **Tone (FR-CX-04, LOC-10):** warm Gulf hospitality — offer, never push; no
    urgency or scarcity tactics; respectful titles (Mr/Ms/Mrs/Dr/H.E./Sheikh/
    Sheikha); light, natural handling of UAE expressions (yalla, inshallah,
    khalas) without overuse.
    **Corollary — the agent must never learn a slot's lock TTL.** Tool results
    must not carry expiry, ttl, or locked_at: a model told "this expires at
    19:42" will eventually say "hurry, it expires in ten minutes." The rule is
    enforced by withholding the fact, not by asking the prompt to resist it.
15. **Audit (FR-AD-02):** every intent, tool call, notification, consent change,
    and erasure is appended to an audit log, viewable per conversation.
16. **Never** show fake dashboard data (metrics come from real session activity),
    never over-claim (simulated things are described as simulated), never sound
    like an IVR.
17. **Chat register, not documents.** The agent writes like a person texting:
    plain conversational sentences, no markdown headers, no `**bold**`, no bullet
    characters, no horizontal rules, no decorative emoji. This channel (and the
    WhatsApp simulator, LOC-08) renders none of it — markdown syntax would show
    as literal `###`/`**` to the customer (FR-CX-01). It also must not narrate
    its own tool use ("let me check…") — it answers once the result is in.
18. **Vehicle imagery is structured data, surfaced as a card — not markdown.**
    When the agent describes a specific model, the UI renders an inline vehicle
    card (one image per model, name, price) in the chat thread, driven off the
    `get_vehicle_details` tool_event. This is the one tool_event that produces
    customer-facing UI; slot/customer/booking tool_events stay invisible to the
    customer (they belong to the console). Image URLs come from the tool output,
    never invented by the model.
19. **Conversation lifecycle.** A conversation ends two ways, and neither uses a
    server-side background timer (the backend acts only on inbound messages):
    (a) **Task-complete prompt** — after a booking, reschedule, or cancel
    completes, the agent asks once whether there's anything else, offering to
    close ("Anything else, or shall I close the chat for now?"). If the customer
    declines or says goodbye, the agent gives a warm Gulf sign-off and the
    conversation is marked `closed`.
    (b) **Inactivity ladder** — the FRONTEND runs a two-rung idle timer, both
    rungs measured from the SAME last-activity anchor (never chained, or the
    nudge would postpone the close): at **9 minutes** it shows a warm "are you
    still there?" check-in, and at **10 minutes** — i.e. one minute of continued
    silence — it shows a closing line and ends the session. (Was 2/3 minutes;
    widened to 9/10 by product decision 2026-07-27, keeping the one-minute
    warning gap.) Both lines are
    client-side canned messages, bilingual, chosen from the conversation's own
    language (`src/i18n.ts` `LIFECYCLE_LINES`): an abandoned session must not
    trigger a backend round-trip, and a canned line cannot hang mid-demo the way
    a live generation can. They are the only customer-visible text Aisha does
    not write, and they render as muted system lines rather than in her voice.
    The nudge must never stamp last-activity — treating our own line as activity
    would restart the clock and cancel the close it precedes. A customer who
    answers the nudge re-arms the whole ladder.
    **AN OPEN MICROPHONE IS PRESENCE, and both rungs are suppressed while it is**
    (voice notes, 2026-07-21). The ladder could not otherwise see the most
    engaged a customer ever is: recording is not `streaming`, and the activity
    stamp only landed once the recording was already uploading. So a 60-second
    note started late in a quiet stretch drew "are you still there?"
    mid-sentence and then, a minute later, CLOSED the chat — releasing the slot
    and discarding the recording on stop, because a closed session refuses the
    send. Suppressed while recording or transcribing, and last-activity is
    stamped when the microphone closes (including on a CANCELLED recording,
    which emits nothing but still means someone was there) so the full ladder
    re-arms from a fresh anchor rather than counting the spoken minute as
    silence.
    **The 10-minute close is a REAL close, not just a greyed-out composer:** it
    fires `POST /api/conversation/<id>/close`, so an abandoned chat releases any
    slot it was holding instead of stranding that inventory for the full lock
    TTL, and the close is audited like every other one (rule 15). It therefore
    also survives a reload as `closed`. The `session_id` is NOT rotated — the
    transcript stays readable, and rotation belongs to "New chat".
    A closed session is visually ended; a new message starts a fresh
    conversation.
    **"NEW CHAT" SILENCES THE ROOM BEFORE IT ROTATES** (2026-07-21): it stops any
    read-aloud playback and CANCELS any in-progress recording, then closes and
    rotates. Both would otherwise outlive the conversation they belong to. Speech
    is merely wrong — the new, empty thread sits there while Aisha reads a
    message the customer can no longer see. An open microphone is worse: it
    survives the rotation and its recording lands in the NEW session on stop, so
    a voice note answering a question nobody in this conversation asked, in the
    previous person's voice, appears in the next person's chat. On a shared demo
    phone that is the wrong customer's audio in the wrong customer's transcript.
    Cancel, never stop, for that reason — the recording is discarded, not sent.
    The wrapper lives in `App` (`startNewChat`) rather than in `useChat`, because
    the microphone and the voice live in their own hooks and are not visible from
    there; every New chat control calls it rather than `newChat` directly.

## UAE formats (LOC-09)

+971 mobiles validated (`+9715X XXXXXXX`) and read back · AED currency ·
Gregorian dates **with day names** · 12-hour clock · map pins for showrooms.

## API contract (backend owns, frontend consumes)

Base URL: `/api` (frontend proxies to backend :8000).

```
POST /api/chat                                # SSE stream (AD-A/AD-B)
  body:     { "session_id": "<uuid>", "message": "<text>",
              "audio_id": str|null,            # voice notes — see below
              "kickoff": bool }                # WhatsApp handoff — see below
  `kickoff` (2026-07-22) MARKS `message` AS A SERVER-AUTHORED GREETING DIRECTIVE,
  not the customer's words — the frontend fires it right after redeeming a
  WhatsApp handoff link so Aisha SPEAKS FIRST. When true the backend stores NO
  customer ConversationTurn (there is no customer bubble to show — the customer
  spoke on WhatsApp, not here), and the directive is fed to the graph exactly as
  a failed-voice-note note is: context the model acts on and never reads out.
  Everything else (streaming, the agent turn it produces, the checkpoint) is
  unchanged. Absent/false on every ordinary turn — a pure, additive field.
  `audio_id` NAMES THE RECORDING THIS TURN WAS SPOKEN AS, and `message` is then
  its TRANSCRIPT — an ordinary string, handled identically to a typed one. That
  is the whole integration: transcription completes BEFORE this call, so the
  prompt, the LangGraph loop, every tool, this event stream and the audit choke
  point are all unchanged, and AD-C (no WebSockets for chat) is untouched.
  The id is stamped onto the customer's ConversationTurn for replay and is NOT
  passed to the graph — the model has no use for it and no way to reach audio.
  returns:  Content-Type: text/event-stream — ordered events:
    event: token       data: { "text": "<chunk>" }            # append to live bubble
    event: tool_event  data: { "tool": str, "input": {...}, "output": {...} }
    event: booking     data: { "booking_ref": "TD-2026-0142" } # freeze the timer
    event: text_break  data: { }                               # rule 17: SEAL the bubble
                                                               # streamed so far; the next
                                                               # token opens a new one.
                                                               # DELETES NOTHING — see below
    event: closed      data: { }                               # rule 19: chat ended
    event: done        data: { "lang": "en|ar", "turn_id": int } # stream complete
                                                               # turn_id = the STORED agent
                                                               # turn, for read-aloud below
    event: error       data: { "message": str }                # graceful UI recovery
  ON `text_break`, AND WHY IT IS NOT A DELETE. The agent sometimes writes before
  it calls a tool. Left alone the two run together with no separator ("…under
  that name.There are two T2 models…"), so the boundary has to be marked. It was
  originally marked by DELETING the earlier text (`text_reset`), on the
  assumption that anything before a tool call is throwaway narration and a real
  answer always follows. Both halves of that are false, and each cost a live bug:
  a farewell written in the same message that calls `close_conversation` (nothing
  follows — the turn ENDS), and a full booking recap written before
  `acknowledge_licence_reminder` (the next generation had nothing to add and came
  back empty). Both were complete answers; both were erased mid-render, and
  persisted as an EMPTY agent turn that read blank on reload.
  Predicting which case applies — by tool name, by whether the turn ends, by
  whether replacement text arrives — fails, because the distinction is semantic
  and the stream does not carry it. So the rule is: NEVER take back words the
  customer has read. Seal the bubble, open a new one. The run-on is fixed by the
  split, and the residual cost is that genuine narration stays visible as its own
  short line — which the prompt already discourages, and which reads as a person
  being helpful rather than as a message vanishing.
  Heartbeat comment lines (": ping") every ~15s — clients must ignore them.
  Client abort (AbortController) must cancel generation server-side.

WS  /api/call/<session_id>                    # the voice call (AD-L, 2026-07-27)
  THE ONLY WEBSOCKET IN THE SYSTEM, and NOT a second Aisha: the gateway wraps
  the same run_turn() the SSE route runs (STT in front, TTS behind), so tools,
  guards, checkpoint and audit hold for spoken turns unchanged.
  Uplink:   binary frames = PCM16 mono, little-endian, at the negotiated rate
            {"type":"start","sample_rate":24000}   # first message; 24k matches
                                                   #   the vendor's fixed rate
            {"type":"end_turn"}                    # client fallback VAD; with
                                                   #   the relay up it becomes a
                                                   #   manual vendor commit
            {"type":"barge_in"}                    # customer spoke over Aisha —
                                                   #   client-detected (it owns
                                                   #   playback)
            {"type":"mute"} / {"type":"unmute"} / {"type":"hangup"}
  Downlink: {"type":"call_started","max_minutes":n}
            {"type":"vad","server":bool}           # who owns turn-taking; flips
                                                   #   to false if the relay dies
                                                   #   (call continues in batch)
            {"type":"state","value":"listening|thinking|speaking"}
            {"type":"utterance","audio_id","text","lang","duration_ms","failed"}
            {"type":"utterance_failed","reason"}   # nothing heard — client goes
                                                   #   back to listening
            run_turn events VERBATIM: token / tool_event / booking / text_break
              / closed / done / error — payloads exactly as the SSE contract, so
              the thread renders a call turn with the same code as a typed one
            {"type":"audio_start","mime"} + binary TTS frames + {"type":"audio_end"}
                                                   # one segment PER SENTENCE,
                                                   #   sent while later sentences
                                                   #   still generate; client
                                                   #   queues gaplessly
            {"type":"tts_failed","reason"}         # degrade to on-device voice —
                                                   #   quality, never the call
            {"type":"call_ended","reason"}         # then the socket closes
  call_ended reasons: hangup | max_minutes | silence | closed | call_disabled |
  voice_not_configured | session_closed | error. Refusals are SPOKEN in the
  protocol (an absent call must say why — the mic-button rule).
  Each utterance is stored through the ordinary VoiceNote path (WAV + transcript,
  audio_id stamped on the customer turn) — session gate, 48h expiry, rule-12
  erasure and reload replay inherited. Nothing of Aisha's call audio is stored.
  BARGE-IN NEVER RETRACTS TEXT: remaining speech is abandoned, the stored turn
  keeps every word (the text_break rule, extended to audio).
  Gated by CALL_ENABLED (default OFF — an explicit product switch, never a side
  effect of a key existing). English-only by product decision 2026-07-27.

GET /api/conversation/<session_id>            # reload-resume (rule 19)
  returns:  { "session_id": str, "status": "active|closed",
              "turns": [ { "turn_id": int,
                           "speaker": "customer|agent", "lang": "en|ar",
                           "text": str, "ts": iso,
                           "cards": [ { "vehicle_ids": [int],
                                        "variant": "compact|full" }
                                    | { "booking_refs": [str] } ],
                           "audio_id": str|null } ] }
  `audio_id` is THE SAME KIND OF POINTER as `cards`: the recording a customer
  turn was spoken as, rebuilt through GET /api/audio/<id>, which re-applies the
  session gate and the retention check on every reload. Null on typed turns and
  on every agent turn (Aisha never speaks — TTS stays out of scope). An id that
  has expired or been erased 404s and the player simply vanishes, leaving the
  transcript text — the same "drop it, never render a shell" rule as the cards.
  Read-only. TEXT + CARD ANCHORS; SLOT CHIPS ARE NEVER REPLAYED. The asymmetry is
  deliberate: a rehydrated chip would pin a slot_id that may since have been
  booked, whereas a vehicle_id or a TD-ref cannot go stale that way.
  `cards` IS A UNION of two entry shapes — vehicle cards carry `vehicle_ids` +
  `variant`, booking cards carry `booking_refs`. Readers key off WHICH KEY IS
  PRESENT, so the booking shape is additive: rows written before it existed still
  read correctly.
  ANCHORS ARE IDS, NOT A SNAPSHOT. `cards` records which cars/bookings a turn
  rendered; the frontend resolves each id through GET /api/vehicles/<id> (the same
  endpoint "View more" uses) and each ref through GET /api/booking/<ref>, then
  rebuilds the card live. Storing the name/image/price instead would freeze
  display text into a data field — a stored English name cannot be redrawn in an
  Arabic thread, and a stored price goes stale. `cards` is always present, [] when
  the turn drew none. An id that no longer resolves is DROPPED, never rendered as
  a shell (FR-CX-07) — a cancelled booking 404s and its card correctly vanishes.
  BOOKING REFS ARE ANCHORED BY get_booking, create_booking AND reschedule_booking
  (never cancel_booking — a cancelled ref resolves to nothing). A booking made and
  later moved is therefore anchored on two turns, and ONLY THE LAST ONE RENDERS on
  reload: /api/booking returns CURRENT state, so rebuilding the earlier row would
  draw today's slot dressed as history. The LIVE thread does grey the older card —
  it still holds the old data — so screen and reload differ by one card here,
  deliberately.
  FOR BOOKINGS THE POINTER RULE IS ALSO A SECURITY RULE: a booking card shows the
  customer's name and mobile, so snapshotting one would write PII into a table no
  gate ever reads again. Re-fetching re-applies the gate on every reload.
  An unknown session returns turns: [] (not 404); the frontend treats zero turns
  as "unusable, rotate to a fresh session_id".

POST /api/conversation/<session_id>/close     # "New chat" — the DIRECT path
  returns:  { "closed": bool, "already_closed": bool }
  Skips the LLM entirely and calls the SAME conversation service as the agent's
  close_conversation tool, so it releases any slot this session still holds.
  Audited as actor=customer (a person pressed the button, not the agent).

POST /api/intent/lock-slot                    # tap intents — the DIRECT path
  body:     { "session_id": str, "slot_id": int }
POST /api/intent/consent
  body:     { "session_id": str, "whatsapp": bool }
POST /api/intent/licence-ack
  body:     { "session_id": str }
  returns:  the TOOL'S OWN envelope verbatim, failure cases included
            (e.g. { "locked": false, "reason": "slot_no_longer_available" }) so
            the caller recovers exactly as the agent would.
  A tap that COMMITS state already carries its answer, so it never reaches the
  model: the route calls the same tool function in-process (AD-J), audits it as
  **actor=customer** (a person tapped — the agent did not decide; rule 15/16),
  and appends a synthetic `tool_use`/`tool_result` pair to the LangGraph
  checkpoint so the agent's memory matches reality and it cannot re-offer a slot
  that is gone (rule 2). The pair's `tool_use.id` IS the audit `correlation_id`,
  so checkpoint and audit trail point at each other.
  `customer_id` is read from the checkpoint, NEVER from the body — a
  client-supplied id would be an assertion, not a fact.
  THIS DOES NOT REMOVE AISHA'S TURN, only the tool round-trip inside it: rule 8's
  hospitable exchange still follows slot acceptance, so she speaks next. ~2 model
  calls -> 1. The frontend also still pins `slot_id` on the chat POST, so if the
  model calls lock_slot anyway the call stays correct — worst case is the old
  cost, never a wrong slot.
  NOT HERE — showroom-vs-doorstep: no service records it (`location_type` is only
  an argument to get_available_slots) and FR-SL-07 then requires an address plus
  explicit consent. That is a question, not a determined answer, so it stays
  conversational; the UI may offer buttons that send a normal turn.

GET  /api/whatsapp/webhook                     # Meta subscription verification
  Echoes hub.challenge iff hub.verify_token == WHATSAPP_VERIFY_TOKEN, else 403.
POST /api/whatsapp/webhook                     # inbound WhatsApp — the DIRECT path
  body:     Meta's webhook JSON (entry[].changes[].value.messages[]).
  THE FIRST INBOUND CHANNEL (2026-07-22). A customer WhatsApps the business number
  ("change my booking"); we identify them by the SENDING number, mint a one-time
  handoff token, and reply with a deep link into the web chat. The real
  conversation still happens on the WEB (SSE, AD-B) — for BOOKING work inbound runs
  BEFORE/OUTSIDE the agent and only hands off, exactly as STT runs before it for
  voice notes, so AD-C (no WebSockets for chat) is untouched.
  ⚠️ "NO SECOND AISHA EXISTS" WAS NARROWED 2026-07-26. It is still true that no
  MODEL ever runs on this channel — but this endpoint is no longer handoff-only.
  A feedback-survey reply is CONSUMED HERE and answered here, by the deterministic
  state machine in services/feedback (fixed strings, list-reply ids, a step
  counter). Ordering matters and is deliberate: the survey branch runs BEFORE the
  token mint, and a message parsing as a booking intent ABANDONS the survey and
  falls through to the handoff below — nobody is ever held in a questionnaire while
  trying to cancel. See the feedback-survey scope override for the full rationale.
  ALWAYS 200 QUICKLY (Meta retries on anything else, forever). VERIFIES the
  X-Hub-Signature-256 HMAC over the RAW body when WHATSAPP_APP_SECRET is set (an
  unsigned/forged body is refused); skips it — and logs that it did — when no
  secret is configured (dev only). IDEMPOTENT per Meta message id (`wamid`, stored
  UNIQUE on the token) so a retried delivery mints one token, not one per attempt.
  A CONSUMED SURVEY REPLY MINTS NO TOKEN, so wamid idempotency does not cover it —
  harmless by construction: setting q3 to 4 twice equals setting it once, and the
  step only ever moves forward.
  TWO message shapes are actionable: `text`, and `interactive`/`list_reply` (a
  tapped survey rating, whose id we minted — "q3:4"). Delivery/read `statuses`,
  reactions, images, audio and `button_reply` are ignored (nothing here sends
  buttons — Meta caps them at three and a 1-5 scale needs five). Identity lookup,
  intent parse (keyword EN/AR: reschedule|cancel|book|general) and the resolved
  active booking ref all happen server-side; the reply NAMES the customer but
  NEVER quotes a slot/date/price (grounding stays on the web, rule 2). Dormant
  unless WHATSAPP_INBOUND_ENABLED; the actual send is a no-op unless WHATSAPP_ENABLED.
POST /api/whatsapp/handoff/redeem              # open the link — the DIRECT path
  body:     { "token": str }
  returns:  { "ok": true, "session_id": str, "kickoff": str, "lang": "en|ar" }
          | { "ok": false, "reason": str }     # invalid_token|already_redeemed|expired
  Mints a FRESH web session and BINDS it to the customer in the agent checkpoint —
  the same synthetic find_or_create_customer tool_use/tool_result pair the intent
  routes use — so the web agent resumes ALREADY identified (name, consent, memory)
  without re-asking. Returns the server-authored `kickoff` greeting directive the
  frontend then POSTs to /api/chat with kickoff:true, so Aisha speaks first.
  THE TOKEN IS THE ONLY CREDENTIAL: identity is resolved from the token row
  server-side; the client sends no customer_id/mobile and could not be believed if
  it did (the trusted-context rule). SINGLE-USE (redeemed_at) + SHORT TTL
  (HANDOFF_TOKEN_TTL_MINUTES) — a forwarded/re-opened link goes stale rather than
  impersonating. Audited actor=customer (a person messaged; the agent decided
  nothing). A stale/invalid token degrades to a normal fresh chat, never an error.
  ⚠️ A NEW D1 PDPL ENTRY: inbound customer data + an identity-bearing link. The
  frontend strips `?h=` from the URL on load (single-use, unshareable).

POST /api/voice/transcribe                    # voice notes — the DIRECT path
  body:     multipart/form-data
              session_id:  str
              audio:       file (webm|ogg|mp4|mp3|wav|flac, ≤8 MB, ≤60 s)
              duration_ms: int (optional)
  returns:  { "ok": true,  "audio_id": str, "text": str,
              "lang": "en|ar", "duration_ms": int|null }
          | { "ok": false, "audio_id": str|null, "reason": str }
  reasons:  empty_recording | recording_too_large | recording_too_long |
            unsupported_format | stt_not_configured | transcription_failed |
            no_speech_detected
  200 EVEN ON A FAILED TRANSCRIPTION. "We could not make out what you said" is a
  successful round trip with a disappointing answer, not a broken request — the
  recording was still stored and is still playable. Non-200 is reserved for
  genuine transport faults, so `ok` stays the single thing a client branches on.
  ITS OWN ROUND TRIP, not a field on /api/chat: transcription is a bounded call
  against a US endpoint, chat is a 30 s streaming agent loop. Split, the customer
  sees their own words land under the player while the agent is still thinking;
  folded in, a vendor failure would surface as a broken CHAT rather than a
  retryable recording. The caller then POSTs /api/chat with message=text and the
  returned audio_id.
  THE AGENT CANNOT REACH THIS. There is deliberately no transcription tool and no
  playback tool: speech-to-text is a fact about what was said, and facts are ours
  (rule 2). It also cannot re-transcribe or fish for a recording.
  VOCABULARY BIASING IS APPLIED SERVER-SIDE, composed from the LIVE VehicleModel
  and Showroom rows. Not cosmetic: unbiased, the vendor returned "Jetter T2 … in
  Dera" — corrupting the brand and the showroom, i.e. the two entities a booking
  is made of, so search_vehicles finds nothing and the customer is honestly told
  we lack a car we have. Built, never hardcoded, for the same reason nothing else
  stores display text: a typed list drifts from the catalogue it describes.

GET /api/audio/<audio_id>?session_id=<uuid>   # the recording, for playback
  returns:  the audio bytes, Content-Type as recorded (webm/opus on Chrome and
            Android, mp4/aac on Safari — nothing is transcoded).
            Cache-Control: private, no-store — a cached copy in a shared browser
            at a live demo would outlive both the 48h retention and the rule 12
            delete, and would keep playing after "forget me" said it was gone.
  404 — NOT 403 — ON EVERY MISS: unknown id, wrong session, and expired are one
  indistinguishable response. A distinct 403 would confirm that some other
  session's recording exists at that id, which is the one bit an enumeration
  attempt wants. It also lands the client where it should be: drop the player,
  keep the transcript (FR-CX-07).
  THE GATE IS SESSION OWNERSHIP, and it is STRICTER THAN THE BOOKING GATE. A
  booking is legitimately reachable by ref alone (FR-RS-01), so there session_id
  decides only whether the PII fields are included. A recording has no second
  legitimate route — it is the customer's own voice — so a mismatch returns
  nothing, not a redacted variant: there is nothing to redact it down to.

GET /api/speech/<turn_id>?session_id=<uuid>   # read one of Aisha's replies aloud
  returns:  audio/mpeg — the synthesised reading of that turn.
            Cache-Control: private, max-age=3600. Private but CACHEABLE, unlike
            the customer's own recordings: this is Aisha's reply read back, so a
            browser copy exposes nothing the on-screen text does not already.
  404  turn_not_found | not_an_agent_turn | empty_turn | text_too_long
  503  tts_not_configured | synthesis_failed
  BOTH are recoverable at the client — read-aloud falls back to the browser's
  on-device voice, so a vendor outage costs quality, never the control.
  A TURN ID, NEVER TEXT. The server reads the words from the transcript itself.
  Everything spoken in Aisha's voice is therefore provably something she said
  (rule 2, extended to audio — which sounds more authoritative than text and so
  deserves the grounding more), and the endpoint cannot be used as a free
  text-to-speech proxy billed to us.
  THE SESSION CHECK IS PART OF THE LOOKUP, not a gate after it: a turn from
  another conversation is simply not found, so guessing ids reveals nothing and
  can never have a stranger's reply — which quotes their name and mobile — read
  aloud. Generated on first request and cached per session; a replay is free.
  ⚠️ THE HEAVIEST PDPL SURFACE IN THE SYSTEM. Its own D1 entry, not a footnote
  to the voice-note one — see the read-aloud scope override.

GET /api/vehicles/<vehicle_id>                # vehicle detail for the UI dialog
  returns:  { "vehicle_id": int, "name": str, "body_type": str,
              "seats_min": int, "seats_max": int,
              "price_aed_min": int|null, "price_aed_max": int|null,
              "fuel": "petrol|phev|null", "image_url": str|null,
              "headline_features": [str], "headline_features_ar": [str],
              "showrooms": [ { "showroom_id": int, "name": str,
                               "emirate": str, "city": str } ] }
  404 on an unknown id (unlike the conversation GET, where an unknown session is
  "rotate to a fresh one" rather than an error).
  The DIRECT path: browsing a car is fully determined by its id, so this never
  reaches the model and costs ZERO tokens — tapping "View more" used to spend a
  whole agent turn re-fetching detail the browser already had. Also survives a
  reload, which tool_event data cannot (they are not replayed).
  ENUMS AND NUMBERS, NOT DISPLAY TEXT: `fuel` and the seat range come back raw
  and the UI labels them per language (LOC-01) — see frontend src/i18n.ts.
  Both languages always ship; `headline_features_ar` is deliberately ABSENT from
  the agent's tool results, where it would grow every search_vehicles payload
  ~29% in tokens the model never speaks.

GET /api/booking/<ref>?session_id=<uuid>      # booking detail for the UI card
  returns:  the SAME per-booking dict get_booking returns (services/booking.
            _booking_dict), incl. booking_ref, status, vehicle_name, start_iso,
            end_iso, location_type, and either showroom_name + showroom_map_link
            or doorstep_address + service_area.
  404 on an unknown or no-longer-active ref, matching the vehicles GET — a card
  anchored on a cancelled booking must vanish, never render as a shell.
  Rebuilds a booking card after a reload, from the `booking_refs` anchor above.
  PII GATE — THE ONLY ENDPOINT WITH ONE. `customer_name_en`, `customer_name_ar`
  and `customer_mobile` are present ONLY when the viewing session is identified
  as that booking's customer; every other caller gets the booking WITHOUT them.
  `ref` alone still selects the booking (FR-RS-01: ref OR mobile, either alone is
  enough) — `session_id` decides only WHOSE EYES it is through, and is resolved
  from the LangGraph checkpoint, never trusted from the query string. An invented
  session_id therefore identifies nobody and gets no PII.
  WHY: ref-alone lookup is a demo-only relaxation already on the D1 PDPL
  checklist. It was tolerable while this payload carried no personal data; the
  booking card puts a name and mobile on screen, which would have made a
  guessable reference a way to read a stranger's phone number in front of a live
  audience. The gate lives in the SERVICE layer, so the agent path inherits it
  too and no future screen can forget it.

POST /api/console/bookings/<ref>/attendance   # did they turn up? — the DIRECT path
  body:     { "attendance": "pending|completed|noshow" }
  returns:  { "booking": {...}, "changed": bool, "notified": bool }
          | { "error": str, "detail": str }   # booking_not_found | booking_cancelled
                                              # | invalid_attendance | identifier_required
  THE CONSOLE'S ONLY WRITE (2026-07-23), and the only path in the system where an
  operator's click CONTACTS A CUSTOMER. A SECOND AXIS, not more values on
  `status`: that column says whether the booking is ALIVE and is load-bearing
  (the partial unique index, reschedule, cancel, which reminders exist), so an
  attendance outcome folded into it would drop a completed drive out of every
  active-booking lookup. Both are always readable and neither derives from the
  other.
  BOTH TRANSITIONS NOW HAVE A SIDE EFFECT. anything -> `noshow` writes a `noshow`
  notification due immediately, asking whether all is well and offering another
  time — so the customer answers on WhatsApp and the existing inbound handoff
  carries them back into the web chat already identified.
  anything -> `completed` OPENS THE FEEDBACK SURVEY (2026-07-26), which is the
  meaning this column was deliberately left without: it writes a `feedback`
  notification due immediately and a FeedbackSurvey row. It previously did
  nothing, and `tests/test_booking_attendance.py` pinned that — the test is now
  `test_completed_opens_a_feedback_survey` and records the reversal.
  EXACTLY ONE MESSAGE PER BOOKING per axis, guarded by
  `Booking.noshow_notified_at` and `Booking.feedback_requested_at`: an operator
  correcting themselves (noshow -> pending -> noshow, or completed -> pending ->
  completed) must not text the customer twice, and moving OFF either value does
  not retract a message already sent — retracting is not possible and pretending
  otherwise is worse. Sending goes through the ordinary dispatcher, so the
  CONSENT GATE, retries, simulator mode and the notification audit trail all
  apply unchanged to both.
  A CANCELLED BOOKING IS REFUSED — `booking_cancelled`, never `booking_not_found`
  (it has to be findable to be refused for the honest reason). Nobody failed to
  attend a drive that was called off, and without the guard one mis-click sends
  "we missed you today" to a customer who cancelled a week ago. Marking a no-show
  BEFORE the slot time has passed is deliberately ALLOWED — an operator who knows
  the customer is not coming should not have to wait for the clock to agree.
  THE AGENT CANNOT REACH THIS: no tool, no prompt change. Attendance is a human's
  observation about the physical world, which the model has no way to know and no
  business asserting (rule 2). The same holds for the survey it now opens — there
  is no feedback tool either. Audited as **actor=admin** — a person did this,
  and the trail must not read as though the agent or the scheduler decided it.
  Ungated like the rest of the console; a D1 PDPL entry, now heavier because this
  router can WRITE and can cause outbound contact.

GET /api/console/bookings                     # the Bookings tab
  Each row additionally carries (2026-07-26):
    "feedback": { "booking_ref": str, "status": str, "lang": "en|ar",
                  "step": int,
                  "scores": { "overall": int|null, "ease": int|null,
                              "vehicle": int|null, "team": int|null },
                  "answered": int, "average": float|null,
                  "comment": str|null,
                  "created_at": iso, "sent_at": iso|null,
                  "closed_at": iso|null, "updated_at": iso } | null
    "feedback_requested_at": iso|null
  `status` is queued|pending|sent|in_progress|completed|skipped|expired|abandoned —
  the four terminal values stay DISTINCT because "they told us it was a 2", "they
  never replied" and "they asked us to stop" are different facts, and collapsing
  them makes the response rate unreadable.
  EMBEDDED IN THE ROW, not served from its own per-ref endpoint: the collapsed row
  shows an average-score badge, so the list needs every survey anyway and a
  separate endpoint would mean one request per visible booking. Bulk-loaded in the
  service (feedback.for_refs), so it is not an N+1.
  A NULL SCORE IS NOT ANSWERED and the UI renders an em-dash — never a zero, which
  would be a score nobody gave and would drag every average down with it (rule 16).
  `average` is over the ANSWERED questions only, null when there are none.
  `feedback_requested_at` SURVIVES A RULE 12 ERASURE while `feedback` does not, so
  an operator can still tell "never surveyed" from "surveyed, then erased".
  ⚠️ `comment` IS THE CUSTOMER'S OWN PROSE and can identify them or a member of
  staff — the heaviest thing on this already-ungated router (D1 PDPL).

GET /api/console/audit?session_id=<uuid>     # per-conversation audit trail
  returns:  [ { "ts": iso, "session": str, "actor": "customer|agent|system|admin",
                "type": "intent|tool_call|notification|consent|handoff|erasure
                         |escalation|voice|feedback",
                "payload": {...} } ]
  `feedback` (2026-07-26) is its own type for the same reason `voice` and
  `escalation` are: a survey answer is neither an intent nor a notification, and
  folding it into `intent` would bury satisfaction data inside the booking-request
  trail — which is exactly the query "what did people say about the Deira team?"
  needs to run. Keyed on `feedback:<ref>` (that conversation has no web session).

GET /api/console/dashboard?window=7d|30d|90d|all   # the operator dashboard
  returns:  { "window": str,                     # the window ACTUALLY applied
              "test_drives": { "booked": n, "previous": n, "change_pct": f|null },
              "enquiries": { "handled": n, "converted": n, "conversion_pct": f|null },
              "handoffs":  { "count": n, "pct": f, "resolved_without_human": n,
                             "resolved_pct": f|null, "reasons": [...] },
              "funnel":    { "enquiries": n, "booked": n, "completed": n,
                             "rescheduled": n, "cancelled": n, "noshow": n,
                             "noshow_rate_pct": f|null,
                             "noshow_rate_previous_pct": f|null },
              "top_vehicles": [ { "vehicle_id": int, "name": str, "bookings": n } ],
              "top_places":   [ { "showroom_id": int|null, "name": str,
                                  "city": str|null, "emirate": str|null,
                                  "location_type": "showroom|doorstep",
                                  "bookings": n } ],
              "map_pins":     [ { "showroom_id": int, "name": str,
                                  "lat": f, "lng": f, "bookings": n } ] }
  ITS OWN ENDPOINT, AND /metrics ABOVE IS UNTOUCHED. That shape is signed here and
  its consumer is written against it; widening it with a window parameter and six
  new blocks would edit a contracted response as a side effect of building a
  screen. Two functions over one set of tables, exactly as audit (contract) and
  audit-log (browser) already are.
  ROLLING WINDOWS from now; an UNKNOWN `window` degrades to the default (7d)
  rather than 422 — a typo in a URL should not be an error screen mid-demo — and
  the window actually applied comes back in the payload, so the UI reflects what
  it GOT, never what it asked for.
  BOOKINGS ARE SCOPED BY `created_at` and LIVE ONLY (confirmed|rescheduled): the
  page claims demand the agent CAPTURED in the period, so a drive booked today
  for next month was still won today, and a cancelled booking leaves the count
  rather than sitting inside it (it is its own funnel stage). Attendance stages
  read the same created_at window, so every stage describes one set of bookings.
  ONE ENQUIRY IS ONE CONVERSATION — distinct session_ids with a customer turn, the
  same unit the Conversations tab counts, so enquiries -> booked reads as a
  conversion rate. Counting customer TURNS would grow with chattiness, not demand.
  NO-SHOW RATE IS OF DRIVES WITH AN OUTCOME (noshow / (completed + noshow)), so an
  unmarked booking cannot dilute it: the rate then moves with customer behaviour
  rather than with an operator's paperwork. Null until one drive is marked.
  ⚠️ `handoffs` IS A TRUE ZERO, NOT A GAP — and only because NOTHING in this
  system records an escalation: rule 13's callback stub is prompt-only and
  AuditType.handoff belongs to the WhatsApp link handoff. So no enquiry has ever
  been handed to a human, `resolved_pct` is 100, and `reasons` is EMPTY because
  there is no breakdown to show. Inventing one would be the fake dashboard data
  rule 16 forbids. When an escalation tool ships these fields move on their own,
  with no change to the endpoint or the page.
  `change_pct` / `noshow_rate_previous_pct` ARE NULL WHEN THERE IS NOTHING TO
  COMPARE — all-time (nothing precedes everything) or an empty previous window.
  "Up ∞%" from a base of zero is not a fact; the UI says so in words instead.
  A NULL COORDINATE DROPS THE PIN rather than defaulting to (0,0), which is open
  ocean — a pin in the wrong place is a false claim about where Elite is
  (FR-CX-07); a missing pin is merely incomplete. Doorstep bookings have no
  branch and so no pin, but DO rank in `top_places` (FR-SL-07) — which is why the
  map and the list beside it can legitimately differ.

GET /api/console/metrics                      # live dashboard (real data only)
  returns:  { "conversations": n, "turns_by_lang": {"en": n, "ar": n},
              "bookings": {"confirmed": n, "rescheduled": n, "cancelled": n},
              "notifications_sent": n,
              "agent_registry": [ { "agent": str, "status": "live|planned" } ] }

GET /api/console/notifications                # WhatsApp simulator feed
  returns:  [ { "ref": str, "kind": "confirmation|t24|t2|reschedule|cancellation|noshow",
                "lang": "en|ar", "status": "scheduled|sent|cancelled",
                "send_at": iso, "body": str } ]
```

Frontend rules for this contract: consume the SSE stream with `fetch` +
ReadableStream; render `token` events live into the last agent bubble; wire a
stop button to `AbortController` (AD-E); detect Arabic per message
(`/[\u0600-\u06FF]/`) and set `dir="rtl"` per bubble; start the on-screen booking
timer on the first user message and freeze it when the `booking` event arrives
(the under-3-minutes AHT proof point); the audit drawer reads
`/api/console/audit` for the current session.

Voice notes (2026-07-21) add one rule and change none of the above. Capture lives
in `src/useVoice.ts` and knows nothing about chat — it hands back a blob, and
`useChat.sendVoice` does upload → transcribe → ordinary `send`. TAP TO START, TAP
TO STOP (not hold-to-talk: a long-press fights mobile Safari's own gestures, and a
finger slipping mid-sentence would send a truncated note on a stranger's phone),
60 s cap mirrored server-side, format negotiated via
`MediaRecorder.isTypeSupported` and never transcoded. The player renders ABOVE the
transcript INSIDE THE SAME BUBBLE — one turn, two views of it — and lazily, with
no `src` until first tap, so rebuilding a thread does not fetch every recording.
**SEND is always visible; the MIC hides once the field has text** (product
decision 2026-07-23, narrowing an earlier both-always-visible rule). Someone
mid-sentence has already chosen their modality, so the mic is dead weight beside
the words they are typing and crowds the control they actually want on a phone.
This is HALF of WhatsApp's swap-in-place, and deliberately the half the original
rule was not defending: that rule protected SEND, because a PRIMARY control
vanishing on an empty field is disorienting for a presenter who did not expect
it. Send therefore never moves — only the mic, the secondary control, steps
aside, and only while there is something to send. Keyed on the trimmed draft, so
whitespace alone does not hide it. Voice is still additive; nothing that existed
before it moved.
**`getUserMedia` REQUIRES HTTPS** (blocked on plain HTTP except `localhost`), so
`useVoice` reports `unsupported` and the UI states the reason on screen rather
than showing a dead button — see the demo-day blocker in the scope override.

## Docker (AD-G)

- `elite-group-backend/Dockerfile` — python:3.11-slim, non-root user, uvicorn
  entrypoint with `--timeout-keep-alive 75` (AD-D).
- `elite-group-frontend/Dockerfile` — multi-stage: node build → nginx serving
  `dist/`, with `/api` proxied to the backend service.
- `docker-compose.yml` at `Elite-group/` root: services `backend`, `frontend`,
  `postgres` (with volume); backend gets `DATABASE_URL` pointing at the postgres
  service and `ANTHROPIC_API_KEY` from the root `.env` (never baked into images).
- Local non-Docker dev (uvicorn + `npm run dev` with SQLite) must keep working —
  Docker is additive, not a replacement.

## Observability — LangSmith tracing (2026-07-24)

Agent tracing on the **deployed VM only**: one trace per chat turn, named
`chat-turn`, containing the graph's `agent`/`tools` nodes, the routing edges, and
the model call inside each generation. Config is `LANGSMITH_TRACING` /
`_API_KEY` / `_PROJECT` (project `elite_group_test_drive_demo`), read from the
**root `.env`**, which `deploy/deploy.sh` ships to the VM.

**Two plumbing facts, both of which fail SILENTLY when missed.** The
compose `backend.environment:` block is an explicit **allowlist**, not
`env_file`, so a `LANGSMITH_*` value in the root `.env` reaches the container
only if it is NAMED there — otherwise tracing is configured, believed, and dead.
And the vars are deliberately **absent from `elite-group-backend/.env`**, since
that is the file pytest loads: langchain-core instruments LangGraph
automatically, so a scripted graph test would export a trace of a fake
conversation to a live US endpoint — both a vendor call from the offline suite
(forbidden) and noise in the project you read on demo morning. `conftest.py`'s
autouse `_hermetic_demo_toggles` forces it off regardless, clearing langsmith's
`get_env_var` lru_cache and deleting the key, so filing discipline is not the
only thing standing between the suite and the network.

**The graph traces itself; THE MODEL CALL DOES NOT.** `_default_generate` is the
raw Anthropic SDK per AD-A — not a Runnable — so without help the trace shows
the node and a HOLE where the generation belongs, taking the prompt, tool
schemas, stop reason, token counts and cache hit rate with it.
`graph._traced()` wraps the client with `langsmith.wrappers.wrap_anthropic` on
both provider paths. Checked rather than assumed: the wrapper's sync stream
object iterates the RAW event stream (which `_default_generate` consumes on
purpose, to see `content_block_start` the instant a `tool_use` begins) and
passes `get_final_message()` through, so AD-A streaming, the per-delta cancel
poll (AD-E) and the markdown stripper are untouched. It returns the client
UNWRAPPED when tracing is off or wrapping fails — telemetry must never break a
live turn, the rule `_log_usage` already follows.

**`build_config` states `metadata: {session_id}` because it will NOT arrive on
its own.** Every turn builds a fresh graph and checkpointer, so every turn is
its own ROOT run and a six-turn booking reads as six unrelated traces unless
grouped; LangSmith groups on `session_id` in the root run's metadata, and
langchain-core 1.x narrowed `ensure_config`'s configurable→metadata copy to
`model`/`checkpoint_ns` only (older guidance assumes it copies everything).
Metadata is SERIALISED AND SENT and `configurable` is not, which is exactly why
`db` and `cancel_event` stay in the latter — the trusted-context rule is
unaffected.

**⚠️ A FOURTH D1 PDPL ENTRY, AND THE HEAVIEST.** A trace carries the WHOLE
conversation — customer name, mobile, doorstep address, booking ref, both sides
of every turn — to a US endpoint. The STT, TTS and WhatsApp entries each send
one utterance or one message; this sends the entire session, structured and
searchable. And **rule 12 cannot reach it**: `services/erasure.py` clears the
customer, bookings, notifications, transcripts, audit rows and the LangGraph
checkpoint, and has no way to delete a third-party trace — so a customer told
"we've cleared everything" would still have their conversation in a trace store.
Demo attendees type real mobile numbers that really receive WhatsApp, so
"fictitious seed data" does not cover it.

**Decision 2026-07-24 (option A): full-text traces DURING BUILD, redacted BEFORE
the QR goes on screen.** The switch is `LANGSMITH_HIDE_INPUTS=true` +
`LANGSMITH_HIDE_OUTPUTS=true`, sitting commented in the root `.env` — read by
the SDK, no code. On, you keep every trace, every step, the tool order, timings,
tokens, cost and errors, and lose only the sentences. **Deferred, not waived:
uncomment both before demo day.** To switch tracing off entirely,
`LANGSMITH_TRACING=false` and redeploy; the wrapper and the metadata go inert.

## Data model (backend)

**The repo is the truth.** This section is a map, not a spec — `models.py` is
authoritative. If they disagree, the code wins and this section gets fixed.

Customer (title, name EN/AR, +971 mobile unique, language_pref, consent flags +
timestamps, winback_flag) · VehicleModel (brand, name, model_code, body_type,
seats_min, seats_max, price_aed_min, price_aed_max — **both nullable, null means
"not published"**, fuel_type petrol|phev, headline_features[], showroom_ids[]) ·
Showroom (brand, name, city, emirate, address, **address_ar** — a street address
is one of the few genuinely bilingual STRINGS in the schema, since unlike price
or seats it cannot be composed at speaking time, map_link, **latitude/longitude
— nullable pin coordinates for the console dashboard's branch map, STORED rather
than derived because `map_link` holds a Google Maps SEARCH URL with no
coordinates in it; a null drops the pin rather than defaulting to (0,0), and the
seeded values are APPROXIMATE DISTRICT POSITIONS pending Elite sign-off**, phone,
hours_by_day) ·
Slot (location_type showroom|doorstep, showroom_id|service_area, vehicle_id,
start, end, status open|locked|booked, locked_at, locked_by_session) · Booking
(ref TD-YYYY-NNNN, customer, driver name/relationship, vehicle, slot, location,
doorstep_address, companions, child_seat, consultant_pref, status, reason_code,
timestamps; **partial unique index on slot_id WHERE status IN
('confirmed','rescheduled')**; **attendance pending|completed|noshow — the
SECOND axis, set by a human in the console and by no tool: `status` says whether
the booking is alive, `attendance` says whether the drive happened, and neither
derives from the other. A cancelled booking stays `pending` and the service
refuses to change it. `noshow_notified_at`** = when the no-show follow-up was
written, which is what makes that message fire exactly once per booking) ·
MemoryProfile (**one row per customer,
consent-gated on READ and WRITE**; preferred_models[] = **vehicle_ids, never
names**, preferred_showroom_id **FK**, preferred_daypart, notes. **No
consultant_pref** — that lives on Booking, per visit: rule 8 says a consultant
preference is honoured when asked and never volunteered, so a structured column
the agent could filter or offer on is the presumption that rule forbids. Soft
preferences go in `notes`) · Waitlist
(customer, vehicle, location, desired range, daypart, status) ·
ConversationTurn (session_id, speaker, lang, text in native script, ts, **cards =
the vehicle-card ids AND booking refs this turn rendered, for reload — a union of
{vehicle_ids, variant} and {booking_refs}, pointers, never a frozen copy of the
card**, **audio_id** = the voice note this turn was spoken as, null when typed
and always null on agent turns) ·
VoiceNote (**customer voice notes AND call utterances (2026-07-27) — the
customer's own audio, INPUT ONLY** — audio_id uuid public handle,
session_id, customer_id **nullable and backfilled at identification**, mime,
size_bytes, duration_ms, **audio = the bytes, in a DB column**, transcript, lang,
stt_model, created_at, **expires_at**. Unlike vehicle imagery this is deliberately
NOT on the `/media` static mount: that is shipped fixture data, identical for
everyone and never deleted, whereas this is personal data with a 48h expiry and a
rule 12 hard-delete obligation. In a column, erasure is transactional and lives or
dies with its audit row; on disk it would depend on a filesystem that can fail to
unlink, and would be readable by anyone who guessed a path. The transcript is
stored **twice on purpose** — the ConversationTurn is the conversation, this is
the evidence next to the audio it came from, and a mis-transcription stays
investigable) ·
SpeechClip (**read-aloud cache** — cache_key = sha256 of spoken text + lang +
voice + model, session_id, turn_id, audio bytes, expires_at. **Scoped to a
session, not global**: a clip is Aisha quoting the customer's own name and mobile
back at them, and a globally shared row containing a phone number belongs to
nobody, which makes retention and rule 12 meaningless for it. Costs real cache
hits, buys trivial deletion. **No FK to ConversationTurn** — the clip is derived
and disposable, the turn is the transcript and must outlive it) ·
AuditEvent (**append-only**) · Notification (ref, kind, lang, **payload JSON =
the facts**, body = rendered customer-facing text, empty until the template
phase, status, send_at) ·
HandoffToken (**WhatsApp→web handoff, 2026-07-22** — token = unguessable
secret.token_urlsafe and the lookup key, NOT the pk; wamid **UNIQUE** so a retried
webhook mints one token; customer_id **nullable** — an unknown inbound number
still gets a link binding no one; booking_ref, intent, lang, expires_at,
**redeemed_at + redeemed_session** = single-use. Disposable and PII-free — a
customer_id and a ref, both already elsewhere; **SINGLE-USE + SHORT TTL are
security, not tidiness**: an identity-bearing link that was reusable or long-lived
would let a forwarded WhatsApp message open a session as that customer) ·
FeedbackSurvey (**post-drive survey over WhatsApp, 2026-07-26** — booking_ref
**FK to bookings.ref and UNIQUE** so even two operators clicking Completed cannot
open two; customer_id **denormalised on purpose** because every inbound reply
looks up by phone number, not by ref, and the alternative is a join per message;
status, **step** (0 = awaiting agreement, 1–4 = that question, 5 = the open
comment) as a plain integer since the machine only ever adds one to it; lang
resolved ONCE at open so the thread stays in one language; q1_overall / q2_ease /
q3_vehicle / q4_team **nullable — null means NOT ANSWERED, never zero**; comment
**free prose, the one field here that can hold anything, and the reason this table
is in the rule 12 sweep**; later_count; created_at / sent_at / closed_at /
updated_at. **THIS TABLE'S TIMESTAMPS USE A PYTHON-SIDE `default=datetime.now`
INSTEAD OF `server_default=func.now()`, AND HAVE NO `onupdate` — a deliberate
deviation from every other table here, so do NOT tidy it back.** `func.now()` is
the DATABASE's clock (UTC on SQLite and Postgres) while every datetime this
service computes is naive LOCAL, and worse, a SQL-expression `onupdate` OVERRIDES
an explicitly assigned value — so each `survey.updated_at = now` was being
silently replaced by UTC and the idle-expiry was off by the whole UTC offset
(measured: 24h meant 19h). Verified, not assumed. Without `onupdate` this column
has exactly one writer on one clock) ·
**`Booking.feedback_requested_at`** (stamped when the survey OPENS — the
once-per-booking guard, the same shape as `noshow_notified_at`).

**No display text in data fields.** This bug has appeared four times (price,
hours, seats/fuel, showroom city). Structured data drives logic; the agent
phrases it, per language, at speaking time. A stored English string cannot be
spoken in an Arabic conversation (LOC-01).

Seed reality: customers, vehicle_models, showrooms, and memory_profiles are
seeded from `seed/*.json` fixtures. Slots are **generated**, not seeded
(`python -m app.cli generate-slots --days 14 --prebooked 0.18 [--reset]`) — the
rolling window goes stale, so regenerate on demo morning and between test runs.
The demo booking is created by `python -m app.cli create-demo-booking`, which
calls the booking service through the real write guards — never an INSERT, so
broken guards fail at your desk instead of on stage.

**Demo morning is ONE command:**

```
python -m app.cli reset-demo
```

It does the whole ritual in order — drop/recreate all tables, re-seed
customers/vehicles/showrooms from `seed/*.json`, seed the memory profiles,
regenerate the rolling slot window, create the demo booking through the real
write guards, and **clear the agent checkpoint store**. The individual commands
remain for targeted work, but run the ritual by hand and the checkpoint step is
the one that gets forgotten.

**It ends by printing what Aisha will actually recall about the demo customer.**
Rehearse S-07 against that, not against `seed/memory_profiles.json`: Ravi's
booking derives his model/showroom/daypart from the slot it really booked, so the
fixture deliberately seeds him **`notes` only** and lets the booking own the rest
— the two writers converge because only one writes each column. The demo car is
pinned (`cli.DEMO_VEHICLE_CODE`) so the result is repeatable, but the daypart
still follows whatever slot is ≥25h out.

**Why the checkpoint step matters (this bites on stage).** The LangGraph
checkpointer is its **own sqlite file** (`AGENT_CHECKPOINT_PATH`, default
`./agent_checkpoints.db` plus its `-wal`/`-shm` siblings), separate from the
business DB. Reset the app data alone and every session's `tool_use`/
`tool_result` history survives — so a browser still holding that `session_id` in
localStorage rehydrates an **empty** visible thread while the agent silently
remembers the whole prior conversation. That reads as the agent inventing
context from nowhere, which is exactly what rule 2 exists to prevent. `reset-demo`
clears both together (belt); the frontend rotates any `session_id` that returns
zero turns (braces).

No Alembic: the DB is disposable. Drop and re-seed on schema change. Migrations
are a production-phase concern (note it in D7).

**THE DEMO IS JETOUR-ONLY.** The shipped seed contains nine Jetour models and six
Jetour showrooms — no SOUEAST, no The Elite Cars. Those two brands are a
**production-roadmap item, not demo data**: Elite Group represents them in
reality, but no vehicle or showroom exists for either in `seed/*.json`, so
nothing — prompt, script, storyboard or test — may promise a customer a car or a
branch from them. An earlier version of this section specified a three-brand
catalogue and cost a real grounding defect: the agent was instructed to
"recommend across all three brands" when two of them had zero inventory, i.e.
instructed to invent (FR-CX-07). Align to the data; the data is the truth.

**THERE IS NO HYBRID 7-SEATER.** Worth stating plainly because it is the gap most
likely to be re-assumed: the catalogue's PHEVs are the G700 (5–6 seats) and the
T2/T1 i-DMs (5 seats), and its 7-seaters (X70 Plus 5–7, X70 FL 7–7) are both
petrol. Any future script beat needing "a hybrid seven-seater" has no car behind
it and will fail on stage.

Actual shipped seed: **9 Jetour models** — G700, T2, T2 i-DM, T1, T1 i-DM,
Dashing, X70 Plus, X70 FL, X50 — incl. two 7-seaters (X70 Plus, X70 FL, both
petrol) and three PHEVs (G700, T2 i-DM, T1 i-DM) · **5 Jetour showrooms** (Al
Quoz, Deira, Abu Dhabi Rabdan, Al Ain, Ajman) · 14 rolling days of
evening-weighted slots with ~15–20% pre-booked (forces realistic "slot taken"
conversations) · doorstep fleet calendar (Dubai, evenings) · **5 customers**
incl. **Ravi Sharma (+971505550103, Sharjah, consented, winback flagged)** as the
returning-customer case · **3 memory profiles** — customers 1, 3 and 4, i.e.
exactly the three who consented; the other two deliberately have none, so the
"we remember nothing about you" path has data behind it too · one pre-seeded
booking created by `create-demo-booking` for reschedule/cancel testing. All
personal data fictitious — never real customer data.

**A sixth showroom was REMOVED on 2026-07-23: "Jetour G700 Al Quoz" (id 2), a
G700-dedicated branch sharing an address with Jetour Al Quoz.** Two Al Quoz
branches answering one question read as a duplicate on screen rather than as a
choice. **Ids were NOT renumbered** — the seed runs 1, 3, 4, 5, 6 and the gap is
deliberate: `Slot.showroom_id`, `VehicleModel.showroom_ids` and
`MemoryProfile.preferred_showroom_id` all point at these by number, so
resequencing to close a gap would silently move every one of them.

It leaves two things worth knowing, both flagged where they live rather than
only here:

- **`showroom_ids` is now inert in the seeded data.** That branch was the ONLY
  model-restricted one; every remaining model lists every remaining showroom, so
  `get_showrooms(vehicle_id=…)` can no longer filter anything out. The filter
  stays — the constraint is real in the data model and re-deriving it later
  would be worse than keeping a currently-unexercised one — but do not read a
  passing demo as evidence it works.
- **The G700's doorstep exclusion lost its stated reason.** `slots.py` keeps the
  flagship out of the doorstep fleet, justified by it being "the only model with
  a dedicated showroom". That showroom is gone, so the exclusion now rests on
  "it is the flagship" alone. Deliberately NOT reversed in the same change:
  removing it would ADD doorstep slots for the G700, which is a product decision
  and demo-visible, not a tidy-up. Still to confirm with Elite.

**Customer memory (FR-MEM, rule 12) — shipped.** `services/memory.py` owns it,
and the gate is on **read as well as write**: gating only the writers would leave
a stale row readable after a failed delete or a seed drift.

**TWO WRITERS, DISJOINT COLUMNS, so there is no precedence rule.**
`derive_from_booking` owns `preferred_models` / `preferred_showroom_id` /
`preferred_daypart` and is a **side effect of `booking.create`/`reschedule`**, on
the same argument notifications use — a write the model must remember can be
forgotten or double-fired. `save_memory` owns `notes` only. Neither touches the
other's columns, so an assertion (the model's paraphrase) can never overwrite a
fact (what they actually booked). **Cancel deliberately does not learn and cannot
retract** — a current-state row has no prior value to roll back to, and that
signal already lives in `winback_flag`.

**Recall has no tool of its own, deliberately.** A consented, non-empty profile
arrives as a `memory` **sibling key** on `find_or_create_customer`'s result
(never inside `customer` — the two have different gates), and the key is
**omitted entirely** when there is nothing to recall. The agent therefore sees
memory once, at identification, and has no way to go fishing — which is what
makes rule 12's "never volunteered to show off" a capability it lacks rather
than a discipline the prompt has to hold.

**"Forget me" is a two-call handshake** (`forget_me`): the unconfirmed call
*structurally cannot* delete and returns `would_delete` — the full scope,
**including each live booking with a spoken date** — so Aisha can say what goes
and ask in her own words; only `confirmed=true` runs the **total erasure**
described in rule 12 (`services/erasure.py`), which deletes the customer row
itself and therefore replaces "put the consent flag back down" with something
stronger: there is no row left to hold a flag, and no customer for the next
booking to re-derive a profile onto. **A confirmed call is idempotent** — once
the row is gone, "no such customer" and "already erased" are indistinguishable,
so a retry reports done rather than an error Aisha would apologise for (the
unconfirmed call still errors on an unknown id: there it is a lookup, and a dead
end is a dead end). Withdrawing memory consent deletes the profile ONLY, with no
confirm step (a withdrawal is already a decision; asking twice is arguing) — it
is a narrower request and must never be silently upgraded into an erasure.

**Known limit, flagged for the D1 PDPL checklist:** `notes` is free prose in a
schema that otherwise forbids display text, so a model *could* write a stale
English string into it. Capped and framed as guidance the agent rephrases, but
not structurally preventable.

## Acceptance (test against these)

Chat-scoped scenario catalogue: S-01 model-direct EN happy path · S-03 discovery
→ recommendation → booking · S-04 no-match honesty · S-05 slot taken →
alternatives · S-06 booking for a family member · S-07 returning customer with
consent · S-08 forget-me visible in audit · S-09 reschedule via reminder action ·
S-11 repeated reschedules, no context loss · S-12 cancel → retention accepted →
reschedule · S-13 cancel → offer declined → reason + win-back flag · S-16
out-of-scope → honest fallback · S-19 mid-conversation EN↔AR switch · S-20
adversarial inputs → graceful, honest, no loops, no invention · S-22 Friday
prayer window verifiably absent · S-23 doorstep end-to-end · S-24 personalisation
incl. instant female-consultant handling.

Quality bar: full chat booking greeting→confirmation in **< 3 minutes**; chat
reply starts **≤ 2 s**; **10 consecutive conversations** without restart;
≥10 adversarial inputs in both languages handled gracefully.

## Working conventions for Claude Code

- **Ask before acting — always.** Do not assume anything, however small. Before
  making any change that was not explicitly requested (a dependency, a folder
  name, a field type, a default value, a refactor, a "while I'm here" fix),
  stop and ask the developer first, presenting the options. If an instruction
  is ambiguous or underspecified, ask a clarifying question instead of picking
  an interpretation. Never expand scope beyond the exact task given.
- **NEVER call a live vendor API — not from tests, not from ad-hoc commands.**
  The test suite is deterministic and offline BY CONSTRUCTION, and it must stay
  that way: agent/graph tests inject a `ScriptedModel` through
  `build_config(..., generate=…)` so the agent node never reaches Anthropic; the
  STT and TTS tests monkeypatch the OpenAI client; nothing hits Anthropic,
  OpenAI, or Azure Foundry. The `@LIVE`/`skipif(AZURE_FOUNDRY_API_KEY)` tier that
  used to make real model calls was **removed on 2026-07-24** — do NOT reintroduce
  it, and do not add any test, fixture, or helper that instantiates a real
  `Anthropic()`/`OpenAI()` client or calls `client.messages.stream`/`create`
  against a live endpoint. If a behaviour genuinely can only be proven against the
  real model, ASK the developer first; never run it on your own.
  Clearing shell env vars is NOT a safe guard: `app/config.py` calls
  `load_dotenv()`, so a key in `.env` is picked up even when the shell variable is
  empty. The protection is *not writing code that calls a vendor*, never "I unset
  the key." Run tests file-by-file (the specific DB-only/scripted files you
  changed), never a blanket `pytest` that could sweep in a live path.
- **Seed fixtures are shipped data, not scratch.** `seed/*.json` for customers,
  vehicles, and showrooms are reviewed and verified — amend them deliberately,
  never regenerate them casually. Slots and the demo booking are produced by CLI
  commands, never by hand.
- Both repos live under `Elite-group/` with this single CLAUDE.md at the parent
  root — Claude Code reads it from the parent directory when launched inside
  either repo. There is one copy; never duplicate it into the repos.
  Change the API contract section in **both** repos in the same change, or not
  at all.
- Requirement IDs (FR-BK-09, LOC-06, S-22 …) are stable — reference them in
  commit messages and comments so work stays traceable to the BRD.
- **Never add a `Co-Authored-By:` trailer to a commit** — no Claude/AI
  attribution, and no "Generated with" line in a commit message or PR body.
  Commits are authored by the developer, full stop. This overrides any default
  or tooling convention that adds one.
- Prefer boring, testable code over cleverness: the demo must survive a live
  audience trying to break it. Every dead-end needs a graceful, honest reply.
- Calendar/business rules belong in the data/service layer, never in the prompt.
  The prompt shapes tone and flow; the tools own the facts.
