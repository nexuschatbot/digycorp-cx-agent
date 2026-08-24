# API contract (backend owns, frontend consumes)

> **Spoke of `../CLAUDE.md`.** This is the authoritative contract, moved out of
> the root file on 2026-08-05 so it is read when it matters rather than in every
> session. **Wording is unchanged.** Root carries a signature index only; if the
> two ever disagree, THIS FILE WINS and the index gets fixed.
>
> There is ONE copy, here at the parent root — the old instruction to "change the
> API contract section in both repos in the same change" described a duplication
> that does not exist. Neither repo holds a copy.

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
  effect of a key existing). ~~English-only~~ BILINGUAL as of 2026-07-31 (see
  the rebuild block in the scope section).
  ⚠️ PROTOCOL ADDITIONS, CALL_MODE=realtime + the 2026-07-29→31 rebuild (the
  per-utterance storage above is PIPELINE-mode; realtime is EPHEMERAL — one
  Speaker.system summary row, utterance events carry audio_id null, done
  carries turn_id null):
  Uplink adds:   {"type":"rtc_offer","sdp"}       # audio upgrade (AD-M); WS-PCM
                                                  #   carries the call until
                                                  #   rtc_active
                 {"type":"rtc_error","detail"}    # (2026-08-04) the browser
                                                  #   could not apply our
                                                  #   rtc_answer. LOG-ONLY, and
                                                  #   the call is unaffected —
                                                  #   WS-PCM was already
                                                  #   carrying it. It exists
                                                  #   because a bare .catch()
                                                  #   in the client used to
                                                  #   SWALLOW this: a browser
                                                  #   that cannot apply the
                                                  #   answer never starts ICE,
                                                  #   so the server saw its own
                                                  #   checks go out and nothing
                                                  #   come back — outwardly
                                                  #   identical to a network
                                                  #   dropping packets, which
                                                  #   is what we blamed three
                                                  #   times. Reported to the
                                                  #   SERVER, not the console:
                                                  #   nobody debugs a call by
                                                  #   asking the customer to
                                                  #   open devtools.
  Downlink adds: call_started gains "engine":"realtime|pipeline",
                   "rtc":bool (the upgrade invitation) and
                   "ice_servers":[{urls[,username,credential]}] (2026-08-04) —
                   THE BROWSER'S OWN ICE SERVERS, shaped for
                   RTCConfiguration.iceServers so the client passes them
                   straight through. Server-sent, not a frontend constant, so
                   the relay can move without a frontend rebuild. ALWAYS
                   PRESENT, [] when unconfigured (localhost, where both ends
                   are one machine and host candidates work) — so the client
                   never branches on undefined.
                   ⚠️ WITHOUT THIS THE CALL CANNOT NEGOTIATE OFF LOCALHOST,
                   however good the SERVER's candidate is — measured
                   2026-08-04, when a proven-working public relay candidate on
                   our side still never reached `connected`. The browser
                   otherwise offers only host candidates, which Chrome
                   publishes as mDNS `.local` names that aiortc cannot
                   resolve, leaving OUR side with no usable address for it;
                   and a TURN relay only forwards from peers it holds a
                   permission for, which a private LAN address cannot earn.
                   A STUN entry cures both: the browser discovers and offers
                   its own PUBLIC address. STUN binding is unauthenticated, so
                   nothing secret is sent to the client — and pointing it at
                   OUR coturn rather than a public Google server keeps
                   customer IPs off a third party (D1 PDPL).
                 {"type":"rtc_answer","sdp"} | {"type":"rtc_unavailable"}
                 {"type":"rtc_active"}            # audio has MOVED to the peer
                                                  #   connection; client stops
                                                  #   WS PCM, worklet stands down
                 {"type":"rtc_failed"}            # mid-call death — WS-PCM
                                                  #   resumes; quality, never
                                                  #   the call
                 {"type":"pcm_start","rate"} + binary PCM frames +
                   {"type":"pcm_end"}             # her voice on the WS fallback
                 {"type":"stop_audio"}            # kill playback NOW (supersede
                                                  #   / language-guard redo)
  call_ended reasons add: rate_limited            # org TPM saturated after
                                                  #   bounded retries — the
                                                  #   client says "line is busy"

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
            would outlive both the 48h retention and the rule 12
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
  WHY: ref-alone lookup is a deliberate temporary relaxation (NOT
  production-ready), already on the D1 PDPL checklist. It was tolerable while
  this payload carried no personal data; the booking card puts a name and
  mobile on screen, which would have made a guessable reference a way to read
  a stranger's phone number. The gate lives in the SERVICE layer, so the agent path inherits it
  too and no future screen can forget it.
  ⚠️ NARROWED IN PRACTICE 2026-08-05, though the gate itself is unchanged. On the
  AGENT journey a TD reference now IDENTIFIES its owner on its own
  (identify_by_reference_or_mobile — see rule 10), so a chat session holding one
  becomes that customer and does get the PII. This endpoint still resolves the
  viewer from the checkpoint and never from the query string, so an invented
  session_id is still nobody; what changed is how cheaply a real session can
  become somebody.

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
  ?status=confirmed MATCHES confirmed OR rescheduled (2026-07-29). The console
  DRAWS a rescheduled booking as "Confirmed": `status` answers "is this booking
  alive?", and a moved booking is exactly as alive as one never moved, so two
  words for one state made an operator hunt for a difference that carries no
  consequence for them. The filter had to widen WITH the badge — relabelling the
  pill alone made a card reading "Confirmed" vanish the moment you filtered for
  Confirmed, and a screen that contradicts itself that plainly is worse than the
  thing it was fixing.
  ?status=rescheduled STILL SELECTS ONLY MOVED BOOKINGS, so "which were moved"
  stays answerable; the Bookings tab simply no longer offers it as a filter.
  THE STORED STATUS IS UNTOUCHED and must stay `rescheduled` — it is load-bearing
  (the partial unique index on slot_id covers WHERE status IN
  ('confirmed','rescheduled'), reschedule and cancel both read it, and the
  dashboard funnel counts reschedules as their own stage, which is a real EVENT
  and a different question from "is it live?"). The row keeps reporting the true
  `status`; the word "Confirmed" is substituted in the UI at the moment of
  drawing, nowhere else. Guarded by tests/test_console_booking_status.py.

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
  rather than 422 — a typo in a URL should not be an error screen mid-session — and
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

