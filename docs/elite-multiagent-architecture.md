# Elite Dealership AI — Multi-Agent Architecture

**Version 2.1 — 29 August 2026.** Rewritten after Phases 0–8 were built; revised
once the cascade was actually connected. v1.2 was a *specification*; this is a
*description*, and where the two disagree the code is right and v1.2 is
superseded.

> **What v2.0 got wrong, recorded because the failure mode is the interesting
> part.** v2.0 said the cascade was "built, tested and switched off behind
> `MULTI_AGENT_ENABLED`", which read as though flipping the flag would turn it
> on. It would not have. `app/orchestrator/` was complete and thoroughly tested
> and **nothing in `app/` imported it** — the graph had two nodes, `agent` and
> `tools`, and no customer turn could reach a sub-agent. The flag gated a
> function no request called.
>
> A description written from the modules rather than from the call graph cannot
> see that, and neither can a test suite that exercises each part in isolation.
> Both were green throughout. §6 below now distinguishes *not built* from *built
> and not reachable*, because the second is invisible in a way the first is not.

**System:** "Aisha" — Elite Group's assistant for Jetour sales, test drives and
aftersales in the UAE
**Stack:** Python · LangGraph · Anthropic (Sonnet + Haiku) · SQLite/Postgres ·
Alembic · ONNX embeddings · Redis (cache only)

---

## 1. What is actually running

**The single agent is still the live path.** The multi-agent cascade is built,
wired into the graph, and switched off behind `MULTI_AGENT_ENABLED`. Nothing was
taken away to make room for it, which is why all 1,532 tests still pass against
the old shape.

Wired means specifically: Aisha holds an `emit_briefs` tool; a generation
containing it routes to an `orchestrator` node instead of the tools node; that
node runs the cascade and returns the findings as that call's *tool_result*, so
her next generation composes the reply through the existing streaming, script
guard, sanitiser and card anchors rather than a second path that would drift from
them. With the flag off she is handed the same 22 tools as before, byte for byte,
which is what keeps the ~14.8k cached prefix alive.

Two other switches gate the other new capabilities:

| flag | default | gates |
|---|---|---|
| `MULTI_AGENT_ENABLED` | off | Aisha → orchestrator → sub-agents. **Excludes voice even when on.** |
| `RETRIEVAL_ENABLED` | off | `search_knowledge` and the catalogue vector pass |
| `PROMPT_VERSIONS` | unset | per-agent prompt version override, no deploy needed |

---

## 2. The shape

```
Customer
   │
   ▼
AISHA (Sonnet) ── greeting · identity · consent · licence · memory · close
   │              she writes BRIEFS; she never learns the roster
   ▼
ORCHESTRATOR ──── select · order · execute · and every COMMERCIAL write
   │
   ├── SALES (read-only)      cars · variants · stock · brochures
   ├── TESTDRIVE (read-only)  showrooms · slots · bookings lookup
   └── FINANCE (read-only)    on-road price · EMI · handoff
```

**The write split is by KIND, not by layer.** A write that is an act of
*dialogue* — who you are, a yes, a licence acknowledgement, a remembered
preference, goodbye — belongs to whoever is talking, and only Aisha talks. A
write that is an act of *commerce* — holding inventory, creating, moving or
cancelling a booking — belongs to the orchestrator. Sub-agents write nothing at
all, and that is the single property that makes it safe to run them on Haiku: a
bad generation can *ask* for a booking, it cannot make one.

Enforced, not documented: `evals/differential.py` fails if any sub-agent card
holds a write tool, if Aisha holds a commercial one, or if a card proposes an
action `orchestrator/write.py` cannot execute.

---

## 3. Six things the build proved wrong about the specification

These are the entries worth reading. Everything else went roughly as v1.2 said.

### 3.1 Embedding routing does not work well enough to be primary

v1.2 assumed a cheap embedding router in front of the specialists, with a model
call only as a fallback. Calibrated against the cards' own utterances:

