# Elite Dealership AI — Multi-Agent Architecture

**Version 2.0 — August 2026.** Rewritten after Phases 0–8 were built. v1.2 was a
*specification*; this is a *description*, and where the two disagree the code is
right and v1.2 is superseded.

**System:** "Aisha" — Elite Group's assistant for Jetour sales, test drives and
aftersales in the UAE
**Stack:** Python · LangGraph · Anthropic (Sonnet + Haiku) · SQLite/Postgres ·
Alembic · ONNX embeddings · Redis (cache only)

---

## 1. What is actually running

**The single agent is still the live path.** The multi-agent cascade is built,
tested and switched off behind `MULTI_AGENT_ENABLED`. Nothing was taken away to
make room for it, which is why all 1,444 tests still pass against the old shape.

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

## 6. What is deliberately not built

- **Sub-agent graphs are not bound to Haiku.** `turn.execute` takes an injected
  runner; every test passes a stub. Binding it needs live calls.
- **The T0.1 baseline has not been re-run** against the new stack (T8.4) — same
  reason.
- **Voice stays single-agent.** Its 400 ms first-audio budget cannot absorb a
  fan-out; the measured Haiku round trip alone is 1,088 ms p50.
- **Salesforce stays dormant** (`SF_ENABLED=false`), not deleted.
- **T0.4b:** ~60 brochure marketing pages unread, five files still
  `draft-partial`. The one that bites is the **T1 i-DM's missing seat count** —
  it is seeded null, so that car matches no seat query, which is visible in a
  test rather than hidden behind a guess.

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