POST /api/feedback                            # agent feedback — the DIRECT path (v10)
  body:     { "session_id": str, "stars": 1..5,
              "comment": str|null, "booking_ref": str|null }
  returns:  { "ok": true, "id": int }
  Feedback on AISHA (the chat), captured by the frontend FeedbackCard when the
  conversation closes — distinct from the post-drive WhatsApp survey. No LLM is
  involved. The row snapshots client name/mobile (resolved from the booking ref,
  else from the audit trail's latest find_or_create_customer input) so the
  Salesforce sidecar can mirror a self-contained Test_Drive_Agent_Feedback__c
  record. ⚠️ UNGATED (no session-ownership check) and the snapshot sits OUTSIDE
  the rule 12 erasure sweep — both on the new D1 entries in the v10 scope block.
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
than showing a dead button — see the go-live blocker in the scope override.
~~The on-screen hint~~ **REMOVED by v10 (2026-08-03):** the `voice-note-hint`
line no longer renders — a denied/unsupported mic now shows nothing. This
reverses the state-the-reason rule above (an external-developer change applied
with the bundle, not a lapse); the HTTPS blocker itself is unchanged, so on a
plain-HTTP deploy the mic button is now silently absent. Restore is a
three-line revert in `App.tsx` if the honest-hint behaviour is wanted back.

Nearest-showroom + agent feedback (v10, 2026-08-03) add three frontend rules.
A `nearest_showroom` tool_event renders RICH SELECTABLE CARDS (`variant:
'nearest'` on the showrooms row — distance · drive minutes · a Get-directions
link · a Select button); an unresolved result (ask_for_landmark) renders no
cards. This makes it the second tool_event with customer-facing UI after the
vehicle card (rule 18); like the get_showrooms pills it has commit semantics —
Select sends a normal turn and settles the row. A contextual **"📍 Use my
current location"** button appears under Aisha's LAST finished message when its
text asks where the customer is (regex `LOCATION_ASK_RE` in `App.tsx`); tapping
it sends the device GPS fix as an ordinary customer turn (denied/unavailable
geolocation falls back to the same question without coordinates). The
composer's earlier location-pin idea was dropped in favour of this contextual
button. On `closed`, the `FeedbackCard` (5 stars + optional comment) renders
above the "This chat has ended" composer and POSTs /api/feedback with the
newest booking ref found in the thread. `MessageBubble` linkifies bare URLs;
a Google-Maps `/maps/dir` link renders as a "🧭 Get directions" chip instead
of the raw URL.
