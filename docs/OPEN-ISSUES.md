# Open issues — carried out of `multi-agent-migration`

> **One down.** #6 is fixed on `fix/health-route` — the entry is kept rather
> than deleted, because how a route and a deploy script stayed individually
> correct for months is the part worth remembering. Seven open.

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
| 2 | [The per-model-call latency floor](#2-the-per-model-call-latency-floor) | ~15s turns | measurement first |
| 3 | [`MAX_CASCADES_PER_TURN = 2` is an unproven guess](#3-max_cascades_per_turn--2-is-an-unproven-guess) | answer quality, unknown | one re-run |
| 4 | [The card strip follows search rank, not the answer](#4-the-card-strip-follows-search-rank-not-the-answer) | wrong car on screen | design |
| 5 | [`.env` leaks flags into pytest — 10 failures](#5-env-leaks-flags-into-pytest--10-failures) | CI trust | small |
| ~~6~~ | [~~`/health` 404s, so every deploy fails its health gate~~](#6-health-404s-so-every-deploy-fails-its-health-gate) — **FIXED**, `fix/health-route` | deploys | done |
| 7 | [Finance's CALLBACK prose instructs a write it cannot propose](#7-finances-callback-prose-instructs-a-write-it-cannot-propose) | a decided buyer stalls | decision |
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
