# Observability — LangSmith tracing (2026-07-24)

> **Spoke of `../CLAUDE.md`.** Moved out of the root file on 2026-08-05, wording
> unchanged. Root keeps the go-live obligation (uncomment the two HIDE flags) and
> points here.

Agent tracing on the **deployed VM only**: one trace per chat turn, named
`chat-turn`, containing the graph's `agent`/`tools` nodes, the routing edges, and
the model call inside each generation. Config is `LANGSMITH_TRACING` /
`_API_KEY` / `_PROJECT` (project `elite_group_test_drive_demo` on prod;
`elite_group_test_drive_dev` on the dev VM, so experiments never pollute the
production project), read from the environment's own deploy env file
(**`deploy/prod/.env`** / **`deploy/dev/.env`**), which that environment's
deploy script ships to its VM. (Until 2026-07-31 this was the root `.env`; that
file now serves local docker compose only and is never deployed.)

**Two plumbing facts, both of which fail SILENTLY when missed.** The
compose `backend.environment:` block is an explicit **allowlist**, not
`env_file`, so a `LANGSMITH_*` value in the shipped env file reaches the container
only if it is NAMED there — otherwise tracing is configured, believed, and dead.
And the vars are deliberately **absent from `elite-group-backend/.env`**, since
that is the file pytest loads: langchain-core instruments LangGraph
automatically, so a scripted graph test would export a trace of a fake
conversation to a live US endpoint — both a vendor call from the offline suite
(forbidden) and noise in the production trace project. `conftest.py`'s
autouse `_hermetic_demo_toggles` forces it off regardless, clearing langsmith's
`get_env_var` lru_cache and deleting the key, so filing discipline is not the
only thing standing between the suite and the network.

**The graph traces itself; THE MODEL CALL DOES NOT.** `_default_generate` is the
raw Anthropic SDK per AD-A — not a Runnable — so without help the trace shows
the node and a HOLE where the generation belongs, taking the prompt, tool
schemas, stop reason, token counts and cache hit rate with it.
`graph._traced()` wraps the client with `langsmith.wrappers.wrap_anthropic` on
both provider paths. Checked rather than assumed: the wrapper's sync stream
object iterates the RAW event stream (which `_default_generate` consumes on
purpose, to see `content_block_start` the instant a `tool_use` begins) and
passes `get_final_message()` through, so AD-A streaming, the per-delta cancel
poll (AD-E) and the markdown stripper are untouched. It returns the client
UNWRAPPED when tracing is off or wrapping fails — telemetry must never break a
live turn, the rule `_log_usage` already follows.

**`build_config` states `metadata: {session_id}` because it will NOT arrive on
its own.** Every turn builds a fresh graph and checkpointer, so every turn is
its own ROOT run and a six-turn booking reads as six unrelated traces unless
grouped; LangSmith groups on `session_id` in the root run's metadata, and
langchain-core 1.x narrowed `ensure_config`'s configurable→metadata copy to
`model`/`checkpoint_ns` only (older guidance assumes it copies everything).
Metadata is SERIALISED AND SENT and `configurable` is not, which is exactly why
`db` and `cancel_event` stay in the latter — the trusted-context rule is
unaffected.

**⚠️ A FOURTH D1 PDPL ENTRY, AND THE HEAVIEST.** A trace carries the WHOLE
conversation — customer name, mobile, doorstep address, booking ref, both sides
of every turn — to a US endpoint. The STT, TTS and WhatsApp entries each send
one utterance or one message; this sends the entire session, structured and
searchable. And **rule 12 cannot reach it**: `services/erasure.py` clears the
customer, bookings, notifications, transcripts, audit rows and the LangGraph
checkpoint, and has no way to delete a third-party trace — so a customer told
"we've cleared everything" would still have their conversation in a trace store.
Real users type real mobile numbers that really receive WhatsApp, so
"fictitious seed data" does not cover it.

**Decision 2026-07-24 (option A): full-text traces DURING BUILD, redacted BEFORE
real users are let in.** The switch is `LANGSMITH_HIDE_INPUTS=true` +
`LANGSMITH_HIDE_OUTPUTS=true`, sitting commented in `deploy/prod/.env` — read by
the SDK, no code. On, you keep every trace, every step, the tool order, timings,
tokens, cost and errors, and lose only the sentences. **Deferred, not waived:
uncomment both before going live.** To switch tracing off entirely,
`LANGSMITH_TRACING=false` and redeploy; the wrapper and the metadata go inert.
