# Migration checklist — Elite-group → digycorp-cx-agent

Source: `/Users/themacstore/Desktop/Digycorp/Elite-group` (333 files)
Target: this repo, branch `test`
Architecture: [elite-multiagent-architecture.md](elite-multiagent-architecture.md)
Carried-forward defects: [OPEN-ISSUES.md](OPEN-ISSUES.md)

**Rule zero: port, don't re-author.** Prose, comments, anchors, tools and tests
move verbatim. Only assembly code (prompt composer, router, tool binding) is new.

**Scope:** Elite only. `general-prod` is deliberately untouched — no multi-tenancy.

---

## Done

- [x] **S0 — Deps + tests**
  - [x] `requirements.txt` ported, pinned to source venv versions
  - [x] `requirements-dev.txt` + `-r requirements.txt` (CI installs only this file) + ruff
  - [x] 49 test files + `conftest.py` + `fixtures/speech_24k.wav` — byte-identical
  - [x] `ops/run-tests.sh`
- [x] **S1 — Copy `app/` verbatim** (parity verified)
  - [x] `app/` 179 files incl. `assets/silero_vad.onnx` (sha match)
  - [x] `seed/`, `salesforce-setup/`, `scripts/`
  - [x] **Suite green — 1016 passed**, 3m28s. The only 2 failures are the
        scaffold's own `tests/test_health.py` (open decision 5).

---

## S2 — Port the non-app estate  ✅

- [x] `docs/inherited/` — 14 files: the 7 specs (2,200 lines), CLAUDE.voice-call.md
      (111KB), source CLAUDE.md, DOCKER.md, SETUP.md, SECURITY-PLAN.md, READMEs
- [x] `sidecars/sf_sync.py`
- [x] `ops/` — `maintain-slots.sh`, `topup_slots.py`, `refresh_sf_slots.py`
- [x] `backend/.env.example` (source's 28-var version; **live LangSmith key
      redacted** — see Security below)
- [x] `backend/Dockerfile`, `backend/.dockerignore`
- [x] `deploy/inherited/` — compose + Caddyfile + dev/prod provisioning, ported
      but **NOT wired** (open decision 2). See its README.
- [x] Real `dev/.env` and `prod/.env` deliberately **not** copied — live secrets

## S3 — Frontend  ✅

- [x] Replaced the `.jsx` scaffold with the source's TypeScript frontend (71 files)
- [x] `src/` — App.tsx, 11 card components, hooks (useChat/useCall/useVoice/useSpeech),
      i18n.ts, rtl.ts, styles.css
- [x] `src/admin/` — 9-tab console; `src/insights/`
- [x] `public/` assets, `scripts/` (6 harnesses), `nginx.conf`, `Dockerfile`
- [x] `tsconfig.json`, `tsconfig.node.json`, `vite.config.ts`, `package.json`
- [x] `npm run build` green — `tsc && vite build`, so it type-checks clean
- [x] `npm run lint` green — **added** as `tsc --noEmit`; the source repo had no
      lint script and no ESLint config, but this repo's CI runs `npm run lint`.
      Overrule if you would rather have real ESLint with a TS config.
- [ ] `package.json` still names itself `elite-group-frontend` (kept verbatim)

## S4 — Prompt split mapping  ✅

- [x] Roster decided: **BASE + TestDrive + BookingManagement**
- [x] Rule-by-rule table signed off — `S4-prompt-split-mapping.md`
- [x] All 29 body sections, 9 anchors and **22** tools assigned
- [x] Three cross-journey breaks resolved by the roster

## S5 — Execute the split  ✅

- [x] `app/agent/prompts/{base,testdrive,bookingmanagement,compose}.py`
      — 13 / 13 / 3 blocks, each a mechanical slice of the original literal
- [x] `prompt._PROMPT_BODY` now assembles from the blocks; callers unchanged
- [x] `compose.single_agent_body()` **byte-identical** to the shipped body,
      asserted against `tests/fixtures/prompt_body_original.txt`
- [x] `compose.body_for(agent)` ready but unused until S7
- [x] Suite green — 1023 passed
- [ ] **Anchors not split** — S5 covered the BODY only. The 9 anchors still live
      in `prompt.py` and reach every agent. → S7

Size honesty: TestDrive is **95%** of the original body (46,629 of 49,277 chars);
BookingManagement is 53%. The split buys correctness, not the ~20k→8–10k token
reduction doc §6.1 promises. If prompt length is the real driver of rule-dropping,
the lever is trimming BASE or splitting TestDrive by stage — not this split.

## S6 — Differential harness  ✅

