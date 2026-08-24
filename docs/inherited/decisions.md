# Product & developer decision log

> **Spoke of `../CLAUDE.md`.** These are the six dated override blocks that used
> to sit inside the root Scope section, moved here on 2026-08-05. **Every word is
> unchanged** — they are reproduced verbatim, in the order they were written.
>
> They are a LOG, not a spec. Read chronologically, later blocks reverse parts of
> earlier ones. **The table below is the key to which parts** — it was written
> during the move and is the only new text in this file. Where a row says a
> statement is superseded, the earlier block still contains the original sentence,
> deliberately: the reasoning that produced a reversed decision is often the
> reason the reversal is right, and deleting it would lose that.

## What supersedes what

| An earlier block says | Current state | Where the change is |
|---|---|---|
| The call is **English-only** (2026-07-27) | The call is **bilingual**, language deterministic | Rebuild §6 (2026-07-31) |
| Language is re-detected per transcript (rebuild §6) | **One language per call**, locked once and never changed. **Chat is untouched** — rule 3's per-turn switching stays | Rebuild §6a (2026-08-03) |
| Call utterances persist as `VoiceNote` rows (2026-07-27) | The call is **ephemeral** — nothing stored mid-call, one `Speaker.system` marker turn at hangup | Rebuild §2 (2026-07-29) |
| The **pipeline** engine (our STT → `run_turn()` → our TTS) is the call (2026-07-27) | **`CALL_MODE=realtime`** is the active mode — GPT speaks, **the tools stay ours**. Pipeline is one config line away and intact | Rebuild §1 (2026-07-27, model upgraded 2026-07-30) |
| Vendor **server VAD** ends the turn (2026-07-27) | **Words decide turns, not sounds** — the vendor VAD is demoted to a segmenter; the gateway creates every reply at transcript time | Rebuild §5 (2026-07-30) |
| Call audio rides **WS-PCM** (2026-07-27) | Audio travels over **WebRTC** (AD-M); WS-PCM is the automatic fallback and still carries every JSON event | Rebuild §3 (2026-07-31) |
| "host candidates suffice for … a public-IP VM" (rebuild §3) | **FALSE, corrected by measurement** — 13 negotiations, 13 failures. Host candidates suffice for **localhost only**; a **TURN relay** is required, and `ice_servers` must reach the browser | Struck through in rebuild §3 + the `ice_servers` note in the API contract (2026-08-04) |
| Read-aloud uses the **on-device** browser voice (read-aloud block, as shipped) | **Server-generated** `gpt-4o-mini-tts`; the browser engine is **demoted, not deleted**, as the fallback | Same block, changed 2026-07-21 after listening |
| Aisha has **two voices** and switches on S-19 (read-aloud) | True for read-aloud. **Does not exist in realtime call mode** — one voice (`marin`) speaks both | Rebuild §6 (2026-07-31) |
| Inbound WhatsApp: **"no second Aisha exists"**, handoff-only | **Narrowed** — a feedback-survey reply is consumed and answered on that channel. Still **no model ever runs there** | Feedback-survey block (2026-07-26) |
| A denied/unsupported mic **states the reason on screen** | The hint was **REMOVED**; the mic button is now silently absent. The HTTPS blocker itself is unchanged | v10 (2026-08-03) |

**⚠️ TO CONFIRM, not asserted here:** the voice-call blocks below state the work
is on the `voice-call` branches and **NOT deployed**. That was true when written.
Verify against the branches and the VMs before relying on either statement — this
table records what the blocks say, and does not independently establish that it
is still true today.

---

<!-- Everything below this line is verbatim from the original CLAUDE.md. -->

## 1. Voice notes (input only) — 2026-07-21

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
> **⚠️ GO-LIVE BLOCKER, unresolved: `getUserMedia` requires HTTPS.** It is
> blocked on plain HTTP everywhere except `localhost`. Local dev is therefore
> fine, but **phone users reaching the app over `http://<ip>:5173` will find
> the mic button dead on every phone** — no code change fixes this. Cheapest
> fix is a tunnel (`cloudflared tunnel --url http://localhost:5173`). Deferred
> by the developer 2026-07-21 with HTTP accepted "for now"; it must be closed
> before real users are let in.