| rule | routes without a model call | of those, correct |
|---|---|---|
| `score ≥ 0.80` (as specified) | 56% | **83%** |
| `margin ≥ 0.02` (as built) | ~37% | **100%** |

**Why:** the three agent cards are **88% similar to each other** — they all
describe car-dealership work in the same register — so almost nothing is left to
express "choosing" vs "booking" vs "paying", and sentence *shape* swamps what
remains. `"which banks do you work with"` routed to TestDrive, because anything
phrased *"which ___ do you work with"* looks like a showroom question.

An absolute threshold cannot work: the whole score range is ~0.09 wide. The rule
is a **margin** — a question about this brief's own scores rather than a global
scale. The Haiku judge is the primary router; the embedding is a shortcut.

`evals/routing_calibration.py` is committed so the number is evidence rather
than a guess.

### 3.2 "Guardrails and evals are v1 scope" was right, and it applies to prompts

Prompts moved to `app/agents/*/v1.md`, editable without a deploy. That removes
the protection that used to come from "a prompt change is a code change is a
code review", so `app/agents/gate.py` replaces it: a version that has lost a
load-bearing rule, or lost a composed section, or names a tool the agent cannot
reach, is refused with the previous one left running.

### 3.3 The retention windows were declared and never enforced

`models.py` had carried retention columns since it was written and **nothing
ever read them**. `services/retention.py` now does — dry-run by default, and it
never touches bookings, customers or consent, which have their own legal basis.

### 3.4 "Forget me" was a partial delete that looked complete

Two gaps, both closed:

- **`turn_trace` was never swept.** It holds the customer's own message text and
  was declared in scope for erasure from the day it was written.
- **Transcripts from an unidentified session were unreachable.** `erasure.py`'s
  own docstring named this and said fixing it "means a schema change,
  deliberately not smuggled in here". Alembic removed the objection;
  `ConversationTurn.customer_id` exists and is back-filled across the session at
  identification.

### 3.5 The data had errors, and they reached customers

The workbook and seed disagreed with the brochures on facts a customer acts on:

- **The two X70s' seat counts were swapped in the seed**, and the comment in
  `catalogue._ranked_for` described the swap as fact. "Which is the seven-seater"
  answered with the wrong car.
- **Two marketing bullets quoted fuel economy and both were wrong** against
  Elite's own dataset.
- **`G700 Topr Fire`** is a typo for `Top Fire`, pinned by a test.
- **The T1's trim ladder is not Comfort → Luxury → Flagship.** Its Luxury is the
  4WD range-topper and costs more than its Flagship. Trim names never imply rank.

### 3.6 A live defect only a baseline could have found

Capturing 12 real conversations before changing anything (T0.1, ~$1.48) found
Aisha writing notes-to-self in the **third person** before tool calls — and
`graph.py` seals pre-tool text, so it was **delivered**: *"They've chosen the
petrol T2 (already confirmed above)."* No test would have caught it. Fixed in
Phase 6.

---

## 4. Data

Nine models, **fifteen variants**, 330 stock rows across 5 branches, 7,544 units.

| lives in | why |
|---|---|
| **Database** — models, variants, stock, on-road pricing, finance banks, accessories | something filters, sorts or calculates on it |
| **Markdown** — 9 brochures, 4 knowledge files | it is read out and explained |
| **LFS** — 10 brochure PDFs, the embedding model | vendor artefacts, replaced wholesale, over GitHub's 100 MB limit |

**Prices live in exactly one place**, at variant level. Two price sources is how
a customer is quoted one number in chat and another on the invoice.

---

## 5. Retrieval

`multilingual-e5-small` (int8, 384-dim) vendored beside `silero_vad.onnx`, run on
CPU by onnxruntime. No torch: a 2–3 GB dependency for one embedding call is the
wrong trade in an image that also runs the test suite.

228 chunks from 13 files. The chunker was **chosen by ablation**, not by taste —
paragraph chunks with a `file — heading` prefix score 11/13 top-1 and 13/13
top-3, where the same chunks without the prefix score 8/13. **The prefix is worth
+3**, because a bare paragraph from a spec table has no idea which car it belongs
to.

