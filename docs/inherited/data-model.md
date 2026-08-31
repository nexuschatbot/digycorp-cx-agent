# Data model (backend)

> **Spoke of `../CLAUDE.md`.** Moved out of the root file on 2026-08-05, wording
> unchanged. Root keeps only the non-negotiables (no display text in data fields,
> the seed reality, the `reset-demo` ritual) and points here for the rest.

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
once-per-booking guard, the same shape as `noshow_notified_at`) ·
AgentFeedback (**post-chat feedback on Aisha herself, v10 2026-08-03** —
`agent_feedback` table, created by `create_all` on startup, no migration;
session_id indexed, customer_id nullable FK, **client_name / mobile /
booking_ref SNAPSHOTTED at write time** so the Salesforce sidecar can push a
self-contained record — a deliberate deviation from the pointers-never-
snapshots rule, inherited from the external build; stars 1–5, comment,
created_at set APP-side as naive Dubai time (the FeedbackSurvey clock lesson
applied by construction — `server_default=func.now()` exists but the API
always assigns explicitly), **synced_at = the sidecar's restart-safe
watermark**. ⚠️ NOT in the rule 12 erasure sweep — see the v10 D1 entries).

**No display text in data fields.** This bug has appeared four times (price,
hours, seats/fuel, showroom city). Structured data drives logic; the agent
phrases it, per language, at speaking time. A stored English string cannot be
spoken in an Arabic conversation (LOC-01).

Seed reality: customers, vehicle_models, showrooms, and memory_profiles are
seeded from `seed/*.json` fixtures. Slots are **generated**, not seeded
(`python -m app.cli generate-slots --days 14 --prebooked 0.18 [--reset]`) — the
rolling window goes stale, so regenerate before any live session and between test runs.
The demo booking is created by `python -m app.cli create-demo-booking`, which
calls the booking service through the real write guards — never an INSERT, so
broken guards fail at your desk instead of in front of a customer.

**Resetting the environment is ONE command:**

```
python -m app.cli reset-demo
```

It does the whole ritual in order — drop/recreate all tables, re-seed
customers/vehicles/showrooms from `seed/*.json`, seed the memory profiles,
regenerate the rolling slot window, create the demo booking through the real
write guards, and **clear the agent checkpoint store**. The individual commands
remain for targeted work, but run the ritual by hand and the checkpoint step is
the one that gets forgotten.

**It ends by printing what Aisha will actually recall about the seeded returning
customer.** Test S-07 against that, not against `seed/memory_profiles.json`: Ravi's
booking derives his model/showroom/daypart from the slot it really booked, so the
fixture deliberately seeds him **`notes` only** and lets the booking own the rest
— the two writers converge because only one writes each column. The booking's
car is pinned (`cli.DEMO_VEHICLE_CODE`) so the result is repeatable, but the daypart
still follows whatever slot is ≥25h out.

**Why the checkpoint step matters (this bites in production).** The LangGraph
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

**THE SHIPPED CATALOGUE IS JETOUR-ONLY.** The shipped seed contains nine Jetour models and six
Jetour showrooms — no SOUEAST, no The Elite Cars. Those two brands are a
**production-roadmap item, not shipped data**: Elite Group represents them in
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
it and will fail in front of a customer.

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
  passing conversation as evidence it works.
- **The G700's doorstep exclusion lost its stated reason.** `slots.py` keeps the
  flagship out of the doorstep fleet, justified by it being "the only model with
  a dedicated showroom". That showroom is gone, so the exclusion now rests on
  "it is the flagship" alone. Deliberately NOT reversed in the same change:
  removing it would ADD doorstep slots for the G700, which is a product decision
  and customer-visible, not a tidy-up. Still to confirm with Elite.

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