- [x] `app/agent/tool_binding.py` — the S4 map as enforceable data; every one of
      the 22 registry tools reachable, asserted in CI
- [x] `evals/differential.py` — cross-checks prompt blocks against tool binding
- [x] `tests/test_evals_differential.py` — CI gate
- [x] `evals/README.md`
- [x] **Network guard** in `tests/conftest.py` — any non-loopback socket fails the
      test that opened it. Credentials were already blanked; this removes the call
      itself, for paths nobody thought of.

**Found four real defects the 1023-test suite could not see:**

1. `get_booking` bound to BookingManagement only, but `_BOOKINGS_ANCHOR` reaches
   every agent and says "call get_booking for it first" → **fixed**, now shared
2. `get_showrooms` shared, but no BookingManagement prose guides it → **fixed**,
   TestDrive only
3. `_SECOND_DRIVE_ANCHOR` reaches BookingManagement, names 3 tools it cannot call
   → allowlisted, S7
4. `_OPTIONS_ANCHOR` carries a `compare_vehicles` paragraph that reaches
   BookingManagement → allowlisted, S7

Live output-level replay is deliberately **not** built — it needs real model calls
and the suite is offline by construction. Run it outside CI, deliberately.

## S7 — Router

- [x] **S7a — anchors split.** `prompts/anchors.py`; `_SECOND_DRIVE_ANCHOR` and the
      `compare_vehicles` paragraph (split out of `_OPTIONS_ANCHOR`) now go to
      TestDrive only. Differential allowlist cleared — both agents clean.
      Rule: a **state** anchor is shared, an **instruction** anchor belongs to the
      journey owning the tool it names.
- [x] **S7b — routing cascade.** `app/agent/router/`: `prefilter` (reuses
      `booking._normalise_ref`, not the doc's regex — which misses `.`/`_`
      separators and has an unbounded suffix), `sticky`, `classifier` (Haiku,
      forced tool use, temperature 0, pooled client), `policy`, `route`.
- [x] `evals/routing_cases.py` — 32 labelled cases (EN/AR/mixed, money traps,
      session-dependent follow-ups)
- [x] `tests/test_router.py` — 42 offline tests, canned classifications
- [x] `evals/routing_live.py` — live checker, deliberately outside pytest
- [x] **Live verified: 32/32 accuracy, 0 false switches, 34% settled free by rules**
- [x] **S7c — tool binding enforced.** `system_prompt(agent=...)` and
      `tool_binding.schemas_for(agent)` both driven by ONE `AgentState.agent`
      value, so prompt and tools can never disagree. TestDrive 20 tools,
      BookingManagement 13, unrouted 22 (unchanged).
- [x] **S7d — `turn_trace`**, 25 columns, created by `create_all`. Fail-safe
      writes; `misroute_candidates()` for doc §9's weekly review.
- [x] **S7e — connected.** `run_turn` routes every customer turn (chat AND the
      pipeline call), behind `ROUTER_ENABLED` (default **on**, kill switch).
- [x] `tests/test_routed_turn.py` — the routed path, classifier stubbed
- [ ] Re-measure classifier latency **on the UAE VM** (dev laptop is not evidence)
- [ ] Router eval in CI for the model cases (needs a key in CI)
- [ ] Re-tune prompt caching for two prompts

### Decisions taken during S7e

**The ported suite is pinned to `ROUTER_ENABLED=False`.** Those 1000+ tests are
the single-agent contract, and that contract is what the migration must not
break. Left routing on, the pre-filter quietly re-points a test like "I want to
cancel my booking" at BookingManagement's shorter prompt and 13 tools — it would
still pass or fail, but no longer testing what it was written to test. The routed
path is covered separately by `tests/test_routed_turn.py` and `test_router.py`.
One test asserts the pin itself.

**`fallback` is not an agent.** There is no fallback prompt and no fallback tool
set; `body_for("fallback")` raises. A clarify-or-escalate turn degrades to
UNROUTED — combined prompt, whole registry — which is exactly the system that
handled those turns before the split. An already-active agent is kept.

**The router client does not retry.** The SDK default (retry with backoff) is
right for the agent's own call and wrong for a classifier, which has a free
fallback. Measured: with retries on, a blocked network turned a 165s suite into
673s. In production that is silence on a phone call.

### Two defects found only by running the suite with routing live

1. SDK retries — 165s → 673s. → `max_retries=0, timeout=5.0`.
2. Nine call-gateway/relay/streaming tests drifted. → suite pinned, routed path
   covered separately.

### Latency — the doc's 350ms is not reproduced

Measured from a dev laptop, pooled client, temperature 0:

