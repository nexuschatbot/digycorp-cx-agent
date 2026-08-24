# Elite Dealership AI — Multi-Agent Architecture

**Version 1.2 — August 2026** (verification pass: Salesforce cutover step, realtime scope boundary, routing/escalation split, anchor caching, schema + regex fixes)
**System:** "Aisha" — Elite Group's assistant for Jetour sales, test drives, and aftersales in the UAE
**Stack:** Python · LangGraph · Anthropic API (Claude Haiku + Sonnet) · Postgres · Redis

---

## 1. Executive summary

The current system is a single production agent (test drive booking, chat + voice + phone, bilingual English/Gulf Arabic) driven by one ~20k-token system prompt with server-side injected context anchors. This document specifies its evolution into a **supervisor-of-specialists** multi-agent system: one cheap router in front of a small set of specialist agents, each owning a narrow journey and a narrow tool set, sharing one persona base, one session state, and one memory layer.

Design verdicts, decided by evidence from production systems (AWS Bedrock multi-agent, Google Dialogflow mega-agents, Sierra, Decagon, Intercom Fin, LangChain's own benchmarks):

1. **One flat routing level, never a hierarchy** at this scale. Both AWS and Google cap flat supervisors at ~10 sub-agents; two-level routing compounds errors (0.95 × 0.95 ≈ 0.90) and adds latency.
2. **Fewer agents than topics.** Agents are split by *tools and data ownership*, not by subject. Target: 2 agents at launch (Sales, Bookings), growing to 4–5 max (add Financing, Support) only when a domain accumulates its own tools.
3. **Routing is sticky and stateful.** The active agent stays active by default; a switch requires a confident, explicit intent change. The router reads session state, never just the last message.
4. **Guardrails and evals are v1 scope, not phase 2.** Research on production failures shows systems fail on specs, verification, and injection — not on topology choice.
5. **The existing prompt prose, anchors, tools, and tests migrate verbatim.** Only the assembly (prompt composer, router, tool binding) is new code. Battle-tested sentences are never re-authored.

---

## 2. Architecture diagram

```
                        ┌─────────────────────────────────────────┐
                        │                CHANNELS                  │
                        │   Chat (web/app)   Voice (STT→TTS)       │
                        │   Phone (telephony / realtime S2S)       │
                        └────────────────────┬────────────────────┘
                                             │  raw user message
                        ┌────────────────────▼────────────────────┐
                        │             INGRESS PIPELINE             │
                        │  lang detect · injection screen · PII    │
                        │  flag · trace_id issued · anchors built  │
                        └────────────────────┬────────────────────┘
                                             │  message + state
                        ┌────────────────────▼────────────────────┐
                        │            ORCHESTRATION                 │
                        │  ┌────────────┐   ┌──────────────────┐  │
                        │  │  ROUTER    │◄──┤  SESSION STATE    │  │
                        │  │  (Haiku)   │   │  current_agent,   │  │
                        │  │  sticky +  │   │  facts, bookings, │  │
                        │  │  confidence│   │  held_slot, lang  │  │
                        │  └─────┬──────┘   └──────────────────┘  │
                        │        │ low confidence → FALLBACK       │
                        │        │ (clarify once / human callback) │
                        └────────┼────────────────────────────────┘
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
      ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
      │ SALES agent  │   │ BOOKINGS     │   │ (later)      │
      │ (Sonnet)     │   │ agent        │   │ FINANCING /  │
      │ discovery,   │   │ (Sonnet)     │   │ SUPPORT      │
      │ vehicles     │   │ book/move/   │   │              │
      │              │   │ cancel       │   │              │
      └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
             │    tool calls through EGRESS GUARD   │
             │    (per-agent allowlist · confirm    │
             │     sensitive writes · validation)   │
              ┌──────────────────▼──────────────────┐
              │              TOOL LAYER              │
              │  search_vehicles (SQL+semantic)      │
              │  get_available_slots (live only)     │
              │  create_booking · reschedule ·       │
              │  cancel · find_or_create_customer ·  │
              │  record_consent · save_memory ·      │
              │  request_human_callback              │
              └──────────────────┬──────────────────┘
              ┌──────────────────▼──────────────────┐
              │            MEMORY & DATA             │
              │                 ┌──────────────────┐ │
              │  Postgres:      │ Redis cache      │ │
              │  · checkpointer │ reads only, TTL  │ │
              │  · trace table  └────────┬─────────┘ │
              │  · pgvector              ▼           │
              │    (embeddings) Salesforce (CRM):    │
              │                 customers, bookings, │
              │                 consent, inventory   │
              │                 (writes go direct +  │
              │                  invalidate cache)   │
              └──────────────────┬──────────────────┘
                                 ▼
              ┌─────────────────────────────────────┐
              │  OBSERVABILITY — LangSmith traces,   │
              │  TurnTrace table, routing evals in   │
              │  CI, cost per conversation           │
              └─────────────────────────────────────┘
```

**Reading the diagram:** layers are stacked by dependency (who calls into what), not by time. The actual timeline of one turn is: read memory → build anchors → route → agent works through tools → write memory → reply.

---

## 3. Technology stack

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **LangGraph** (Python) | Already in use; graph/state/checkpointer model fits; supervisor + Send API for multi-intent |
| LLM — router | **Claude Haiku** (`claude-haiku-4-5`) | Classification-grade task; benchmarked >90% routing accuracy at ~350 ms in AWS's enterprise study |
| LLM — specialists | **Claude Sonnet** (`claude-sonnet-4-6`) | Tool-calling reliability + bilingual register quality |
| API framework | **FastAPI** + WS gateway | Already in use for chat + realtime |
| Business data (system of record) | **Salesforce** (CRM) | Customers, leads, bookings, consent, inventory — accessed only through a thin `salesforce_service` layer |
| Cache | **Redis** in front of Salesforce | Read-through with TTLs; protects the 24h API quota and voice latency; invalidated on writes |
| Conversation machinery | **Postgres** via `langgraph-checkpoint-postgres` | Checkpointer threads, `turn_trace`, embeddings — deliberately kept out of Salesforce to save API quota |
| Semantic search | **pgvector** extension | Hybrid search on an inventory mirror/embeddings — no second vector datastore to operate |
| Embeddings | Any strong multilingual model (must handle Arabic) | Powers the hybrid inventory search + future knowledge layer |
| Voice | Existing STT→TTS pipeline + realtime S2S mode | Unchanged; compact persona per agent for realtime TPM limits |
| Tracing | **LangSmith** + own `turn_trace` table | LangSmith for debugging; own table for aggregates, PDPL control, eval harvesting |
| Testing | **pytest** (existing behavior tests) + routing eval suite in CI | Green tests define "migration done" |
| Config/deploy | Existing (env-driven `config.py`, GitLab CI) | Prompts versioned in git, deployed like code |

Explicitly deferred (documented so nobody adds them early): fine-tuned BERT-class router (needs labeled traffic — revisit after ~5k conversations), semantic response cache, hierarchical routing (only past ~10 truly distinct domains), OKF-style knowledge layer (when warranty/manual content onboards — author concept-per-file Markdown from day one so adoption is trivial), langgraph-swarm (only if supervisor latency hurts voice after optimization).

---

## 4. The routing layer

### 4.1 Pipeline (cheap-first cascade)

Every turn runs the same cascade; most turns exit early.

1. **Pre-filter (rules, ~0 ms).** Obvious signals route instantly: a booking reference pattern (lenient: `(?i)td[\s-]*\d{4}[\s-]*[a-z0-9]+` — refs contain letters, e.g. TD-2026-HTXE, and customers write them in any format, so the filter must be case- and separator-tolerant), an explicit "cancel"/"reschedule" + identified booking → Bookings; a bare greeting → current agent or default.
2. **Sticky gate (rules, ~0 ms).** If a `current_agent` exists and the message is short/ambiguous (bare "yes", "ok", "تمام", a number, a time), stay — no model call at all. Follow-ups answer the active agent's question.
3. **Haiku classifier (~350 ms).** Structured output over the message + the last ~6 messages + session state:

```python
class RouteDecision(BaseModel):
    agent: Literal["sales", "bookings", "fallback"]
    confidence: float          # 0–1
    second_best: str
    margin: float              # confidence gap to second_best
    intents: list[str]         # supports multi-intent turns
```

4. **Decision policy (rules, ~0 ms):**

```
switch to a DIFFERENT agent   only if confidence ≥ 0.85 AND margin ≥ 0.15
confidence ≥ 0.90             → route
0.70 ≤ confidence < 0.90      → route if same agent; clarify (one short
                                 question) if it implies a switch
confidence < 0.70 OR margin
tiny with no active agent     → fallback: clarify once, else human callback
```

Rules are evaluated top-down: the switch gate is checked first, so a confident classification that implies a switch still needs the margin. Thresholds are starting points, tuned against the eval set and live traces — never hardcoded truths.

### 4.2 Router prompt contract

The router prompt contains, per agent: one line of scope, 3–5 example utterances (including Arabic and Arabizi-free Gulf phrasings), and 2–3 *negative* examples ("are there dealer add-on fees" → sales, NOT bookings, even mid-booking). One distinction the examples must teach, because the business rules (rule 13) depend on it: **factual price/fee questions answerable from tool data → sales; negotiation, discounts, financing, insurance and trade-ins → fallback/human callback** — routing those to Sales would invite the agent to answer deals it is forbidden to make. It receives the session-state note (current agent, vehicle in play, booking status, held slot) because "what about the weekend?" is unroutable without it. The router **never answers the user and holds zero business tools** — enforced by binding it no tools.

### 4.3 Multi-intent turns

"I want to book a test drive and also tell me your finance plans" → the router returns `intents: ["bookings", "sales"]`. The supervisor loop processes them **sequentially**: Bookings completes (writes results into session state), then Sales runs already knowing which car, and one combined reply goes out. Parallel fan-out via LangGraph's `Send` is a later optimization for voice — not v1, because parallel state merges are harder to debug. For mid-task consultation (Sales needs a payment estimate), expose a slim tool (`get_finance_estimate`) rather than an agent-to-agent conversation. Agents never free-chat with each other.

---

## 5. State, memory, and context

### 5.1 The three memory scopes

| Scope | Lives in | Lifetime | Contents |
|---|---|---|---|
| **Session state** | LangGraph state (working copy) | one conversation | `current_agent`, session_facts (name, mobile, vehicle, showroom, daypart), live_bookings, held_slot, booked_refs, detected lang, trace accumulator |
| **Thread persistence** | Postgres checkpointer | resumable per `thread_id` | full message history + state snapshots |
| **Customer profile** | Salesforce (cached in Redis, write-through) | across conversations (consented) | validated fields only: usual showroom, usual daypart, preferred car, care notes. Written via `save_memory`; two-step `forget_me` wipe |

### 5.2 The anchor system (carried over verbatim)

The existing anchors are the context-engineering layer and migrate unchanged: **date anchor** (dates injected in both ISO and spoken form — the model never does calendar arithmetic), **facts anchor** (identity restated every turn so it survives the trim window), **bookings anchor** (live bookings read from the DB each turn; "knowing vs describing" split preserved; authorship attribution on bookings created this conversation), **language anchor** (server-side per-turn detection; hard directive for pure-script messages, soft for mixed), **voice anchor** (register compression on call turns). Every agent's prompt gets the same anchors appended — which is precisely what makes handoffs lossless.

### 5.3 Handoff object

A switch from one agent to another passes no transcript. The receiving agent gets the same anchors plus a one-line handoff note in state:

```json
{
  "from_agent": "bookings",
  "to_agent": "sales",
  "reason": "customer asked about add-on fees (pricing intent)",
  "last_user_message": "are there any dealer add-on fees?"
}
```

Everything else it needs (customer, vehicle, booking refs) is already in session state. Handoff/routing messages are stripped from agent context (LangChain-benchmarked: this plus verbatim forwarding ≈ 50% reliability gain on supervisor systems).

### 5.4 Memory hygiene rules

Profile writes accept **validated structured fields only** — never free-text summaries of what a user said (memory-poisoning defense). Cache is **read-only with TTLs** (inventory 5–15 min, static content hours; profile is write-through). Never cached: slot availability, prices at commitment, any write. Traces and threads carry PII → retention policy + LangSmith input/output masking per PDPL.

---

## 6. Agents

### 6.1 Prompt composition

One shared base + per-agent journey + anchors, assembled per turn:

```python
def build_prompt(agent: str, *, today, session_facts, current_lang,
                 live_bookings, held_slot, voice_call) -> str:
    return (
        BASE_BODY                 # persona, tone, language, output format,
                                  # grounding, one-question, guardrails,
                                  # memory conduct, escalation
        + JOURNEY[agent]          # only this agent's rules
        + date_anchor(today)
        + facts_anchor(session_facts)
        + bookings_anchor(live_bookings, held_slot, session_facts)
        + lang_anchor(current_lang)
        + (VOICE_ANCHOR if voice_call else "")
    )
```

Per-agent prompt size drops from ~20k to ~8–10k tokens. The system's own measurements show rule-following degrades under prompt length (date conversion: 7/12 wrong under the long prompt, 0/12 with the fix; the compound-question live defect; the realtime TPM burn) — the split attacks the cause.

### 6.2 Agent roster (launch → growth)

**Sales (launch):** discovery ("FINDING THE RIGHT CAR"), vehicle detail discipline (search vs `get_vehicle_details`, never-say-popular, narrowing-is-a-new-search, feature_query/catalogue_digest), showroom Q&A, visit personalisation. Tools: `search_vehicles`, `get_vehicle_details`, `get_showrooms`, `nearest_showroom`, `find_or_create_customer`, `save_memory`, `request_human_callback`.

**Bookings (launch — the current agent's core):** slot flow (branch stickiness, back-to-back second drives, lock→recap→licence→consent→create, PDPL consent separation), reschedule (never-ask-why, same-branch default), cancel (single retention offer), closing. Tools: `get_available_slots`, `lock_slot`, `create_booking`, `reschedule_booking`, `cancel_booking`, `get_booking`, `identify_by_reference_or_mobile`, `find_or_create_customer`, `record_consent`, `acknowledge_licence_reminder`, `save_memory`, `forget_me`, `request_human_callback`.

**Financing (later):** payment plans, estimates — currently an escalation topic; becomes an agent only when it owns real tools. **Support (later):** warranty, complaints, order status — same criterion. Until then, both remain fallback/escalation paths.

**Split criterion (standing rule):** an agent earns existence by owning distinct tools and data — never because a topic sounds separate. Split an existing agent only when it exceeds ~8 tools or its measured accuracy drops.

### 6.3 Per-agent discipline (from failure-mode research)

Each agent carries: an explicit **done condition** (Bookings: booking created/moved/cancelled and confirmed → one closing offer), a **step cap** (`recursion_limit` ≈ 15; hitting it is a routing/prompt bug, not a limit to raise), and **verbatim forwarding** — the supervisor never paraphrases a specialist's reply (kills the telephone-game failure).

---

## 7. Guardrails & security

**Ingress (code, before any model):** language classification (exists), prompt-injection screening (pattern + lightweight classifier; suspicious turns get flagged in state — the agent still answers, guarded, but writes require confirmation), PII flagging for trace masking. Voice hardening (audio-anomaly screening pre-STT; hidden-audio injection succeeds 79–96% against unguarded pipelines) is a later phase, documented now.

**Egress (code, around every tool call):** per-agent tool allowlists enforced by binding (Sales physically lacks `create_booking`); Pydantic validation on all tool I/O (hallucinated-parameter defense — exists); step-up confirmation for sensitive writes (recap-confirm before `create_booking` — exists — extended to reschedule/cancel confirms); consent writes fail-safe (ambiguous yes → no consent recorded — exists).

**Prompt layer (defense-in-depth, carried verbatim):** HOLDING THE LINE — persona attacks, other customers' data, data minimisation, no promises beyond scope, off-limits topics, rudeness/flirtation/minors handling.

**Resilience:** retries with backoff + per-call timeouts on LLM and tool calls; circuit breaker to a degraded mode (phone: "let me take your number and have someone call you" — never dead air); Anthropic rate limits monitored per model tier.

---

## 8. Salesforce integration

Salesforce is the **system of record for business data**: customers and leads (standard objects), test-drive bookings (Service Appointments or a custom object), consent records (PDPL audit), and inventory. Postgres deliberately keeps only conversation machinery (checkpointer, `turn_trace`, pgvector) so CRM API quota is never spent on plumbing.

**Access pattern — one service layer.** Tools never call the Salesforce API directly. Everything goes through `salesforce_service.py`, which owns auth, retries/backoff, timeouts, the cache, and quota accounting. This keeps the tool layer testable (mock one service) and makes the API meter observable in one place.

**Read path:** tool → Redis (hit? return) → Salesforce REST → write to Redis with TTL → return. TTLs: customer profile write-through (never stale), inventory 5–15 min, static content (showrooms, fee tables) hours. **Never cached:** slot availability and anything at commitment moment — those calls always go live and are budgeted for.

**Anchor reads:** the bookings anchor reads the customer's live bookings on *every* turn — hitting Salesforce each time would dominate the quota. Those reads are cache-first with **write-through**: every `create/reschedule/cancel_booking` updates the cached entry in the same operation, so the anchor is always current without an API call, and a short TTL (60–120 s) covers edits made inside Salesforce until CDC invalidation lands.

**Write path:** straight to Salesforce, then invalidate/update the affected cache keys in the same operation. Production hardening: subscribe to Salesforce **Change Data Capture / Platform Events** so records edited inside Salesforce (a salesperson updating a customer) invalidate the cache immediately rather than waiting for TTL expiry.

**Why the cache is load-bearing here, not optional:** Salesforce meters API calls per 24 hours per org, and a REST round-trip runs 200–800 ms versus single-digit ms for Redis. The cache is simultaneously the quota shield and the voice-latency fix. Track **Salesforce API calls per conversation** and **cache hit rate** in `turn_trace` from day one — quota burn is a first-class metric alongside token cost.

---

## 9. Observability & evaluation

**LangSmith tracing** on every run (router decision, agent, tool I/O, tokens, latency as a navigable tree), with PII masking configured.

**Own `turn_trace` table** (one row per turn): `trace_id, conversation_id, channel, user_message, detected_lang, router_intent, router_confidence, margin, routed_to, sticky_applied, tool_calls(jsonb), agent_response, tokens_in/out, sf_api_calls, cache_hits, latency_ms, created_at` + retention job. Powers the dashboards that matter: misroute rate, tie rate, cost/conversation, Salesforce API calls per conversation + cache hit rate, p50/p95 latency per channel, escalation rate, tokens trend.

**Routing eval suite (CI gate):** starts at ~30 labeled utterances — booking intents, discovery intents, ambiguous follow-ups ("what about the fees?", "and the weekend?"), Arabic + English + mixed, consent-adjacent traps. Every observed misroute in `turn_trace` becomes a new case. Target ≥ 92% routing accuracy, < 5% false-switch rate; the suite runs on every prompt/threshold/model change, alongside the existing behavior tests (one-question, introduction, chat register, booking sequence), which all stay green throughout.

**Silent-failure defense:** weekly sample of ~20 real conversations human-reviewed — the only known catch for misroutes that "work" but lose sales.

**Realtime scope (v1 boundary):** the router + multi-agent graph serves chat and the STT→TTS voice pipeline. The realtime speech-to-speech channel bypasses the graph — it keeps the single compact persona (`realtime_instructions`) and calls the same service layer through its tools. Routing inside realtime is explicitly out of scope for v1; revisit only if realtime traffic needs the Sales journey.

**Voice latency budget:** p50 < 400 ms, p95 < 800 ms to first audio. On the voice hot path the cascade prefers pre-filter + sticky exits; Haiku fallback only when needed; Sonnet streams to TTS.

---

## 10. Migration plan

Rule zero: **port, don't re-author.** Prose, comments, anchor functions, tools, and tests move verbatim; only assembly code is new. Existing tests green at every step.

1. **Step 0 — Tests first.** Port the test suite into the new repo/layout. Red until code arrives; green = done.
2. **Step 1 — Extract the base.** Split the prompt file into `agents/base.py` + `agents/bookings.py` + `agents/sales.py` (sections moved whole, comments included). `build_prompt()` composer. Behavior byte-identical for the booking flow.
3. **Step 2 — Bookings agent behind a dummy router.** Graph gains a router node that always routes to Bookings. Wire `turn_trace` + LangSmith now, while there's nothing to misroute. The current database stays in place — the agent refactor and the data migration are never the same step.
4. **Step 3 — Sales agent + real routing.** Bind per-agent tools. Haiku classifier + sticky + thresholds. Routing eval suite lands in CI before real traffic hits the switch.
5. **Step 4 — Salesforce cutover.** Build `salesforce_service.py` + the Redis cache **together** (the cache is load-bearing for quota, not an optimization — see §8) behind the existing tool signatures, so no agent or prompt changes. Map objects, dual-run reads against the old DB to verify parity, then flip writes. Tools are the seam: they never knew what was behind the service layer.
6. **Step 5 — Guardrail hardening.** Ingress injection screen; egress confirmations extended; degraded mode; cost + quota alerts.
7. **Step 6 — Optimize on evidence.** pgvector hybrid search on inventory; CDC-based cache invalidation; realtime compact personas per agent; revisit thresholds monthly against traces.

Each step ships independently; the system is releasable after every one.

---

## 11. Standing decisions (so future-you doesn't relitigate)

Flat routing until >10 truly distinct domains. Agents split by tools, not topics. Sticky by default; switching needs confidence + margin. Never guess on ambiguity — clarify once or escalate. Supervisor forwards verbatim. Facts from tools only; calendar and language computed server-side. Profile memory: validated fields, consented, two-step forget. Cache reads only, TTL everything, availability always live. Prompts in git; eval suite gates every prompt change. When the model must restate an injected fact in another format, inject that format too. Salesforce is reached only through the service layer; every read is cache-first; API quota is tracked per conversation.