**Scores are compressed into 0.75–0.87 on this corpus.** Absolute thresholds are
meaningless anywhere in this system; rank and margin are the only usable signals.

---

## 6. What is not done, and what was done but unreachable

Split up, because v2.0 conflated them and that is how the wiring gap hid for
a whole phase. **Not built** is visible — the code is absent, and anyone reading for it finds
nothing. **Built and not reachable** is invisible: every test passes, every module
is complete, the imports all resolve, and the only symptom is a feature that does
nothing when you switch it on. A third category, **measured wrong**, is worse
again — it returns a number.

### Not built

- **Voice stays single-agent.** Its 400 ms first-audio budget cannot absorb a
  fan-out; the measured Haiku round trip alone is 1,088 ms p50. `turn.enabled()`
  excludes it even when the flag is on.
- **Salesforce stays dormant** (`SF_ENABLED=false`), not deleted.
- **T0.4b:** ~60 brochure marketing pages unread, three files still
  `draft-partial` (`dashing`, `t1-i-dm`, `t2-idm`). The one that bites is the
  **T1 i-DM's missing seat count** — seeded null, so that car matches no seat
  query, which is visible in a test rather than hidden behind a guess.
- **Aisha has no prompt prose about delegating.** She has `emit_briefs` and its
  description and nothing in `base/` or `aisha/` about when to hand work over.
  Measured once and it read well; it has not been measured against a correct
  fixture.

### Built and not reachable — the class of defect, and how each was found

Four in a row, none of which any test could see, each found only by running the
thing end to end:

| what | how it hid | found by |
|---|---|---|
| `orchestrator/turn.py` never imported by `app/` | flag gated a function nothing called | reading the call graph |
| sub-agent cards named 5 tools that did not exist | `_schemas_for` skipped unknown names silently; **finance bound zero tools** and reported `blocked`, which looks like dependency ordering | counting declared vs bound |
| `orchestrator/write.py` never imported by `app/` | Aisha correctly held no commercial write, sub-agents correctly held none, and the component that owned them was connected to nothing — so **no booking could be created at all** | asking who calls it |
| sub-agents had no date anchor | resolved "Saturday" to a date 19 months stale and reported "no slots"; the date it used appears nowhere in the findings | one live smoke run |

All four are now closed. The guard against a recurrence is
`tests/test_subagent_tools.py`, which fails the build when a card declares a tool
nothing provides, and `log.error` where the silent `continue` used to be.

### Measurement, which is its own trap

- **The T0.1 baseline has not been re-run** against a *correct* fixture (T8.4).
  Two runs were spent before `evals/baseline._seed_db` was found to seed zero
  variants, zero stock and zero finance banks — it predates the variant-level
  catalogue. The single agent never noticed, because its tools are model-level.
  The sub-agents were starved, and Aisha's impeccable behaviour against a broken
  database (retry, apologise, escalate) read exactly like a broken agent.
  `tests/test_baseline_fixture.py` now asserts every sub-agent tool returns data
  against the fixture.
- **T3.6's threshold is calibrated on 32 short card utterances**, not on real
  briefs. `turn_trace.steps` records live scores for the re-run.

---

## 7. Open questions for Elite

1. **What is the 80% financed measured against** — list price or on-road? The
   sheet says "80% financing" without naming the base, and the choice moves a
   G700 Flagship instalment by ~AED 110/month. Built against the list price, per
   Reg 29/2011's "value of the car".
2. **"Ramadan Value Drive" has no dates at all** — current, historic or
   illustrative? Aisha naming a Ramadan offer in August would be odd.
3. **The registration timeline says RTA throughout**, which is Dubai's authority.
   Three of five branches are in Abu Dhabi, Ajman and Al Ain.
4. **The T1 brochure and the catalogue disagree on fuel economy** by roughly a
   factor of two.
5. **Accessories have no Arabic** — an Arabic conversation gets a price and an
   English sentence.