| | p50 |
|---|---|
| bare 1-token Haiku call, tiny prompt | 1021ms |
| our full router system prompt, 1 token out | 772ms |
| full classifier call (forced tool use) | 1088ms |

~750ms is round-trip floor **from this machine**; the router prompt adds nothing
measurable and forced tool output adds ~300ms. So the figure must be re-measured
on the UAE VM before anyone trusts it. Even so, ~300–600ms of real model work
against a 400ms first-audio budget makes the free pre-filter/sticky exits
**load-bearing on voice**, not an optimisation.

### Two defects found only by calling the model

1. `temperature` left at the default 1.0 — the same message scored 0.85 then 0.65,
   crossing `CLARIFY_FLOOR` and changing the route. A customer saying identical
   words twice could get different agents. → temperature 0.
2. A fresh `anthropic.Anthropic()` per call — new TLS handshake every turn.
   → pooled; p95 3284ms → 1723ms.

## S8 — Data layer (four separate steps, suite green between each)

- [ ] **S8a** Postgres checkpointer — replace `SqliteSaver`
      (`langgraph-checkpoint-postgres`)
- [ ] **S8b** Salesforce as system of record — rewrite `services/salesforce.py`
      behind existing tool signatures; dual-run reads to verify parity, then flip
      writes; retire `sf_sync.py`
- [ ] **S8c** Redis cache — read-through + TTLs + write-through on the bookings anchor
- [ ] **S8d** pgvector hybrid search — **additive only**: embeddings widen recall,
      `_ranked_for`/`_pick_spread` keep selection + `basis`, or
      `test_catalogue_ranking.py` and `test_catalogue_spread.py` go red

## S9 — Guardrails & infra hardening

- [ ] Ingress: prompt-injection screening, PII flagging for trace masking
- [ ] Egress: confirmations extended to reschedule/cancel
- [ ] Degraded mode (phone: take a number, never dead air)
- [ ] Cost + Salesforce quota alerts
- [ ] Provision a database on the test VM (INFRASTRUCTURE.md: "No database. To be decided")
- [ ] Find a home for the background jobs: notification dispatcher (apscheduler),
      `sf-refresh` thread, cache prewarm, and the 3 `ops/` crons
- [ ] TLS on the test environment (currently plain HTTP)
- [ ] Check the VM holds it — `Standard_D2als_v7`, 2 vCPU / 4 GB
- [ ] Fix the broken morning VM auto-start (managed identity needs
      Virtual Machine Contributor)

---

## Open decisions

| # | Decision | Blocks | Status |
|---|---|---|---|
| 1 | **Agent roster** — `TestDrive + BookingManagement` (proposed) vs doc's `Sales + Bookings` | S4, S5, S7 | **open** |
| 2 | Deploy model — source ships 4 containers (incl. Postgres + sf-sync); target is rsync + systemd + nginx, room for one service | S2, S9 | open |
| 3 | `ruff check .` — 12 errors in ported tests, more expected across `app/`. Fix, or add a config matching how the source was linted? | CI | open |
| 4 | Python — source dev runs 3.14, this repo's CI pins 3.12 | CI | open |
| 5 | `tests/test_health.py` asserts `service == "digycorp-cx-agent"`; ported `main.py` is titled "Elite Test Drive Booking API" — **and the app serves `/api/health`, not `/health`, so `remote-deploy.sh` fails every deploy's health gate** ([OPEN-ISSUES.md #6](OPEN-ISSUES.md#6-health-404s-so-every-deploy-fails-its-health-gate)) | S1, **deploys** | open, now urgent |
| 6 | `psycopg[binary]` left unpinned — not installed in the source venv, so no production version to copy | — | noted |

Eight further defects, each with its live evidence, are in **[OPEN-ISSUES.md](OPEN-ISSUES.md)** — carried out of `multi-agent-migration` deliberately, to be fixed on their own branch.

## Multi-agent migration — Phases 0 to 8 (August 2026)

