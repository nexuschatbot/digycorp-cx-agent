# Booking Identity-Hardening Plan

**Status:** IMPLEMENTED 2026-07-23. Verified against DB/service + scripted-graph
tests (262 passing). Live-model tests still to be run by the developer — see
"Verification" at the end.
**Date:** 2026-07-23
**Goal:** A booking can no longer be viewed or changed from a guessed TD reference.
The customer must prove identity with **full name + phone number**, and cancel /
reschedule physically cannot run without it — even if the model is confused.

---

## Decisions (confirmed with the developer)

| # | Decision |
|---|----------|
| 1 | To **view or change** any booking: customer gives **full name + phone number**, both. |
| 2 | **Full name, case-insensitive.** "Ravi" alone FAILS — "Ravi Sharma" required. |
| 3 | Gate applies to **reads AND writes** (view, reschedule, cancel). |
| 4 | **Ref alone is not enough** — the agent asks for full name + phone before anything. |
| 5 | **Name must match the number.** Mismatch → honest **"no booking found"** (reveals nothing). |
| 6 | Cancel / reschedule refuse server-side unless the session is **verified** as the booking's owner. |
| 7 | **Refs stay sequential** (`TD-2026-0003`) for the demo — the guard closes the enumeration hole. Random refs = production note only. |

---

## What is true today (why this is needed)

- `services/booking.py:_lookup` finds a booking by **ref OR mobile, either alone**,
  and deliberately does NOT check session ownership (FR-RS-01, decided 18 Jul).
  The file already carries a `# PDPL PRODUCTION-HARDENING` note saying to require a
  name/number match in production — this plan implements that note.
- `reschedule` and `cancel` act on whatever a bare ref/mobile resolves to. **A ref
  alone will cancel a booking.** Their only callers are the two agent tools.
- `create_booking` is already safe — its `customer_id` is injected server-side, never
  from the model. Untouched by this plan.
- `find_or_create` (customers.py) never compares the name — it only fills a blank
  field, never matches or overwrites. So a right-number / wrong-name pair passes today.
- Refs are `MAX+1`, sequential and enumerable (`_next_ref`).

---

## The changes (5 steps)

**1. Name+number match — `services/customers.py`**
Full name becomes a **match key**, not just a fill value. If the number belongs to an
existing customer whose stored full name does not match (case-insensitive, EN or AR
script) → return "not found", do NOT bind the session.

**2. Verified-identity guard on writes — `services/booking.py`**
`reschedule` and `cancel` receive the session's verified `customer_id` (injected).
After the booking is found: if it is not this customer's → `{"error":
"identity_not_verified"}` as data, no write.

**3. Read lookup requires name+number — `services/booking.py` `_lookup` + get_booking tool**
Mobile branch requires a matching name. Ref-alone returns an "ask for name + phone"
signal so the agent requests both before showing anything.

**4. Wire injection — `app/agent/tool_node.py` + `app/tools/booking_tools.py`**
Add `reschedule_booking` / `cancel_booking` to the customer-id inject set; thread the
id through the thin tool wrappers. Same mechanism `create_booking` already uses.

**5. Production note only (no logic change) — `_next_ref` docstring + D1 checklist**
Propose random ref suffixes (`TD-2026-8K3F`) for production. Demo keeps sequential.

---

## Stays untouched (server-trusted, not a customer typing a ref)

- **WhatsApp inbound** (`whatsapp_inbound.py`) + **handoff redeem** — the sending
  number is the credential; forcing a name would break inbound handoff (S-09).
- **Card reload** `GET /api/booking/{ref}` — rebuilds a card THIS session already
  showed, PII-gated by the session checkpoint. Not a "customer looks up a booking".

---

## Also affected

- **Tests:** reschedule/cancel tests need the verified id; new tests for
  name-mismatch → "not found" and `identity_not_verified`.
- **Acceptance:** S-09 (reminder→reschedule — arrives already identified, fine),
  **S-12 / S-13 (cancel)** now begin with a full-name + phone identification step —
  confirm this matches the demo script before building.
- **Requirements:** this reverses FR-RS-01 / FR-CN-01 ("either alone is enough") for
  the demo. Flag on the D1 PDPL checklist as an intentional override.

---

## What shipped (files changed)

- `services/customers.py` — `verify_name` second factor (`_verify_name`,
  `_name_matches`, full-name case-insensitive, EN/AR). OFF by default.
- `services/booking.py` — `owner_customer_id` scoping in `_lookup`/`_resolve_target`/
  `find`; `session_customer_id` identity guard on `reschedule` + `cancel`; random-ref
  production note in `_next_ref`.
- `tools/booking_tools.py` — `get_booking` refuses an unidentified session and
  owner-scopes results; `reschedule_booking`/`cancel_booking` pass the injected
  verified id; descriptions teach the new errors.
- `tools/customer_tools.py` — agent tool passes `verify_name=True`; description
  covers `name_required` / `identity_unconfirmed`.
- `app/agent/tool_node.py` — `reschedule_booking`/`cancel_booking` join
  `_INJECT_CUSTOMER_ID` (ownership key); comments corrected.
- `app/api/whatsapp.py` — handoff redeem binds via the SERVICE (`verify_name` off):
  the single-use token is the credential, so the name factor must not apply there.
- Tests: `test_customers.py`, `test_booking_management.py` (new identity-guard +
  name-match tests), `test_memory.py`, `test_booking_attendance.py`,
  `test_agent_graph.py` (journeys now identify first).

## Verification

- **Ran (passing):** test_customers, test_booking, test_booking_management,
  test_booking_attendance, test_memory, test_consent, test_agent_graph,
  test_memory_recall, test_whatsapp_handoff, test_tap_intents,
  test_conversation_close, test_conversation_resume, test_vehicle_detail_api.
- **NOT run — live model calls, developer must run:**
  `test_discovery_and_personalisation.py`, `test_chat_register.py`,
  `test_licence_reminder.py` (they call `build_config` without a scripted model).
  Also `test_speech.py` / `test_voice.py` (OpenAI). These exercise the full agent
  against the real model; run them before the demo to confirm the prompt/schema
  changes read well end-to-end.

## Flows that gained an identification step (check the storyboard)

- **S-07 returning customer:** the agent must now collect the FULL name + mobile
  before recall/booking works for a customer we hold a name for (e.g. Ravi Sharma
  → "Ravi Sharma", not "Ravi").
- **S-09 reschedule via reminder:** fine — the WhatsApp handoff arrives already
  identified (trusted bind).
- **S-12 / S-13 cancel:** now begins with full name + mobile. If the cancel
  storyboard does not already have the customer give both up front, add one line,
  or the demo will hit "please give me your full name and phone number" mid-flow.

## Not touched (deliberate)

- CLAUDE.md's FR-RS-01 wording ("ref OR mobile, either alone is enough") now
  diverges from the shipped write behaviour. Left as-is: it is a requirements
  override to log on the D1 PDPL checklist and with Elite, not a silent edit to the
  shared contract. Flag it there.
- Ref generation stays sequential (demo readability); random suffix is a
  documented production note only.