## 2. Read-aloud (opt-in TTS) — 2026-07-21

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
> listening: the browser's voices are the DEVICE'S, and on the hardware tested
> the English ones were 1990s novelty voices (Boing, Zarvox, Fred — macOS ships
> ~180, of which two are real) while Arabic had exactly one. Server synthesis is
> the only way to get the same warm Gulf register on every customer's phone.
> `gpt-4o-mini-tts` over `tts-1` because it takes a plain-English TONE
> INSTRUCTION — the one place rule 14's register is expressed to a model that is
> not Claude — and it was audibly warmer in BOTH languages at the same price
> (~$0.015/min; a 60-second reply costs about 1.4 cents, and the cache means a
> replay costs nothing).
>
> **THE BROWSER ENGINE IS DEMOTED, NOT DELETED, and that is deliberate.** A 404,
> a 503, an unset key or a flat conference network falls back to
> `speechSynthesis` instead of a dead control. A vendor outage must cost QUALITY,
> never the feature — an icon that stops working mid-conversation is worse than
> one that sounds worse. `useSpeech.pickVoice` therefore still matters: it ranks by name
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
> language mid-conversation** — which is S-19, a scenario the product must
> handle. Each language gets its best voice at the cost of sounding like two
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
> of the booking flow. References, +971 numbers and clock times are rewritten for speech
> only; **the bubble's text is never altered**, so what is read and what is on
> screen stay the same message.

## 3. Post-drive feedback survey — 2026-07-26

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
> env file each VM was deployed from (`deploy/prod/.env` — and `deploy/dev/.env`
> for dev) before going live; the only symptom is in the audit trail.

## 4. Voice call — 2026-07-27

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
> origin, so the voice-note go-live blocker covers the call unchanged. The
> nginx config now upgrades WS on `/api/call/` and the Vite dev proxy has
> `ws: true`; local dev on localhost works out of the box.

## 5. The call rebuilt — 2026-07-29 → 2026-07-31

