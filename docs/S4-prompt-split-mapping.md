# S4 — Prompt split mapping (SIGNED OFF — executed in S5)

Roster: **BASE + TestDrive + BookingManagement**.
Nothing has moved. This table is the plan; S5 executes it verbatim.

Line numbers refer to `backend/app/agent/prompt.py`.

---

## 1. Prompt body sections (`_PROMPT_BODY`, lines 163–937)

| Lines | Section | → | Why |
|---|---|---|---|
| 167–194 | WHO YOU ARE (rule 1) — introduction | **BASE** | Every first reply carries it, whichever agent answers |
| 195–202 | GROUNDING (rule 2) | **BASE** | Facts-from-tools-only is universal |
| 203–210 | Call `get_showrooms` before offering branches | **TestDrive** | |
| 211–212 | Prefer nearest branch over listing all | **TestDrive** | |
| 213–214 | ONE QUESTION — restated for the showroom card | **TestDrive** | ⚠️ cross-journey #3, resolved: card *and* day question are now one agent |
| 215–239 | Time not free → offer, never ask | **TestDrive** | |
| 240–249 | Stay at the branch they already have | **TestDrive** | |
| 250–268 | Buttons / `offer_options` (words first, not for open questions) | **BASE** | Presentation; both agents ask closed questions |
| 269–292 | Brochure to WhatsApp | **TestDrive** | Needs `send_brochure` |
| 293–313 | Compare two cars side by side | **TestDrive** | |
| 314–337 | A model name is a lookup; you do not know the catalogue | **TestDrive** | |
| 338–355 | Doorstep offer (rule 7) | **TestDrive** | |
| 356–363 | Second drive runs after the first | **TestDrive** | |
| 364–415 | LANGUAGE (rule 3) | **BASE** | |
| 416–462 | HOW YOU TALK (rule 4) — one question per message | **BASE** | The hardest rule; both agents need it whole |
| 463–473 | Twice is the limit, then fetch a person (rule 13) | **BASE** | |
| 474–500 | YOUR VOICE (rule 14) | **BASE** | |
| 501–551 | OUTPUT FORMAT (rule 17) | **BASE** | |
| 552–652 | FINDING THE RIGHT CAR (rule 5) — incl. "a booking intent is not a request for cars", never-say-popular, narrowing-is-a-new-search | **TestDrive** | |
| 653–677 | MAKING THE VISIT THEIRS (rule 8) | **TestDrive** | ⚠️ cross-journey #2, resolved: fires between slot-accept and recap, now same agent |
| 678–705 | WHAT WE REMEMBER (rule 12) | **BASE** | Memory conduct applies to both |
| 706–781 | BOOKING SEQUENCE (rule 9) — lock→recap→licence→consent→create, PDPL separation, two drives in one chat | **TestDrive** | |
| 782–795 | MANAGING AN EXISTING BOOKING (rules 10/11) — identify by ref OR mobile, never ask for a name | **BookingManagement** | The distinct identity door |
| 796–809 | RESCHEDULING (rule 10) — never ask why, same-branch default | **BookingManagement** | |
| 810–819 | CANCELLING (rule 11) — one retention offer | **BookingManagement** | |
| 820–850 | BRINGING IN A PERSON (rule 13) — escalation, helpline | **BASE** | |
| 851–890 | HOLDING THE LINE (rule 16) — persona attacks, other customers' data, data minimisation, no promises, off-limits topics, rudeness | **BASE** | Defence-in-depth for both |
| 891–937 | CLOSING (rule 19) | **BASE** | Text already covers "a booking, reschedule or cancellation" |

## 2. Anchors (lines 938–1373)

| Anchor | → | Note |
|---|---|---|
| `_DATE_ANCHOR` | **BASE** | |
| `_FACTS_ANCHOR` | **BASE** | |
| `_BOOKINGS_ANCHOR` + `_HELD_SLOT_SENTENCE` | **BASE** | Both agents must know live bookings |
| `_OPTIONS_ANCHOR` | **BASE** | |
| `_SECOND_DRIVE_ANCHOR` | **TestDrive** | ⚠️ cross-journey #1, resolved: it commands `search_vehicles`, which TestDrive now owns |
| `_RETURNING_ANCHOR` | **BASE** | Recognition can fire in either journey |
| `_LANG_ANCHOR` / `_LANG_ANCHOR_MIXED` | **BASE** | |
| `_VOICE_ANCHOR` | **BASE** | |

## 3. Tools (all 22)

**Shared — bound to both agents (10)**
`find_or_create_customer` · `identify_by_reference_or_mobile` ·
`confirm_returning_customer` · `save_memory` · `forget_me` ·
`request_human_callback` · `close_conversation` · `offer_options` ·
`get_available_slots` · `lock_slot` · `get_booking`

`get_available_slots` / `lock_slot` are shared because `reschedule_booking`'s own
description says *"call get_available_slots, let them choose, and lock_slot the
one they pick."*

`get_booking` is shared because `_BOOKINGS_ANCHOR` — appended to **every** agent —
says *"call get_booking for it first"*. Found by `evals/differential.py`, not by
the suite.

**TestDrive only (9)**
`search_vehicles` · `get_vehicle_details` · `get_showrooms` · `nearest_showroom` ·
`compare_vehicles` · `send_brochure` · `acknowledge_licence_reminder` ·
`create_booking` · `record_consent`

`get_showrooms` is **not** shared, reversing the proposal in §4.1 below: no
BookingManagement block guides branch selection, and writing that prose would
break rule zero. Port the guidance first, then widen the binding — in that order.

**BookingManagement only (2)**
`reschedule_booking` · `cancel_booking`

Resulting counts: TestDrive 20 tools, BookingManagement 13. TestDrive is over the
doc's ~8-tool guideline — acceptable at launch because the funnel is one journey,
but worth re-checking once the router is measured.

## 4. Open questions in this mapping

1. **Does BookingManagement need `get_showrooms` / `nearest_showroom`?** Rule 10
   defaults to the same branch *"unless they ask to move it"* — moving branch implies
   branch selection. Proposal: add `get_showrooms` only, not `nearest_showroom`.
2. **`confirm_returning_customer`** — see above.
3. **Does BookingManagement need the brochure section?** Proposal: no.

## 5. What S5 will do

- Move each block **verbatim**, comments included, into
  `backend/app/agent/prompts/{base,testdrive,bookingmanagement,anchors}.py`
- Add `compose.py` with `build_prompt(agent, ...)`
- Keep `system_prompt()` as a thin wrapper so nothing downstream changes
- Full suite (1016 tests) must stay green

---

## Corrections made after sign-off

- The registry holds **22** tools, not the 19 stated when this was drafted (and
  not the 13 the architecture doc §6.2 names). All 22 are assigned;
  `tool_binding.unassigned()` is asserted empty in CI.
- Open question 1 (`get_showrooms` for BookingManagement) resolved **no** — see above.
- Open question 2 (`confirm_returning_customer`) resolved **shared**, matching
  `_RETURNING_ANCHOR` in BASE.
- Open question 3 (brochure for BookingManagement) resolved **no**.
- The body ends at prompt.py line **908**, not 937 as the table's last row said.
- **S5 split the prompt BODY only.** The nine anchors still live in `prompt.py`
  and are appended to every agent. Moving `_SECOND_DRIVE_ANCHOR` to TestDrive and
  splitting the `compare_vehicles` paragraph out of `_OPTIONS_ANCHOR` is S7 work,
  tracked by the allowlist in `tests/test_evals_differential.py`.