Built and **connected**, on branch `multi-agent-migration`. **The single-agent
path is untouched and still live**; everything new sits behind a flag, which is
why the single-agent suite still passes against the old shape — **1,653 tests,
12 failing as the dev `.env` stands** ([OPEN-ISSUES.md #5](OPEN-ISSUES.md#5-env-leaks-flags-into-pytest--10-failures)
and [#6](OPEN-ISSUES.md#6-health-404s-so-every-deploy-fails-its-health-gate)).

> **This section said "Done" for a phase while the cascade was unreachable.**
> Every module existed and was tested; nothing in `app/` imported
> `app/orchestrator/`. Corrected 29 Aug 2026 — see the architecture doc §6, which
> now separates *not built* from *built and not reachable* and lists the four
> defects of the second kind that this migration produced in a row. The lesson
> for this checklist: a phase is done when a REQUEST can reach it, not when its
> files exist and its tests pass.

| flag | default |
|---|---|
| `MULTI_AGENT_ENABLED` | off — and voice is excluded even when on |
| `RETRIEVAL_ENABLED` | off |
| `PROMPT_VERSIONS` | unset — per-agent rollback with no deploy |

**What changed structurally**

- **Alembic**, adopted on a `create_all` database. `main.py` migrates at boot.
- **Prompts are Markdown** under `app/agents/`, versioned, with an activation
  gate that refuses a version which has lost a load-bearing rule.
- **Variant-level catalogue**: 9 models, 15 variants, 330 stock rows, 7,544 units.
- **Local retrieval**: vendored ONNX embeddings, 228 chunks, Redis-cached.
- **Orchestrator**: select → order → execute, plus the only commercial writes.
- **PDPL**: `turn_trace` swept, transcripts reachable by customer, retention job.

**Wired on 29 August 2026** — the work that turned the above from modules into a
system, all of it found by asking "who calls this?" rather than by a failing test:

- **`emit_briefs`** written and bound to Aisha; an `orchestrator` node added to
  the graph; findings returned as that call's tool_result so her next generation
  composes through the existing streaming and guards.
- **Five sub-agent tools** written or registered — `get_variants`, `check_stock`,
  `get_on_road_price`, `get_finance_estimate`, `search_knowledge`. The cards had
  named all five for a phase; none existed, and **finance bound zero tools**.
  They live in a `SUBAGENT_REGISTRY` so `TOOL_SCHEMAS` — and the single agent's
  cached prefix — stays byte-stable.
- **The commercial write path**: sub-agents propose, the card is the allowlist,
  `booking_state` gates, the service guards refuse independently, the
  orchestrator commits. `write.py` had no caller at all before this.
- **The date anchor** for sub-agents; without it TestDrive dated "Saturday" to
  Jan 2025 and reported no slots.

**What is deliberately not done**

- The T0.1 baseline has not been re-run against a correct fixture (T8.4) — see
  the fixture note below.
- Aisha has no prompt prose about delegating; she has the tool description alone.
- ~60 brochure marketing pages unread; three files still `draft-partial`.
- T3.6's threshold is calibrated on card utterances, not real briefs.

**Two eval fixtures were wrong, and both produced plausible results**

- `booking_full` turn 3 said "any branch, whatever slot is earliest" — which
  names no branch, so Aisha could not lock a slot and the remaining five turns
  talked past each other. It deadlocked identically in the single- and
  multi-agent runs, which is why it looked like a property of the script rather
  than a fault in it.
- `baseline._seed_db` seeded **zero variants, zero stock, zero finance banks** —
  ported from a script predating the variant-level catalogue. Model-level tools
  were unaffected, so the single agent never noticed and the sub-agents were
  starved. ~$1.10 of live measurement was spent before this was found.
  `tests/test_baseline_fixture.py` now guards it.

**Things the build found that no test would have**

- **Four "built but unreachable" defects in a row** — the orchestrator, the
  sub-agent tools, the write path, the date anchor. Each was complete, tested,
  and connected to nothing. See the architecture doc §6 for the table.

- Capturing a baseline first (~$1.48) caught Aisha **delivering third-person
  notes-to-self** to customers. Fixed in Phase 6.
- The seed had the **two X70s' seat counts swapped**, and a code comment repeated
  the error as fact.
- **"Forget me" was a partial delete that looked complete** — `turn_trace` was
  never swept.
- The **retention windows had been declared and never read** by anything.

### Post-wiring hardening — 31 August 2026

The cascade had been reachable for two days. **Six defects, none of which any
test could see, all found by driving the app in a browser and reading the logs
beside it.** Three commits: `8f8c80f`, `c47f9f1`, `a0130fe`.

| # | Defect | How it presented | Fix |
|---|---|---|---|
| 1 | A specialist's reply was **discarded whole** when it held no JSON — 14 of 47 steps | the step returned `ok` with no findings, which `compose_input` put in neither list, so Aisha saw a lookup that was not there and briefed the same specialist three times over 22s | prose rides `_note`; a step that truly returns nothing is **named as a gap** |
| 2 | The finance card declared `produces: [emi_estimate, on_road_price]` — **two names its tools never return** | `entities=[]` on every finance step, and no early settle: all five measured paid a second round to summarise what they already had | `produces: [on_road_aed]`. 2 rounds / 3.2–4.1s → 1 round / 1.3s, answer unchanged |
| 3 | A specialist **asked for the same booking twice** — once by tool call, once by proposal, with different argument spellings | both survived the dedupe, both executed, **two identical cards**. One DB row only, because `booking.create`'s guard 2 is idempotent | proposals dedupe on *what the write acts on*, not on every argument |
| 4 | **Accessories had no tool.** Seeded since T0.7, read by nothing | asked the price of the alloy wheel upgrade, Aisha said we did not hold it, with `4500` sitting in the row | `services/accessories.py` + `tools/accessory_tools.py` + the sales card entry |
| 5 | **Retrieval buried the policy docs** whenever a customer named a car | `"warranty"` → warranty.md rank **1**; `"what warranty comes with the X70 FL Plus"` → rank **39** | `COLLECTION_FLOOR = 2`. 7/9 policy questions at a floor of one, **9/9 at two** |
| 6 | **Visit details were promised and not recorded.** "my wife and two kids, we'll need a child seat" → `child_seat=0`, `companions` empty | nothing in the pipeline dropped them — the sub-agent that writes the booking had never been told the brief *is* the conversation it did not see | prose at three decision points: the testdrive card, `emit_briefs`, `create_booking` |

**Why five of six were invisible to the suite.** Each is a disagreement between
two things that are individually correct: a card and its tools (2), a tool call
and a proposal (3), a table and the code (4), two collections and one ranking
(5), a gatherer and a writer (6). A unit test asserts one side.

**The latency picture, measured over ~120 calls.** Recorded here because it
changed the ranking of what to fix next:

| | fixed cost per call | per output token |
|---|---|---|
| Sonnet 4.6 (Aisha) | 2,401 ms | 20.0 ms (r = +0.94) |
| Haiku 4.5 (specialists) | 1,123 ms | 7.3 ms (+2,248 ms per extra round) |

The fixed cost held flat (r ≈ 0) against **every input the app controls** —
prompt length, cache state, tool count, brief count — so it is not shortenable
from here. A median 17.7s cascade turn was prefill 5.2s (30%), **Aisha's own
typing 7.4s (42%)**, specialists 5.1s (29%). Post-fix p50 14.7s, small sample,
directional only. Detail and the remaining levers:
[OPEN-ISSUES.md #2](OPEN-ISSUES.md#2-the-per-model-call-latency-floor).

**Also shipped that day:** `MAX_CASCADES_PER_TURN` (default 2) — a ceiling on
re-briefing within one turn, refused as a `tool_result` rather than an error so
Aisha answers from what she already has. It is a **number, not a constant**, and
whether it costs answer quality is unmeasured —
[OPEN-ISSUES.md #3](OPEN-ISSUES.md#3-max_cascades_per_turn--2-is-an-unproven-guess).

**All ten of Elite's workbook sheets were audited against what the app can
answer.** Models, Stock, On-Road Pricing and Financing are wired and correct
(banks 3/3, stock 330 rows); Purchase Confirmation was already prose in the
finance card; Warranty, Delivery, Registration and Promotions existed but were
buried by defect 5; Accessories was defect 4.

**What was NOT fixed, and why:** eight items, each with its evidence and the
reason it needs a decision rather than a patch →
**[OPEN-ISSUES.md](OPEN-ISSUES.md)**. The one to take first is a specialist that
narrates instead of proposing the write — two of three live booking runs
succeeded, and the third ended in a human handoff.

---

## Known gaps in the architecture doc

The doc describes roughly 60% of the system. Absent entirely: feedback surveys,
notifications/WhatsApp reminders, waitlist, brochures, admin console + insights,
device recognition, erasure/PDPL, and the one-tap intents that bypass the LLM
completely (no agent, no router — routing accuracy does not apply to them).

---

## Security — found during S2

The source repo stores live production credentials in two places:

1. `deploy/dev/.env` and `deploy/prod/.env` — Anthropic key, OpenAI key,
   WhatsApp access token + phone number id, LangSmith key, Salesforce client
   id/secret, Google Maps key, TURN secret. Gitignored, so not in git history,
   but present on disk and shipped to the VMs. **Not copied into this repo.**
2. `elite-group-backend/.env.example` — a **real** `LANGSMITH_API_KEY`. This
   file is tracked (`!.env.example`), so the key is in the source repo's git
   history on GitHub. Redacted in this repo's copy.

- [ ] **Rotate the LangSmith key** — it is in a public-capable git history
- [ ] Decide whether the other credentials need rotating before they are reused
      for the new VMs