> ⚠️ **THE CALL WAS REBUILT 2026-07-29 → 2026-07-31** (developer decisions,
> each approved explicitly; still on the `voice-call` branches, still NOT
> deployed). The block above documents the original pipeline call; five of
> its statements are now superseded:
>
> **1. THE REALTIME ENGINE IS THE ACTIVE MODE** (`CALL_MODE=realtime`,
> decision 2026-07-27, model `gpt-realtime` full — upgraded from mini
> 2026-07-30 after its conversational judgment produced the "robotic"
> complaints; ~$0.06–0.15/min). One speech-to-speech vendor session hears and
> speaks; THE TOOLS REMAIN OURS — every function call runs through the same
> dispatch choke point (`app/agent/tool_node._dispatch`), same write guards,
> same audit rows, `correlation_id` = the vendor call_id. The honest cost:
> conversational judgment is GPT's (rule 2 by compliance, not by Claude's
> discipline), and call turns do NOT enter the Claude checkpoint (split
> memory — the session is seeded with the trailing typed thread instead).
> The pipeline mode remains intact; one config line flips back. Personas:
> `CALL_PERSONA=natural` (voice-native brief, `app/agent/call_persona.py`,
> voice `marin`) vs `classic` (the compact `realtime_instructions()`), A/B
> by env.
>
> **2. THE CALL IS EPHEMERAL** (2026-07-29, reversing per-turn persistence
> above). Nothing is stored mid-call: no VoiceNote, no per-turn rows, no
> audio of either voice. Hangup writes exactly ONE `Speaker.system` marker
> turn — `call:<seconds>`, with the booking refs the call produced as an
> ordinary `cards` anchor — which the client renders as a muted "Voice call —
> 2:14" line plus the live booking card. PDPL gets lighter (nothing for the
> 48h expiry or rule 12 to chase in this mode); the bookings stay real (tools
> and audit untouched); what was SAID is nowhere, like a phone call. The
> `Speaker` enum gained `system` for this; read-aloud refuses such turns.
>
> **3. AUDIO TRAVELS OVER WEBRTC (AD-M, 2026-07-31), WS-PCM IS THE
> FALLBACK.** aiortc peer connection per call (`services/rtc_transport.py`),
> negotiated over the existing WS (`rtc_offer`/`rtc_answer`; refusal =
> `rtc_unavailable`, mid-call death = `rtc_failed` and the WS-PCM path
> resumes). The point: Aisha becomes a real remote call party, so the
> browser's NATIVE AEC3 echo cancellation engages (the AEC-loopback trick
> and RMS echo gates become fallback-only), Opus cuts bandwidth ~10x, and
> playback is SERVER-side — which is what makes interruption honest
> (pause/clear within one 20 ms frame). ICE is non-trickle. ~~host candidates
> suffice for localhost and a public-IP VM (`RTC_STUN_URL` for NAT)~~ —
> **FALSE, corrected 2026-08-04 by measurement: 13 negotiations on the dev VM,
> 13 failures, never once `connected`.** Host candidates suffice for
> LOCALHOST ONLY. Deployed, the backend sits on the compose bridge and can
> offer nothing but `172.18.0.x` behind Docker's masquerade AND Azure's NAT,
> while the browser (`new RTCPeerConnection()` with no `iceServers`) offers
> only its own LAN address — two private addresses cannot pair, so every
> deployed call fell back to WS-PCM. **That fallback is the echo bug**: with no
> native AEC3 Aisha's voice reaches the mic, gets transcribed as the customer
> (observed as Turkish, Chinese and Arabic nonsense), triggers false barge-ins
> that cut her off mid-word, and draws replies to her own speech — the
> "doubling and fading" reported live. STUN alone does not fix it (a pinhole
> through two endpoint-dependent NATs, varying by the customer's own network);
> **a TURN RELAY does**, because both sides connect OUTBOUND to it.
> `RTC_TURN_URL`/`_USERNAME`/`_CREDENTIAL` configure it, a `coturn` service
> runs it (dev overlay only so far — `TURN_RELAY=1` in `deploy/dev/config.sh`
> gates the UDP NSG rules, since `provision-core.sh` is shared with prod), and
> **only the SERVER needs credentials**: our relay candidate is publicly
> reachable, so the browser needs no config and no code change. A missing,
> half-configured or unreachable relay costs call QUALITY, never the call —
> the WS-PCM fallback is untouched.
>
> **4. A SILERO VAD GATE SITS IN FRONT OF THE VENDOR**
> (`services/speech_gate.py`, model vendored at `app/assets/silero_vad.onnx`,
> onnxruntime on CPU, zero network — the suite runs REAL inference against
> local fixtures). Only detected speech is forwarded: echo residue, breaths
> and room noise never reach the vendor's VAD to misfire on, and silence is
> never billed. Server-side deliberately: transport-agnostic (the same gate
> will serve a future Twilio leg) and offline-testable. Fails open — a
> missing model forwards everything. Pre-roll preserves onsets; the hangover
> deliberately exceeds `CALL_VAD_SILENCE_MS` so the vendor still hears the
> silence that closes a turn.
>
> **5. WORDS DECIDE TURNS, NOT SOUNDS** (the turn engine, 2026-07-30 — after
> 48 replies died to `turn_detected` misfires in two calls). The vendor VAD
> is demoted to a segmenter: `interrupt_response: false` AND
> `create_response: false` — it may end the customer's turn and commit
> audio, nothing more. Every reply is created by the GATEWAY at TRANSCRIPT
> time: empty/noise transcripts resume her paused reply (no reply drawn);
> her own words back (echo past AEC, judged by word-overlap with her live
> reply text) are silently discarded; a backchannel over her speech ("yeah",
> "تمام") lets her keep talking; real words supersede — playback cleared,
> in-flight audio embargoed until the next reply starts, the old response
> cancelled BY NAME (`response.cancel` + `response_id`, so a racing cancel
> can never kill the successor), then our `response.create`. A reply is
> known from `response.created` (not first audio), so generation-phase
> interruptions cancel properly. A 2.5 s fallback answers from audio alone
> if the transcript never lands. `user_speech_started` is a HINT only: it
> pauses server playback (recoverable), never cancels.
>
> **6. THE CALL IS BILINGUAL (2026-07-31, reversing the English-only
> decision of 2026-07-27) AND LANGUAGE IS DETERMINISTIC, chat-style.** The
> model never judges language: the gateway detects the transcript's script
> (the chat's own Arabic-script rule) and pins EVERY response the session
> creates — turn replies, tool follow-ups (the path that shipped English
> answers to Arabic questions when left unpinned), rate-limit retries — via
> response-level instructions (persona + "LANGUAGE FOR THIS REPLY: … ONLY").
> On top, a LANGUAGE GUARD enforces: her reply streams to us as text, the
> first ~20 letters are majority-script-checked against the pin, and a
> mismatch is cancelled within its first breath and redone under the same
> pin (one retry, then play-with-warning). One voice (`marin`) speaks both
> languages — the S-19 voice-switch limitation does not exist in this mode.
> Arabic register on the call remains UNTESTED with native speakers — same
> caveat as everything Arabic-audio in this project.
>
> **6a. ONE LANGUAGE PER CALL (product decision 2026-08-03, superseding 6's
> per-transcript re-pinning; CHAT IS UNTOUCHED — rule 3's per-turn switching
> stays).** The call's language is decided ONCE and never changes: the seeded
> chat thread's language locks it at bootstrap (the language the chat was in
> when the customer pressed Call — `_seed_pin`, last judgeable customer
> turn); a fresh call greets in English and is locked by the first CLEAR
> utterance (`_lock_verdict`: ≥2 words AND letter-majority script — strict on
> purpose, so a one-word noise hallucination like "آه" can never commit a
> call to a language). After the lock, a mid-call switch is understood and
> answered in the call's language, without comment (the persona says so);
> the per-transcript verdict survives only in the `heard:` log line. The
> deterministic machinery of 6 is all retained — script detection, the
> per-response pin, the LANGUAGE GUARD — only the re-decision is gone, which
> also closes the last door on the noise-flip bug class (a garbled
> transcript could previously change what she speaks). `utterance`/`done`
> events keep per-text detection so an off-language transcript still renders
> RTL. Known cost, accepted: a caller who genuinely wants to change language
> mid-call must hang up and call again.
>
> **Interruption ownership, restated for this mode:** the vendor may never
> cancel Aisha (`interrupt_response: false`); only the gateway does —
> word-confirmed supersede, client barge_in, or the language guard. What she
> wrote is never taken back (text_break, extended to audio); only unheard
> audio dies.
>
> **Munsit (CNTXT AI) EVALUATED 2026-07-30, not integrated:** UAE Arabic
> STT/TTS vendor; voice "Ghalia" (Emirati, bilingual) confirmed via API and
> sampled (PCM16 24k — the call's native rate; batch ~3–4 s, streaming first
> chunk ~0.85 s). Candidate homes: read-aloud's Arabic side (risk-free), or
> the call via realtime-text-out + streamed TTS (+~1 s/reply). `MUNSIT_API_
> KEY` in the backend `.env`. Their Arabic STT is also the natural candidate
> for the documented Gulf-dialect voice-note gap.

## 6. v10 — Salesforce, nearest showroom, agent feedback — 2026-08-03

> ⚠️ **v10 — SALESFORCE INTEGRATION + NEAREST SHOWROOM + AGENT FEEDBACK,
> applied 2026-08-03.** Built by an external developer on an isolated CLONE of
> the dev VM (VM `ELITE-SF-VM`, decommission when no longer needed) and delivered as
> the `elite-handoff-v10` bundle; applied to the **`voice-call` branches** of
> both repos on 2026-08-03 (the bundle's baseline was exactly those branches'
> tips — backend `3bfc4d4`, frontend `fd9044f` — so every patch applied
> conflict-free and the result is byte-identical to the author's reference
> files). NOT committed at apply time, NOT deployed. Three features, every
> vendor touch **fail-open by construction**: any Salesforce or Google Maps
> problem silently falls back to local data — an outage can never break a
> conversation.
>
> **1. SALESFORCE READ-THROUGH + AVAILABILITY GATE** (`app/services/
> salesforce.py`, new). The app's own Postgres stays the runtime engine; the
> read paths CONSULT Salesforce so the CRM drives what the customer sees.
> `catalogue.get_details` / `detail_for_dialog` / `list_showrooms` try the
> matching `*_sf()` first and fall through to the local DB on None — the SF
> functions return the EXACT local dict shapes (author-verified byte-identical
> for all 9 vehicles + 5 showrooms), so cards and prose are unchanged.
> `search_vehicles` deliberately stays local (it is only a picker).
> `slots.query_available` gains a gate: after local candidates, before the
> `limit` slice, any slot Salesforce marks ≠ Available (Booked/Locked) is
> dropped; on ANY SF problem the gate is a no-op and the local result stands.
> Performance is pre-paid so no request (or live call turn) ever waits on
> Salesforce: OAuth client-credentials token cached in-process, HTTP
> keep-alive, a startup `prewarm()` + daemon refresher (both called from
> `app/main.py`'s lifespan, guarded and fail-open) keeping an in-memory
> booked-slot snapshot fresh every `SF_SNAPSHOT_SECONDS` (10) and the
> catalogue cache every `SF_CATALOGUE_REFRESH_SECONDS` (300). ⚠️ API budget:
> those defaults cost ≈9.3k SF API calls/day per backend process against the
> Developer Edition's 15k/day cap — raise the snapshot interval if the org is
> tight or workers multiply. Gated on `SF_ENABLED` + `SF_DOMAIN` +
> `SF_CLIENT_ID`; default OFF, so the app runs fully local without config.
> **The WRITE path is a sidecar, not app code** (`sf_sync.py`, deploy-side —
> parent dir, in NO repo): polls Postgres, upserts Contact (by mobile) +
> `Test_Drive__c` (by booking ref), flips `Slot__c` status, and reaps
> "forget me" erasures by deleting the SF records it previously synced.
> ⚠️ The reap's tracking is IN-MEMORY (author-flagged, NOT production-hardened): a sidecar
> restart between a booking and its erasure loses that one reap — the
> production fix is hooking `services/erasure.py` to delete in SF directly.
>
> **2. NEAREST SHOWROOM (Google Maps)** — `app/services/maps.py` (new;
> UAE-biased geocode + emirate parse, reverse-geocode for GPS, Distance
> Matrix driving times, Haversine fallback, key-less directions deep link)
> and `app/tools/location_tools.py` (new) registering **`nearest_showroom`**
> in the tool REGISTRY. Emirate-aware result logic lives in the TOOL, not the
> prompt: a bare city/emirate returns `ask_for_landmark` (never city-level
> distances, never "we have no branch in your city"); a specific landmark or
> shared GPS resolves to the nearest branch(es) with real driving
> distance/time and a directions URL. Branch coordinates come from Salesforce
> (`Showroom__c.Latitude__c/Longitude__c`) with the local `Showroom.latitude/
> longitude` console-map pins as fallback. Without `GOOGLE_MAPS_API_KEY` the
> geocoder returns None (the tool asks for a landmark or uses shared GPS) and
> distances degrade to straight-line; the directions link needs no key and
> always works. The prompt gains nearest-branch-first guidance plus a ONE
> QUESTION PER MESSAGE paragraph that RESTATES rule 4 — redundant with the
> 2026-07-28 hardening, not contradictory. ⚠️ Note: that hardening commit
> (`4e8e7c9`) currently sits on `main`, not `voice-call` — `prompt.py` now
> carries changes on BOTH sides of that divergence and will need hand
> reconciling when the branches merge.
>
> **3. AGENT FEEDBACK (on Aisha, at chat close)** — `POST /api/feedback`
> (new router), `AgentFeedback` model (`agent_feedback` table, created by
> `create_all` on startup), and a frontend `FeedbackCard` (5 stars + optional
> comment) rendered above the closed composer. DISTINCT from the post-drive
> WhatsApp `FeedbackSurvey`. The row SNAPSHOTS client_name / mobile /
> booking_ref at write time (resolved from the chat's booking, else from the
> audit trail's latest `find_or_create_customer` input) so the Salesforce
> sidecar can push a self-contained `Test_Drive_Agent_Feedback__c` record.
>
> **⚠️ NEW D1 PDPL ENTRIES, all three unresolved:** (a) the sidecar mirrors
> customer name, mobile and booking data INTO the Salesforce org, and rule 12
> erasure reaches SF only through the sidecar's restart-fragile in-memory
> reap; (b) a spoken landmark — and, from the location button, the customer's
> GPS COORDINATES — go to Google's endpoints, and the GPS fix is also written
> into the transcript as an ordinary customer turn, so it persists in
> `ConversationTurn`; (c) `POST /api/feedback` is UNGATED (any caller, any
> session_id) and snapshots name+mobile into a row that **`services/
> erasure.py` does NOT delete** — verified 2026-08-03: a confirmed "forget
> me" currently leaves `agent_feedback` rows holding the customer's name and
> mobile. The snapshot also deviates from the pointers-never-snapshots rule
> the booking card established. Flagged, deliberately NOT fixed as part of
> applying the bundle — fixing changes behaviour and is the developer's call.
>
> **Also in v10:** the composer's "Microphone unavailable" hint was REMOVED
> from `App.tsx` — this reverses the documented state-the-reason-on-screen
> behaviour (see the voice-notes frontend rules); the HTTPS `getUserMedia`
> blocker itself is unchanged. Bare URLs in Aisha's bubbles are now
> linkified, with a Google-Maps directions link rendered as a "🧭 Get
> directions" chip. The 13 one-time Salesforce org scripts live in
> `elite-group-backend/salesforce-setup/` (schema, tabs/app/layouts/list
> views, permission sets, seeding, coordinates, the Agent Feedback object);
> ⚠️ `seed_sf_slots.py` and `update_slot_names.py` are referenced by the
> author's runbook but MISSING from the bundle — superseded 2026-08-03 by
> **`salesforce-setup/reset_sf_demo.py`**, written in-house: reset-demo's
> Salesforce twin. Run it AFTER every `reset-demo` (`docker exec -i
> elite-group-backend-1 python - < salesforce-setup/reset_sf_demo.py`): it
> wipes the org's transactional data (slots, test drives, feedback, synced
> contacts) and reseeds everything from the LIVE DB — vehicles, showrooms
> (incl. enrichment + coordinates), a bounded slot window (`SF_SEED_DAYS`,
> default 3 — 7 days ≈ 1.9k slots ≈ 3.7 MB against the Developer Edition's
> ~5 MB storage cap, hence the small default) PLUS, always, every slot a
> live booking references, then re-pushes live bookings and unsynced
> feedback. Without this ritual the org's slot ids drift from the DB's after
> a reset-demo, and every booking push fails its first attempt with
> "Foreign key external ID not found" (seen live 2026-08-03: TD-2026-HTXE)
> while the availability gate reads phantom booked slots. The LOCAL compose
> stack now runs the full loop: the root docker-compose.yml carries the
> SF_*/GOOGLE_MAPS_API_KEY allowlist entries and the `sf-sync` sidecar
> (script at the parent root, in NO repo, like everything else there). Env additions (`SF_*`, `GOOGLE_MAPS_API_KEY`) must ALSO be
> named in each compose file's backend `environment` ALLOWLIST or they
> silently never reach the container — the same trap as `LANGSMITH_*` and
> `NOTIFY_SEND_KINDS`. Secrets stay in the server `.env` only; the dev-org
> credentials shared during the build are throwaway — ROTATE the SF consumer
> secret and the Google key before production.
