# D1 PDPL checklist — every entry, collected

> **Spoke of `../CLAUDE.md`.** Assembled 2026-08-05 during the docs split.
>
> **This is an INDEX, not an authority.** Every entry below was already written
> somewhere in the old CLAUDE.md — scattered across six decision blocks, the API
> contract, the observability section and the data model, which is why nobody
> could see how many there were. Each row quotes the original and points at it.
> **The linked file is the authority; if this index and the source disagree, the
> source wins.** Nothing here was resolved by collecting it.
>
> Sixteen entries. Six were numbered "first" through "sixth" as they were written;
> the rest were logged in place and never numbered, which is precisely the problem
> this page exists to fix.

## The numbered audio/telemetry entries

| # | Entry | Status | Source |
|---|---|---|---|
| 1 | **STT: customer audio leaves the UAE** to a US endpoint — "which is new — no other part of this system sends customer data outside the region" | Unresolved | [decisions §1](decisions.md) |
| 2 | **STT: voice recordings are personal data and arguably biometric-adjacent** — so "forget me" (rule 12) MUST hard-delete the blobs; 48h retention | Mitigated, standing | [decisions §1](decisions.md) |
| 3 | **Read-aloud TTS: Aisha's REPLIES leave the UAE** — "a name, a mobile and a booking reference". Heavier than the STT path; "MUST NOT BE FILED WITH IT" | Mitigated (session-scoped clips, 48h, hard delete) | [decisions §2](decisions.md) |
| 4 | **LangSmith traces carry the WHOLE conversation** to a US endpoint, structured and searchable — **and rule 12 cannot reach it**. "THE HEAVIEST." | **Deferred, not waived** — uncomment `LANGSMITH_HIDE_INPUTS/OUTPUTS` in `deploy/prod/.env` before real users | [observability.md](observability.md) |
| 5 | **Feedback survey stores the customer's own prose** about a visit, which can identify them AND a member of staff; messages them outside any conversation they initiated | Erasure deletes survey rows and counts them in `would_delete` | [decisions §3](decisions.md) |
| 6 | **Voice call streams CONTINUOUS live microphone** to a US endpoint for the call's whole duration — not one bounded utterance | Mitigated (`CALL_ENABLED` default OFF, ephemeral mode stores nothing, minutes cap) | [decisions §4–5](decisions.md) |

## v10 — Salesforce & Google Maps (2026-08-03, all three unresolved)

| # | Entry | Source |
|---|---|---|
| 7 | The **sidecar mirrors customer name, mobile and booking data INTO the Salesforce org**, and rule 12 erasure reaches SF only through the sidecar's **restart-fragile in-memory reap**. Production fix: hook `services/erasure.py` to delete in SF directly | [decisions §6](decisions.md) |
| 8 | A spoken landmark — and, from the location button, the customer's **GPS COORDINATES** — go to Google's endpoints, **and the GPS fix is written into the transcript** as an ordinary customer turn, so it persists in `ConversationTurn` | [decisions §6](decisions.md) |
| 9 | **`POST /api/feedback` is UNGATED** (any caller, any session_id) and snapshots name+mobile into a row that **`services/erasure.py` does NOT delete** — verified 2026-08-03: a confirmed "forget me" leaves `agent_feedback` rows holding the customer's name and mobile | [decisions §6](decisions.md), [api-contract.md](api-contract.md) |

## Logged in place, never numbered

| # | Entry | Source |
|---|---|---|
| 10 | **Ref-alone booking lookup is a deliberate temporary relaxation, NOT production-ready.** Tolerable only while the payload carried no PII; the booking card put a name and mobile on screen, hence the service-layer gate. ⚠️ **WIDENED 2026-08-05:** on the agent journey a reference now IDENTIFIES its owner, so a session holding one *does* get the PII — see 16 | [api-contract.md](api-contract.md) — `GET /api/booking/<ref>` |
| 11 | **The whole console router is ungated** — "now heavier because this router can WRITE and can cause outbound contact" (attendance → WhatsApp) | [api-contract.md](api-contract.md) — `/api/console/bookings/<ref>/attendance` |
| 12 | **`comment` is the customer's own prose** and can identify them or a member of staff — "the heaviest thing on this already-ungated router" | [api-contract.md](api-contract.md) — `GET /api/console/bookings` |
| 13 | **Inbound WhatsApp: customer data + an identity-bearing link.** Mitigated by single-use tokens, short TTL, and the frontend stripping `?h=` from the URL on load | [api-contract.md](api-contract.md) — `/api/whatsapp/handoff/redeem` |
| 14 | **AI disclosure was REMOVED** by product decision 2026-07-20 — the original was a Must-have (FR-CX-06) on PDPL-transparency grounds. "A deliberate requirements override, not a lapsed control." Needs **Elite engagement sign-off** | [agent-rules.md](agent-rules.md) — rule 1 |
| 15 | **`MemoryProfile.notes` is free prose** in a schema that otherwise forbids display text — a model *could* write a stale English string into it. Capped and framed as guidance, **not structurally preventable** | [data-model.md](data-model.md) |
| 16 | **The manage-a-booking journey identifies by TD reference OR mobile, EITHER ALONE — no name** (product decision 2026-08-05, replacing the name+number second factor of 2026-07-23). So **one identifier is the whole credential**: a reference binds the session to its owner, and a mobile binds to that number's customer, exposing name, consent flags, consented memory and their bookings, and permitting reschedule and cancel. Asked for and weighed twice with the developer before building. Mitigations, all structural: the reference is 4 random base32 chars from `secrets` (~1.05M, no ordering signal) and is delivered by WhatsApp to that number; the identify path is a pure lookup that can never CREATE a customer; the ownership guard is untouched, so a bound session still cannot act on a booking it does not own. **Production fix if it is ever wanted back: require both, or fall back to the name only when no reference is held.** `_next_ref`'s randomness is now the only line guarding this journey rather than defence in depth | [agent-rules.md](agent-rules.md) — rule 10; `services/booking.identify`; `tests/test_identity_by_reference.py` |

## Go-live blockers that are not PDPL entries but sit beside them

- **`getUserMedia` requires HTTPS** — the mic button is dead for every phone user on plain HTTP, and since v10 removed the on-screen hint it is now **silently** dead. `wss://` needs a secure origin too, so this covers the call as well. Cheapest fix is a tunnel. *(decisions §1, §4)*
- **Rotate the Salesforce consumer secret and the Google Maps key** before production — the dev-org credentials shared during the v10 build are throwaway. *(decisions §6)*
- **`feedback` must survive `NOTIFY_SEND_KINDS`** — any `.env` that sets that var explicitly overrides the whole default list, and the survey opening is then silently retired as `kind_not_enabled`. Has already bitten `noshow` once. Check `deploy/prod/.env` and `deploy/dev/.env`. *(decisions §3)*
