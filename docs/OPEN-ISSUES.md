# Open issues — carried out of `multi-agent-migration`

> **#4, #6 and #7 are fixed**, and the entries are kept rather than deleted
> because how each pair of individually-correct pieces disagreed is the part
> worth remembering. **Five open here, plus five new ones in the deployment
> section below**, all found on 31 Aug by putting the app on a real server for the first
> time since the port. The pipeline itself is now green end to end —
> [DEPLOYMENT.md](DEPLOYMENT.md).
> **#1 and #6 are fixed**, and the entries are kept rather than deleted — #6
> because how a route and a deploy script stayed individually correct for months
> is the part worth remembering, #1 because the shape of the fix (name the gap,
> don't ask the model harder) is the lesson this repo keeps re-learning. **Six
> open here, plus five new ones in the deployment section below**, all found on
> 31 Aug by putting the app on a real server for the first time since the port.
> The pipeline itself is now green end to end — [DEPLOYMENT.md](DEPLOYMENT.md).

Everything here was found by **running the cascade end to end in a browser** on
31 August 2026, after the wiring work described in
[MIGRATION.md](MIGRATION.md#post-wiring-hardening--31-august-2026). Six defects
found that day were fixed on that branch; these are the ones that were not, and
they are here rather than in a commit message because each needs a decision, a
measurement, or a change too wide to smuggle into a bug fix.

**None of these is a regression.** Every one of them predates the three commits
of 31 August, and the app is in better shape than it was that morning.

Each entry states the **symptom** a customer would see, the **evidence** (a log
line, a measurement, or a live transcript), **where** it lives, and **why it was
left**. Ordered by what it costs, not by how hard it is.

| # | Issue | Costs | Size |
|---|---|---|---|
| 1 | [A specialist narrates instead of proposing the write](#1-a-specialist-narrates-instead-of-proposing-the-write) | bookings | prompt + guard |
| 2 | [The per-model-call latency floor](#2-the-per-model-call-latency-floor) — levers built, `fix/latency-floor`; run still owed | ~15s turns | measurement first |
| ~~1~~ | [~~A specialist narrates instead of proposing the write~~](#1-a-specialist-narrates-instead-of-proposing-the-write) — **FIXED**, `fix/write-gap` | bookings | done |
| 2 | [The per-model-call latency floor](#2-the-per-model-call-latency-floor) | ~15s turns | measurement first |
| 3 | [`MAX_CASCADES_PER_TURN = 2` is an unproven guess](#3-max_cascades_per_turn--2-is-an-unproven-guess) | answer quality, unknown | one re-run |
| ~~4~~ | [~~The card strip follows search rank, not the answer~~](#4-the-card-strip-follows-search-rank-not-the-answer) — **FIXED** upstream, `8776775` (untested) | wrong car on screen | done |
| 5 | [`.env` leaks flags into pytest — 10 failures](#5-env-leaks-flags-into-pytest--10-failures) | CI trust | small |
| ~~6~~ | [~~`/health` 404s, so every deploy fails its health gate~~](#6-health-404s-so-every-deploy-fails-its-health-gate) — **FIXED**, `fix/health-route` | deploys | done |
| ~~7~~ | [~~Finance's CALLBACK prose instructs a write it cannot propose~~](#7-finances-callback-prose-instructs-a-write-it-cannot-propose) — **FIXED**, `fix/latency-floor` | a decided buyer stalls | done |
| 8 | [Accessories became reachable, and they have no Arabic](#8-accessories-became-reachable-and-they-have-no-arabic) | AR conversations | data |

---

## 1. A specialist narrates instead of proposing the write

**Symptom.** The customer asks for a test drive, agrees a slot, and the turn ends
with a human handoff instead of a booking. **Two of three live booking runs
succeeded; the third did not.**

**Evidence** — the failing turn, from the 31 August logs:

```
testdrive  tools=[none]                    1098 chars (0 json)  proposals=0
testdrive  tools=[get_available_slots]                          proposals=0
cascade ceiling reached (2)
```

The sub-agent had the slot. It wrote 1,098 characters *about* booking it and
proposed nothing, so `write.py` — the only thing that can create a booking — was
never handed an action. Round two called a read tool and again proposed nothing.
The ceiling then stopped a third round, correctly, and Aisha did the only honest
thing left: she offered a person.

**Where.** `backend/app/agents/testdrive/v1.md` (the prose that is supposed to
turn a settled slot into a proposal) and `backend/app/orchestrator/subagent.py`
(which observes `proposals=0` and cannot tell "nothing to write" from "forgot to
write it").

**Why it was left.** The fix is not one line. The prose lever has already been
pulled three times this branch — `MAKING_THE_VISIT_THEIRS`,
`THE_VISIT_DETAILS_ARE_IN_THE_BRIEF`, the `create_booking` schema paragraph —
and asking harder is what `config.py` and `sanitise.py` both record as *not* the
fix. The candidate worth building is a **structural** one: a brief that names a
write in its `produces` and comes back with `proposals=0` and no stated reason is
an incomplete step, and should be named as a gap the way an empty step now is
(see the `_prose_note` work). That is a change to the step contract, not to a
paragraph.

**This is the one to take first.** Everything else on this list is latency or
tidiness; this one loses bookings.

**FIXED on `fix/write-gap`.** The structural candidate above, built as
described — two halves, both in assembly code, no ported prose touched:

- **The guard** (`turn.py::compose_input`): a step whose card may propose
  writes, that comes back `ok` with `proposals=0` and **no structured
  findings** — only a `_note` or bare evidence — is appended to `unavailable`
  as `status: "no_action"`. The detail tells Aisha the three things she needs:
  nothing was written whatever the note says, never confirm it, and re-brief
  **once, naming the exact action** — or offer a person plainly. Both live
  rounds match the signature (round one was note-only, round two evidence-only);
  a read brief answered with data (`{"slot_id": 12}`) does not, so "what times
  on Sunday" stays clean, and the total-silence case keeps its existing
  `empty` name — one gap per step, never two.
- **The contract** (`subagent.py::PROPOSALS_INSTRUCTION`): the specialist is
  now told that an asked-for action it is not proposing needs a reason in its
  note — "prose about booking is not a booking". This is the orchestrator's
  T5-authored clause, not card prose, so migration rule zero is intact.

Seven tests in `tests/test_orchestrator.py` pin the signature from both sides
(fires on the two live shapes; stays quiet for read answers, read-only agents,
proposing steps, and the empty case). What is deliberately NOT here: no prompt
lever pulled on `testdrive/v1.md` — the three pulls recorded above were the
evidence that a fourth would not help.

---

## 2. The per-model-call latency floor

**Symptom.** A cascade turn takes about fifteen seconds.

**Evidence** — measured over ~120 calls on 31 August:

| | fixed cost per call | per output token | extra round |
|---|---|---|---|
| Sonnet 4.6 (Aisha) | **2,401 ms** | 20.0 ms (r = +0.94) | — |
| Haiku 4.5 (specialists) | **1,123 ms** | 7.3 ms | +2,248 ms |

The fixed cost held flat (r ≈ 0) against **every input the app controls** —
prompt length, cache state, tool count, brief count. It is not something the
code can shorten.

Where a median 17.7s cascade turn went, before the 31 August fixes:

| | | |
|---|---|---|
| prefill, all calls | 5.2 s | 30% |
| **Aisha's own typing** | **7.4 s** | **42%** |
| specialists | 5.1 s | 29% |

**The largest single line is Aisha's output tokens**, which was not the ranking
this was expected to have. Of those tokens, roughly **110 per call are the
`emit_briefs` JSON** — invisible to the customer and billed at 20 ms each, so
about 2.2 s a turn spent typing something nobody reads.

Post-fix p50 measured **14.7 s**, but on a small sample; treat it as directional.

**Where.** `backend/app/tools/brief_tools.py` (the `emit_briefs` argument shape),
`backend/app/config.py` (model choice).

**Why it was left.** The two honest levers both need a decision that is not the
code's to make: **a faster model for simple turns** (a router that keeps Sonnet
for the conversation and drops to Haiku when no specialist is needed), or **fewer
round trips**. Shortening the `emit_briefs` payload is the cheapest real win and
does not need either decision — but it changes a tool schema Aisha is measured
against, so it wants its own before/after run.

**LEVERS BUILT on `fix/latency-floor` — the before/after run is still owed.**
Both were built offline (no live calls; the decision to build both was taken
2026-08-31):

- **The payload.** `emit_briefs` now accepts a brief as **the bare question
  string**; the `{"brief": ..., "depends_on": [...]}` object survives for the
  one thing only it can say. `run.plan_turn` is the single place both shapes
  become one. The ~8 wrapper tokens per brief at 20 ms each come off every
  cascade with no loss of content — the brief itself, complete question and
  all, is untouched.
- **The model dial.** `SIMPLE_TURN_MODEL` (config, empty = OFF — the shipped
  default) runs Aisha's generation on a cheaper tier when a deterministic rule
  proves the turn simple. The only rule today is the router prefilter's
  bare-greeting check, reused not re-implemented; an affirmation or a consent
  answer never qualifies, because those sit inside the booking sequence.
  Same trusted-flag shape as `CALL_AGENT_MODEL`.

**What is still owed:** the before/after latency run (this entry's own
requirement) before either the schema change is trusted at the measured ~110
tokens/call or the dial is turned ON. Neither has been measured live; the p50
numbers above are still the current truth. Tests:
`tests/test_simple_turn.py`, plus the string-brief cases in
`tests/test_orchestrator.py`.

---

## 3. `MAX_CASCADES_PER_TURN = 2` is an unproven guess

**Symptom.** None observed. That is the problem.

**Evidence.** The ceiling shipped on 31 August to stop a re-brief spiral that was
costing 22 seconds a turn, and it works. But its own comment says so plainly:

> *A NUMBER, NOT A CONSTANT, because whether this costs answer quality is an
> open question that only a re-run can settle.*

Two was chosen because Aisha's prompt legitimately permits a second pass "for
something the answers themselves revealed", and one would have removed that.
Nothing has measured whether a turn that wanted a third round now answers worse.

**Where.** `backend/app/config.py::MAX_CASCADES_PER_TURN`.

**Why it was left.** It needs the T0.1 baseline re-run (T8.4), which is still
outstanding for its own reasons — and that run costs live model calls against the
USD 3 cap. It is settable from the environment, so nothing has to be deployed to
test it.

---

## 4. The card strip follows search rank, not the answer

**Symptom.** Aisha recommends one car in prose and the card strip leads with a
different one.

**Evidence.** The strip is built in `runner.py` from `tool_event` payloads as
they stream — so its order is whatever `search_vehicles` ranked, and its contents
are whatever the specialists happened to look at. Aisha's judgement about which
car actually answers the question is expressed in the sentence she writes, and
the strip never sees it. The duplicate-suppression and trimming work of 29–31
August made the strip *correct* (no car drawn twice, no stale row) without making
it *agree* with the reply.

**Where.** `backend/app/agent/runner.py::_card_anchor` and the streaming loop
around it.

**Why it was left.** Fixing it means the composer nominates the cards — the strip
becomes an output of the answer rather than a side effect of the lookups — and
that is a change to what a turn emits, with a frontend contract on the other end
of it. Too wide for a bug-fix branch.

**FIXED upstream by `8776775` ("choose the card strip once, where the answer is
composed", 31 August 2026) — this entry just never caught up.** On the
multi-agent path the specialists' `search_vehicles` events no longer draw cards
as they stream (`turn.py::CURATED` defers the drawing; the results still reach
Aisha in full as evidence). `graph.py::_select_cards` then picks the strip ONCE
at compose time — round-robin across the per-clause searches, dedupe AFTER
selection — and the same list is both emitted to the screen and written into
Aisha's payload as `cards_shown`, whose prose orders her to lead with those
cars. Prose and strip cannot disagree because they are the same list read twice.

**Two caveats, recorded so they are not rediscovered.** The single-agent
fallback (`MULTI_AGENT_ENABLED=false`) still draws in stream order — deliberate,
that path is byte-frozen as the kill switch. And nothing in `tests/` pins
`_select_cards`, `CURATED`, or `cards_shown`: the fix is live but unprotected,
and a refactor could regress it to stream order with the suite green. Writing
those tests is the remaining (optional) work.

---

## 5. `.env` leaks flags into pytest — 10 failures

**Symptom.** `pytest` fails for a developer who has run the app, and passes for
one who has only ever done what the README says (`cp .env.example .env`, which
sets both flags to `false`). The suite's result depends on the developer.

**Evidence.** `backend/.env` carries the dev settings:

```
MULTI_AGENT_ENABLED=true
RETRIEVAL_ENABLED=true
```

`config.py` reads them at import, so the suite runs against the multi-agent shape
while ~1,500 of its tests are the **single-agent contract** — the contract the
migration exists not to break. Measured 31 August:

```
with the .env as it stands   12 failed, 1641 passed   (5m31s)
same tests, flags forced off  2 failed,  115 passed   ← the 2 are issue 6
```

**Ten of the twelve are the flags**, and the mechanism is visible in the captured
log of every one of the graph failures:

```
WARNING: app.agent.tool_node: refusing 'lock_slot': not bound to this caller
WARNING: app.agent.tool_node: refusing 'create_booking': not bound to this caller
```

That refusal is **correct** — with the cascade on, the commercial writes are the
orchestrator's and Aisha does not hold them. The scripted single-agent tests hand
her those exact tools, so they fail by design, and nothing in the run says so.
Two more (`test_multi_agent_is_off_by_default`, `test_disabled_by_default`) assert
the defaults the `.env` has overridden.

This is the same trap S7e recorded and pinned for `ROUTER_ENABLED`. The
multi-agent and retrieval flags were never given the same pin.

**Where.** `backend/tests/conftest.py` — where `ROUTER_ENABLED` is already pinned.

**Why it was left.** It is small and safe, but it changes what every test in the
suite runs against, and doing that in the same branch as six behavioural fixes
would make a red build ambiguous. Do it alone, on a green tree.

**Careful of the obvious over-fix.** Two of the ten (`test_multi_agent_is_off_by_default`,
`test_disabled_by_default`) exist to assert the *shipped default*, and a blanket
`monkeypatch` in `conftest.py` would make them pass without testing anything.
`ROUTER_ENABLED`'s pin has one test asserting the pin itself; do the same here.

---

## 6. `/health` 404s, so every deploy fails its health gate

**Symptom** (as filed). A deploy to the test VM restarts the service, waits 30
seconds, and exits 1 with a page of journal logs — on a service that is running
perfectly.

**Evidence.** `@app.get("/api/health")` at `app/main.py:262` is the **only**
app-level GET route in the file, so both `/health` and `/` 404. These are the two
failures that survive with the flags of issue 5 forced off:

```
FAILED tests/test_health.py::test_health_returns_ok        assert 404 == 200
FAILED tests/test_health.py::test_root_returns_service_name assert 404 == 200
```

Three more things ask for the bare path anyway:

```
scripts/remote-deploy.sh:46   curl -sf http://127.0.0.1:8000/health   # x15, then exit 1
README.md                     "...then polls /health until it returns 200"
backend/tests/test_health.py  asks for /health, and for service == "digycorp-cx-agent"
```

`remote-deploy.sh` retries fifteen times at two-second intervals and then fails
the deploy. The scaffold's `test_health.py` also asserts a service name the
ported `main.py` does not use — it is titled "Elite Test Drive Booking API".

**Where.** Five places name the route and one serves it:
`backend/app/main.py` (serves `/api/health`), `scripts/remote-deploy.sh`,
`backend/tests/test_health.py`, `README.md`, `docs/INFRASTRUCTURE.md` (*"proxies
`/api` and `/health` to uvicorn"*), and the `<ENV>_HEALTH_URL` secret in
`.github/workflows/deploy.yml`.

**FIXED on `fix/health-route`, 31 August 2026.** Open decision 5 is closed with it.

`main.py` now serves `/health` and `/` beside the existing `/api/health`, all
three from one `_liveness()` helper. **Serving both paths was the fix, not
choosing between them**, for a reason that only turned up on inspection: the two
deploy scripts poll *different* paths and both are load-bearing —

```
scripts/remote-deploy.sh:46       /health       ← this repo's deploy
deploy/inherited/deploy-core.sh   /api/health   ← Elite's, inherited at S2
```

— and the workflow's own gate reads `${ENV}_HEALTH_URL` from a repository
secret, so no edit to a script could guarantee that gate points anywhere real.
A route answering on both paths is correct whichever the secret names.

Three further decisions taken, each recorded in the code:

- **`status` was added to `/api/health`, nothing removed.** Elite's script reads
  that body; `api` and `database` keep their exact meaning.
- **Still always HTTP 200, including when the database is down.** Both gates
  treat non-2xx as "roll back", and a DB outage is not a deployment fault — it
  wants a different person. `status: degraded` carries the truth in the body.
- **`_SERVICE_NAME = "digycorp-cx-agent"`** — the deployed service's name, not
  the product's. `FastAPI(title="Elite Test Drive Booking API")` is untouched:
  a machine asking what runs here gets the machine's answer, a person reading
  `/docs` gets Elite's.

**The guard matters more than the fix.** `test_the_paths_the_deploy_scripts_poll_are_served`
reads the paths *out of the deploy scripts* and asserts the app routes each one.
A hardcoded list would have been a second copy of the same fact, drifting
silently — which is precisely how this happened: `remote-deploy.sh` and
`main.py` were each individually correct for months.

---

## 7. Finance's CALLBACK prose instructs a write it cannot propose

**Symptom.** A customer who says "I want to buy it" gets figures and no callback.

**Evidence.** `backend/app/agents/finance/v1.md` declares:

```yaml
proposes: []
```

with the reason stated right above it — `request_human_callback` is a write and
writes are Aisha's, so `write.py` would refuse it from a sub-agent. But the
`WHERE_YOU_STOP` section of the same file still tells the agent to *"propose a
human callback"*, and ends with the sharpest line in the card:

> *stranding somebody who has decided to buy is the worst outcome available to
> you.*

So the prose instructs an action the card forbids and the write path would
reject. The card's own comment flags it: *"The CALLBACK prose below still
instructs one; see the open question."*

**Where.** `backend/app/agents/finance/v1.md`.

**Why it was left.** Three possible answers and they are not equivalent: allow
finance to propose the callback and add it to `write.py`'s action map; keep the
prose but reword it so the *finding* asks Aisha to offer a callback; or drop the
instruction. The first widens the write surface, which was the whole point of the
split. This is a decision, not a bug.

**FIXED on `fix/latency-floor`, 1 September 2026 — the second answer.** The
deciding evidence: every piece of the callback machinery already exists on
Aisha's side, multi-agent path included. Her card declares
`request_human_callback` ("rule 13's exit"), `aisha_schemas()` serves it, the
tool's reason enum already has `price_negotiation` and `finance`, and her base
prose already says *"the moment a figure has to be committed to, it belongs to a
human colleague, and a callback is how you get them there."* Widening the write
surface would have duplicated a tool she holds; dropping the instruction would
have stranded the decided buyer. So `WHERE_YOU_STOP` now tells finance to flag
the moment **in its note** — "customer wants to negotiate/buy — needs a human
callback" — and states plainly it cannot arrange one, so the note never reads
back as a confirmation. Aisha reads the flag and arranges it with the tool and
rules she already has. Two tests in `tests/test_subagent_tools.py` pin the
reword. T6.1's finance rewrite is untouched: this removes a contradiction, it
authors no money policy.

**Related:** the finance body is thin on purpose — its real guardrails are T6.1,
still unwritten.

---

## 8. Accessories became reachable, and they have no Arabic

**Symptom.** An Arabic conversation gets an accessory price and an English
sentence describing it.

**Evidence.** `services/accessories.py::_row` returns `name_en` and
`description_en`; the `Accessory` model has no Arabic columns, because Elite's
"List of accessories and pricing" sheet supplied none.

**Where.** `backend/app/services/accessories.py`, `backend/app/models.py`,
and Elite's workbook.

**Why it was left.** This was already **open question 5** for Elite in the
architecture doc, filed when nothing read the table. It stops being theoretical
now that a tool does: as of 31 August a customer can actually ask, and in Arabic
they will get a half-translated answer. Needs the Arabic strings from Elite, then
a schema change.

---

## Already recorded elsewhere — not repeated here

- **T0.1 baseline not re-run** against a correct fixture (T8.4) —
  [architecture §6](elite-multiagent-architecture.md#6-what-is-not-done-what-was-unreachable-and-what-threw-its-answer-away).
- **Aisha has no prompt prose about delegating** — same section.
- **~60 brochure marketing pages unread**, three files `draft-partial` — same.
- **T3.6's routing threshold calibrated on card utterances, not real briefs** —
  same.
- **The other four questions for Elite** (the 80% financing base, Ramadan Value
  Drive dates, RTA vs the other emirates, the T1 fuel-economy disagreement) —
  [architecture §7](elite-multiagent-architecture.md#7-open-questions-for-elite).
- **S8 and S9** — Postgres, Salesforce, Redis, pgvector, guardrails, infra —
  [MIGRATION.md](MIGRATION.md).
- **Rotate the LangSmith key** — MIGRATION.md, Security.

---

# Deployment — found 31 August 2026

The app reached the test VM for the first time since S1 replaced `app/`. Nine
faults surfaced between the merge and a working page; **eight are fixed and in
`test`**. What follows is what those left behind.

Full pipeline description, and the table of all nine with causes, is
[DEPLOYMENT.md §5](DEPLOYMENT.md#5-failures-this-pipeline-has-actually-produced).

## 9. A green CI can mean nothing was checked

**Symptom.** A ✅ on `test` that ran no tests.

**Evidence.** `ci.yml` gates both build jobs on `dorny/paths-filter`:

```yaml
backend:   ['backend/**', '.github/workflows/**']
frontend:  ['frontend/**', '.github/workflows/**']
```

A commit touching only `scripts/` or `docs/` matches neither, both jobs are
skipped, and the run reports success. Four of today's merges were exactly that
shape. **Two real test failures have been sitting latent behind those green
ticks** — see #10.

`ruff` running before `pytest` compounds it: a lint failure also produces a red
build in which the suite never ran, which reads as "tests failed".

**Where.** `.github/workflows/ci.yml`.

**Why it was left.** The path filter is doing its job — it exists so a
frontend-only PR does not pay for the backend suite. The fix is not to remove it
but to make "skipped" legible: a required status check that distinguishes *passed*
from *not run*. That is a branch-protection decision, not a code change.

## 10. Two guardrail tests contradict the shipped handoff wording

**Symptom.** None visible. They fail only when something in `backend/` changes,
which is why four green runs went past them.

**Evidence.**

```
FAILED test_phase6_guardrails.py::test_the_honest_version_is_supplied_not_just_the_ban
       assert 'they will be in touch' in <aisha's rules>
FAILED test_phase6_guardrails.py::test_the_rewrite_introduced_no_em_dashes_into_aishas_rules
       AssertionError: Be careful what you promise
```

The human-takeover work rewrote the handoff paragraph to offer a live chat join
*or* a callback. The new text no longer contains the literal `'they will be in
touch'` one test pins, and it uses em dashes the other bans.

**Where.** `backend/app/agents/aisha/v1.md` (or the prompt block carrying
`Be careful what you promise`), `backend/tests/test_phase6_guardrails.py`.

**Why it was left.** It is a genuine two-way decision and not mine to take: the
prose is deliberate and reads better, and the tests are deliberate too — the
em-dash ban exists because em dashes in Aisha's rules were a real regression
once. Either the tests are updated to the new wording, or the wording moves back
toward what they assert.

## 11. The nginx config for the docker path and the VM path can still drift

**Symptom.** A routing bug that appears in exactly one deployment shape.

**Evidence.** This already happened, in the direction nobody expected:
`frontend/nginx.conf` (docker) was correct for months while the VM's
hand-written config silently 404ed every `/api` route. The VM config is now
version-controlled in `deploy/nginx/`, which fixes *that* instance — but there
are now **two** files describing the same routing, and nothing compares them.

`frontend/nginx.conf`'s own comment predicted this class of failure and still
names only one direction of it.

**Where.** `frontend/nginx.conf`, `deploy/nginx/digycorp-app.conf`.

**Why it was left.** The honest fix is one source of truth with the upstream and
document root templated per environment, which means changing how the docker
image is built. A cheaper interim: a test asserting the two files declare the
same set of `location` blocks with the same `proxy_pass` trailing-slash
behaviour.

## 12. Redis is configured and absent

**Symptom.** `retrieval cache unavailable — the index will be built in process`
on every boot, and ~9s added to every restart while 228 chunks are re-embedded.

**Evidence.** `.env` carries `REDIS_URL=redis://localhost:6379/0`; nothing is
listening on 6379 on the VM. The fallback is graceful and correct — it just runs
every time.

**Where.** the VM, and `docs/INFRASTRUCTURE.md`'s service table.

**Why it was left.** One `apt install redis-server` plus a systemd enable, but it
is a new service on a 2 vCPU / 4 GB box that also holds a 118 MB model in
process, so it wants measuring rather than assuming.

## 13. The morning auto-start is still broken, and now masked

**Symptom.** The VM will not come up on its own. Today it was found deallocated
at 18:50 PKT, inside its documented 10:00–20:00 window.

**Evidence.** The Automation Account's managed identity still lacks **Virtual
Machine Contributor** on the resource group — the long-standing S9 item in
`INFRASTRUCTURE.md`, which needs an Owner or User Access Administrator to grant.

**Auto-shutdown is currently disabled**, so the VM stays up and this cannot
bite — but it also means the box now bills around the clock, and the day
somebody re-enables the shutdown, the morning start fails again.

**Where.** Azure RBAC; command is in `INFRASTRUCTURE.md`.

**Why it was left.** It needs a permission this project's account does not hold.
