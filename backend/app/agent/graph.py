"""The agent loop as a LangGraph StateGraph (AD-H).

Shape (AD-H, reversed to LangGraph on 18 Jul):

        START
          │
          ▼
     ┌─────────┐   has tool_use blocks   ┌──────────┐
     │  agent  │ ──────────────────────► │  tools   │
     │  node   │ ◄────────────────────── │  node    │
     └─────────┘      tool_result        └──────────┘
          │ no tool_use  (final text, OR the graceful
          ▼               fallback when a bound trips)
         END

- agent node: the model with the existing TOOL_SCHEMAS bound. In production it
  runs on the RAW Anthropic SDK via `client.messages.stream(...)` (AD-A, literal
  — never blocking create()). The call is behind an injectable `generate` so the
  whole graph is testable deterministically without an API key; the injection
  point is also where Stage 2 will forward token deltas to SSE.
- tools node: app/agent/tool_node — the custom REGISTRY dispatcher (audit choke
  point + trusted-context injection + correlation id). NOT the prebuilt ToolNode
  (AD-H: keep tools one source of truth).
- conditional edge: back to agent while the last assistant turn wants tools;
  otherwise END (== stop_reason != "tool_use").
- checkpointer: SqliteSaver, thread_id = session_id — persists the FULL block
  history incl. tool_use/tool_result, the session-state gap ConversationTurn
  cannot hold. Trusted context (db, session_id) rides in RunnableConfig, never in
  state, so it is never checkpointed and never reaches the model.

LOOP BOUND + ~60s CEILING (AD-D): both are checked at agent entry, before each
generation. When either trips, the node emits ONE graceful, honest fallback
message ("let me have a specialist follow up") and the empty-of-tools turn routes
straight to END — never a stack trace to the customer, never an unbounded loop.
"""

from __future__ import annotations

import logging
import operator
import re
import time
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agent.prompt import SYSTEM_PROMPT, system_prompt
from app.agent.sanitise import MarkdownStripper
from app.agent.tool_node import tool_node
# Read-only, for the bookings anchor built in _agent_node. The agent node reads
# these services and never writes through them — see the note at the call site.
from app.services import booking, recognition, slots
from app.config import (
    CALL_AGENT_EFFORT,
    CALL_AGENT_MODEL,
    AGENT_EFFORT,
    AGENT_MODEL,
    ANTHROPIC_API_KEY,
    AZURE_FOUNDRY_API_KEY,
    AZURE_FOUNDRY_RESOURCE,
    LANGSMITH_TRACING,
    USE_DIRECT_ANTHROPIC,
)
from app.agent import tool_binding
from app.tools import TOOL_SCHEMAS

log = logging.getLogger("app.agent.graph")


class StreamAborted(Exception):
    """Raised inside the agent node's token loop when the client has dropped
    (AD-E). It unwinds graph.stream so the worker stops immediately instead of
    running on to the AD-D ceiling burning tokens. The tool node handles abort
    differently — it stops BEFORE dispatching a write (see tool_node) — so a
    departed customer never gets a booking written."""


class _WrongLanguage(Exception):
    """Raised inside a generation's token loop when the reply's opening script
    contradicts the turn's language directive (rule 3). Only ever raised while
    the _ScriptWindow is still holding text, so nothing has reached the customer;
    exiting the stream's `with` closes the HTTP stream and stops the model
    server-side, exactly as the cancel path does. Caught by _default_generate's
    retry loop, never propagated further."""


# The same script tests app/lang.py uses, redeclared here rather than imported
# because lang.py exposes language answers, not its regexes — and the guard asks
# a narrower question (which script are these characters) than detect/classify.
_ARABIC_CHAR = re.compile(r"[؀-ۿ]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")


class _ScriptWindow:
    """Holds a generation's opening until its script is known — the rule 3 GUARD.

    The prompt's language anchor is an instruction, and an instruction can lose
    to conversation momentum: verified live twice on 2026-07-27, when a bare
    English "no" mid-Arabic-conversation drew an Arabic reply through both the
    soft and the hardened anchor wording. The reply's language exists only in
    the model's output, so a guarantee has to inspect the output. This class
    buffers the first WINDOW characters of a generation, decides which script
    they are written in, and only then releases them — a wrong-language opening
    is never emitted at all. That ordering is what keeps the text_break lesson
    intact: text the customer has read is never taken back, because withheld
    text was never shown.

    The rules are deliberately asymmetric, because the two scripts mix
    asymmetrically in this domain. Expecting English, ANY Arabic letter in the
    window is a violation — an English reply has no business opening in Arabic
    script. Expecting Arabic, Latin letters violate only when NO Arabic arrives
    within the window — an Arabic sentence may legitimately open with a
    Latin-script model name ("Jetour X50 تبدأ من..."). Digits and punctuation
    belong to no script and decide nothing. An Arabic character settles the
    matter the instant it appears (either way); otherwise the window fills to
    WINDOW characters, or is settled by flush() when the generation ends or a
    tool call seals the bubble first.

    `enforce=False` is the retry attempt's mode: same detection, but text is
    released immediately (a wrong-language reply beats a dead turn) and
    `violated` is only recorded so the failure is logged honestly.

    Shaped like MarkdownStripper on purpose — feed/flush, one instance per
    generation, state meaningless across generations — one choke point per
    concern (the stripper owns rule 17, this owns rule 3).
    """

    WINDOW = 40  # characters — reached within the first few deltas

    def __init__(self, expected: str | None, *, enforce: bool = True):
        # Only a wholly-one-script turn carries an expectation; mixed messages,
        # kickoff directives and failed voice notes arrive as None and pass through.
        self.expected = expected if expected in ("en", "ar") else None
        self.enforce = enforce
        self.violated = False
        self._buffer = ""
        self._decided = self.expected is None

    def feed(self, text: str) -> str:
        """Text safe to emit now: all of it once decided, "" while holding."""
        if self._decided:
            # A settled violation (enforce mode) releases nothing ever — the
            # caller aborts on the first violated feed, but the class must not
            # depend on that to keep wrong-language text off the wire.
            return "" if (self.violated and self.enforce) else text
        self._buffer += text
        ready = bool(_ARABIC_CHAR.search(self._buffer)) or len(self._buffer) >= self.WINDOW
        if not self.enforce:
            if ready:
                self._settle()
            return text
        if ready:
            return self._release()
        return ""

    def flush(self) -> str:
        """Settle on whatever is held — the generation ended, or a tool call is
        about to seal the bubble, so the window will not fill any further."""
        if self._decided:
            return ""
        if not self.enforce:
            self._settle()
            return ""
        return self._release()

    def _settle(self) -> None:
        self._decided = True
        has_arabic = bool(_ARABIC_CHAR.search(self._buffer))
        has_latin = bool(_LATIN_CHAR.search(self._buffer))
        if self.expected == "en":
            self.violated = has_arabic
        else:  # "ar"
            self.violated = has_latin and not has_arabic

    def _release(self) -> str:
        self._settle()
        held, self._buffer = self._buffer, ""
        return "" if self.violated else held

# Loop bound (agent generations per turn) and the AD-D wall-clock ceiling. A full
# booking is ~6 tool round-trips (≈7 generations) even with a re-query, so the
# cap is set well above it — it exists to stop a runaway loop, not to interrupt
# real work.
MAX_ITERATIONS = 16
AGENT_DEADLINE_SECONDS = 60  # AD-D "hard ~60s cap on the agent loop"
MAX_TOKENS = 2048

# How many trailing messages the agent node sends to the model per generation. The
# CHECKPOINTER still stores the full history — this only bounds what is re-sent, so
# a long conversation does not re-pay for every earlier turn on every call, and the
# model is not asked to attend to a history it has stopped attending to anyway. The
# facts a long chat depends on (name, number, chosen car) survive the cut because
# they are injected as the system-prompt facts anchor (session_facts), not fished
# out of old turns. Set well above one full booking turn (~7 messages) so the
# CURRENT turn is never truncated mid-flight — the window only ever drops COMPLETED
# earlier turns.
#
# RAISED FROM 12 TO 40 ON 2026-07-27, and the old value was measurably too small.
# A message is not an exchange: one exchange is a customer message, a tool_use, a
# tool_result and a reply, so 12 messages was about THREE exchanges of memory. In a
# live two-booking conversation that meant a booking confirmed six exchanges
# earlier was gone, an answer the customer had already given was re-asked three
# times, and the agent denied the existence of a booking it had just made. The
# anchors are the real fix (identity, and now bookings) because ANY window
# eventually runs out and the anchors never do; this is the margin around them, so
# ordinary recent context does not have to be promoted to a fact to survive.
# Affordable because both the system prompt and the message list are cache
# breakpoints (see _cached_messages) — a longer window is mostly cache reads.
TRIM_KEEP = 40

# A graceful, honest dead-end (behaviour rule 13 flavour): no fabricated policy,
# no stack trace, offer a human. The one place a tripped bound surfaces to the
# customer. Stage 2's SSE renders this as ordinary `token` output.
FALLBACK_TEXT = (
    "I'm sorry — I'm having trouble completing that just now. Let me have one of "
    "our specialists follow up with you so nothing falls through the cracks."
)


class AgentState(TypedDict):
    """Working state, checkpointed per session (thread_id = session_id).

    `messages` uses operator.add so each node APPENDS Anthropic-format message
    dicts (raw SDK shape, not langchain messages — AD-H keeps the agent node on
    the native SDK). `iterations` is per-turn control and is reset by the caller
    each new turn. `customer_id` is the session -> customer binding minted by
    find_or_create_customer and injected into the two identified-customer writes;
    it persists across turns so a returning-in-conversation customer stays
    identified.
    """

    messages: Annotated[list, operator.add]
    iterations: int
    customer_id: int | None
    # Rule 19: set by a successful close_conversation, read by the post-tools
    # edge to END the turn rather than hand the model another generation after
    # its farewell. Per-TURN control like `iterations` — the caller resets it
    # each turn — not a persisted "this session is finished" bit. Session-level
    # closed-ness lives in the audit trail (services/conversation.is_closed),
    # which is also what makes a repeat close idempotent rather than a second row.
    closed: bool
    # Terminal for THIS TURN only (2026-08-14): offer_options drew chips, so the
    # question is already in the stream and the next thing that should happen is
    # the customer answering. Deliberately NOT `closed` — the conversation is
    # wide open and the composer must stay live; it is only the generation loop
    # that has nothing left to add. Per-turn control, reset by the caller.
    options_offered: bool
    # The date-anchor pattern applied to IDENTITY (2026-07-24). A small dict of
    # confirmed facts about THIS conversation — { customer_id, name, title, mobile,
    # vehicle_id, vehicle_name, showroom_name, daypart }, all optional — computed
    # SERVER-SIDE from tool RESULTS by the tool node (never the model, never the
    # prompt) and injected into the system prompt, so a name/number/car given early
    # survives as the transcript grows and the model stops attending to old turns.
    # PERSISTED like `customer_id`: absent from the per-turn input in chat.py, so it
    # carries across turns from the checkpoint rather than resetting. Default
    # (last-value) semantics are correct because the tool node is the SOLE writer
    # and always returns the FULL merged dict — the "overwrite" is a merge it has
    # already done. Deliberately NOT trimmed away by _agent_node's message window;
    # outliving the turn that established it is the whole point.
    session_facts: dict
    # Rule 3 language enforcement (2026-07-25). The current turn's detected language
    # ("en"|"ar"), computed once server-side in chat.py from the customer's message
    # and injected into the system prompt so the reply language is a fact, not the
    # model's guess off a long, possibly Arabic-heavy window. Per-TURN like
    # `iterations`: the caller sets it each turn, and it stays constant across the
    # turn's tool loops (unlike the last message, which becomes a tool_result
    # mid-loop). Absent -> no language anchor, so old checkpoints stay valid.
    turn_lang: str | None
    # C10 recognition, and the pair below is a GUARD, not bookkeeping.
    #
    # `customer_turns` counts CUSTOMER messages, not iterations: the caller adds 1
    # per turn and the reducer sums it, so it advances when a human speaks and at
    # no other moment. `recognition_offered_turn` is stamped by the agent node the
    # first time it renders the "is that still you?" anchor.
    #
    # WHY IT HAS TO EXIST. The first live test of C10 had the model call
    # confirm_returning_customer on the word "hello" — binding the session and
    # reciting the customer's booking to whoever was holding the phone, without
    # ever asking. The tool description forbade exactly that, in capitals, and the
    # description lost. So the rule is now arithmetic the model cannot argue with:
    # YOU CANNOT ANSWER A QUESTION THAT HAS NOT BEEN ASKED YET, i.e. the bind is
    # refused unless customer_turns has advanced past the turn the question was
    # asked on. Iteration count is deliberately not used — a model that gets
    # refused and simply calls again inside the same turn would walk straight
    # through that, which is the hole this shape closes.
    #
    # Same lineage as forget_me's two-call handshake and the tool node's injected
    # customer_id: where a mistake costs somebody their privacy, the prompt states
    # the rule and the code enforces it.
    customer_turns: Annotated[int, operator.add]
    recognition_offered_turn: int | None
    # S7 ROUTING. Which journey agent owns this turn — "testdrive" or
    # "bookingmanagement" — decided by app/agent/router before the node runs.
    # It selects BOTH the prompt body (system_prompt(agent=...)) and the tool
    # allowlist (tool_binding.schemas_for), so the two can never disagree about
    # what this agent is.
    #
    # PERSISTED, not per-turn, and that is the sticky rule made durable: the
    # active agent survives to the next turn so a bare "yes" has something to
    # stay with. The router overwrites it whenever it decides to switch.
    #
    # ABSENT means UNROUTED — the whole registry and the combined prompt, exactly
    # as before the split. Every channel that has not adopted the router yet
    # (realtime S2S by design, doc §9) keeps working unchanged.
    agent: str | None


# --------------------------------------------------------------------------- #
# The model call (AD-A) — injectable so the graph is testable without a key
# --------------------------------------------------------------------------- #
def _normalise_block(block: Any) -> dict:
    """Anthropic SDK content block -> minimal JSON-safe dict.

    Only the two block types the agent emits are kept, trimmed to the fields the
    API accepts back as input — so replaying the history never trips on an
    SDK-only field (e.g. a null `citations`).
    """
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    # Defensive: unknown block type — keep it as data, never crash the turn.
    dump = getattr(block, "model_dump", None)
    return dump() if callable(dump) else {"type": str(btype)}


_client = None


def _traced(client):
    """Wrap the SDK client so the model call itself appears in LangSmith.

    Without this the trace shows the graph — agent node, tools node, the edges
    between them — and a HOLE where the generation should be: LangGraph is
    instrumented by langchain-core, but `client.messages.stream(...)` is the RAW
    Anthropic SDK (AD-A, deliberately not a Runnable), so nothing observes it.
    The prompt, the tool schemas, the stop reason, the token counts and the
    cache hit rate all live in that hole, which is most of what the trace is for.

    SAFE AGAINST OUR USAGE, checked rather than assumed: the wrapper's sync
    MessageStreamWrapper implements __iter__ over the RAW event stream (which
    _default_generate consumes on purpose, to see content_block_start the
    instant a tool_use begins) and passes get_final_message() straight through.
    So AD-A streaming, the per-delta cancel poll (AD-E) and the markdown
    stripper are untouched — this observes the call, it does not reshape it.

    Two consequences worth knowing rather than rediscovering: an aborted stream
    (StreamAborted) closes its span as an error, which is honest and not a bug;
    and PREWARM_PROMPT_CACHE emits one extra trace per boot.

    Returns the client UNWRAPPED when tracing is off, and on any failure to
    wrap. Observability must never be able to break a live turn — the same rule
    _log_usage follows.
    """
    if not LANGSMITH_TRACING:
        return client
    try:
        from langsmith.wrappers import wrap_anthropic

        log.info("LangSmith tracing enabled — wrapping the Anthropic client")
        return wrap_anthropic(client)
    except Exception:  # noqa: BLE001 — never let telemetry stop the agent
        log.warning("LangSmith wrap failed; continuing untraced", exc_info=True)
        return client


def _build_client():
    """Construct the Anthropic client for the configured provider.

    Two clients, one surface: both `Anthropic` and `AnthropicFoundry` expose
    `messages.stream(...)` identically, so this is the ONLY place in the agent
    that knows which provider is in play — everything downstream (AD-A streaming,
    the cancel poll, the markdown stripper) is provider-agnostic.

    Imported locally because scripted tests inject their own `generate` and never
    reach here; they must not need an SDK client at import time.
    """
    if USE_DIRECT_ANTHROPIC:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — the live agent node needs it on the "
                "direct path. Set it, or set USE_DIRECT_ANTHROPIC=false to use Azure "
                "Foundry. (Deterministic tests inject a scripted `generate` and never "
                "reach here.)"
            )
        from anthropic import Anthropic

        log.info("agent model provider=anthropic-direct model=%s", AGENT_MODEL)
        return _traced(Anthropic(api_key=ANTHROPIC_API_KEY))

    if not AZURE_FOUNDRY_API_KEY or not AZURE_FOUNDRY_RESOURCE:
        raise RuntimeError(
            "AZURE_FOUNDRY_API_KEY / AZURE_FOUNDRY_RESOURCE are not set — the live "
            "agent node needs them on the Foundry path. Set them, or set "
            "USE_DIRECT_ANTHROPIC=true to call Anthropic directly. (Deterministic "
            "tests inject a scripted `generate` and never reach here.)"
        )
    from anthropic import AnthropicFoundry

    log.info("agent model provider=azure-foundry model=%s", AGENT_MODEL)
    return _traced(AnthropicFoundry(
        api_key=AZURE_FOUNDRY_API_KEY,
        resource=AZURE_FOUNDRY_RESOURCE,
    ))


def _safe_writer():
    """The LangGraph custom-stream writer, or None outside a streaming context
    (so graph.invoke in tests emits nothing rather than raising)."""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 — not streaming; no writer to hand back
        return None


def _system_blocks(system: str) -> list[dict]:
    """The system prompt as ONE cacheable block (prompt caching).

    WHY ONE BLOCK, NOT TWO. The usual advice is to keep volatile content AFTER
    the cache breakpoint, and system_prompt() does append a date anchor. But
    that anchor is DATE-granular, not per-request: it is byte-identical for
    every session and every customer all day long. Splitting it out would only
    shrink the cached prefix by ~270 tokens and buy nothing. It changes at
    midnight, invalidating the entry once a day, which is correct.

    WHAT THIS DEPENDS ON. The cached prefix is `tools -> system` — that render
    order is fixed by the API — so TOOL_SCHEMAS must stay byte-stable for this
    to hit. It is a module constant, so today it does. Anything that later
    builds the tool list per session or per turn silently kills this, with no
    error: the API does not report a cache miss, it just bills full price.
    `agent usage ... cache_read=0` in the log is the only detector.

    NO CUSTOMER DATA IS CACHED. The key covers the system prompt and tool
    schemas only — nothing from a MemoryProfile, a booking, or a mobile number
    reaches it. Relevant to the D1 PDPL checklist.

    ONE HOUR, NOT THE DEFAULT FIVE MINUTES — and deliberately NOT the same TTL
    as the conversation breakpoint in _cached_messages(). The two breakpoints
    have opposite lifetimes. A conversation's own history is re-read seconds
    later by the next call of the same booking, so five minutes is generous
    there. THIS prefix is re-read by the NEXT CUSTOMER, and at a live demo the
    gap between one attendee finishing and the next scanning the QR is exactly
    the dead air five minutes cannot survive. Measured on a real booking the
    prefix is ~14.8k tokens and 46% of the whole bill; losing it to an idle
    booth means the next person pays the cold write AND waits through it on
    their first message, which is the one moment an audience is watching.

    The trade is a 2x write instead of 1.25x, needing three reads to pay for
    itself — trivially met, since a single booking re-reads this prefix ~35
    times. prewarm_cache() writes through this same function, so the boot
    warm-up inherits the hour automatically.

    The date anchor still rolls the key at midnight (see above), so a backend
    booted the night before demo day warms a prefix that is stale by morning.
    Restart after reset-demo, not before it.
    """
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


def _cached_messages(messages: list) -> list:
    """The message list with ONE moving cache breakpoint on its last block.

    WHY THIS EXISTS. Caching the system+tools prefix fixed the CONSTANT cost.
    What it does not touch is the conversation itself, which sits after that
    breakpoint and is re-sent at FULL price on every call — and grows all turn.
    Measured early in a conversation the uncached remainder is ~330 tokens; by
    turn ten, with every tool result replayed, it is thousands. Without this,
    turn ten re-pays for turns one to nine on each of its model calls.

    ONE MOVING BREAKPOINT, not an accumulating set. Each call places it at the
    end of what exists now; the previous call's entry is a PREFIX of this one,
    so the old portion is read at ~0.1x and only the delta is written. That is
    the documented multi-turn pattern, and it is why hits accrue as the
    conversation grows rather than degrading.

    Budget: 4 breakpoints per request, one already spent on the system prompt.
    This adds a second, leaving headroom.

    The 20-block lookback is the failure mode to watch: a breakpoint only
    searches back 20 content blocks for an existing entry. Our loop appends two
    or three blocks per model call, so the window is comfortable — but a single
    assistant turn emitting many parallel tool_use blocks could exceed it and
    silently miss. `agent usage ... cache_read` is the detector.

    COPIES rather than mutates: `messages` is graph state, and stamping
    cache_control onto it would checkpoint a request-shaping detail as if it
    were conversation content.
    """
    if not messages:
        return messages

    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")

    if isinstance(content, str):
        # Customer turns arrive as a plain string (chat.py). A one-block list is
        # equivalent on the wire and gives the breakpoint somewhere to live.
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        last["content"] = blocks
    else:
        return messages  # nothing markable; send it untouched

    return [*head, last]


def prewarm_cache() -> None:
    """Write the cached prefix once at startup. Never raises.

    A cache entry is only readable after the first response BEGINS streaming.
    Without this, a QR-code wave — many attendees scanning at once — all miss
    together, and each concurrent first message pays the 1.25x write. That is
    the worst possible moment for it, so we pay it once, here, before the room
    is watching.

    max_tokens=0 runs prefill (which is what writes the cache) and returns
    immediately with empty content — no output tokens billed, no reply to throw
    away. Note the 5-minute TTL still applies: this covers the start of the
    demo, not a booth that then sits idle for ten minutes.
    """
    try:
        client = _build_client()
        client.messages.create(
            model=AGENT_MODEL,
            max_tokens=0,
            system=_system_blocks(system_prompt()),
            tools=TOOL_SCHEMAS,
            messages=[{"role": "user", "content": "warmup"}],
            # Mirror the live calls' effort (AGENT_EFFORT) so this warmup is
            # request-shaped exactly like the traffic it warms the cache for —
            # a shape difference here would risk writing an entry the real
            # calls cannot read, which is this function's entire purpose.
            **({"output_config": {"effort": AGENT_EFFORT}} if AGENT_EFFORT else {}),
        )
        log.info("prompt cache pre-warmed — model=%s effort=%s", AGENT_MODEL, AGENT_EFFORT or "default")
    except Exception:  # noqa: BLE001 — the demo must start even if this fails
        log.warning(
            "prompt cache pre-warm failed; first turn will pay the cache write",
            exc_info=True,
        )


def _log_usage(final: Any, *, session_id: str, streamed: int) -> None:
    """Record what one model CALL actually cost. Never raises.

    Per CALL, not per turn, and that distinction is the point: every call
    re-sends the whole prefix (tools -> system -> messages, ~11.7k tokens as of
    2026-07-20), so a turn that loops N times pays it N times. Correlate with
    the `agent iteration=` line logged immediately before this one to read the
    multiplier off a real conversation instead of guessing at it.

    Cost here is dominated by INPUT, not output — output is capped at
    MAX_TOKENS, input grows with every tool result the checkpointer replays.

    `cache_write`/`cache_read` are 0 until prompt caching is wired. Once it is,
    a cache_read that sits at 0 across repeated calls is the ONLY signal that a
    byte changed in the prefix and caching has silently stopped working: the
    API does not error on a cache miss, it just bills full price. This log is
    the detector.

    Swallows everything: a missing or oddly-shaped usage field must never kill
    a live turn, and providers differ on which cache counters they report.
    """
    try:
        usage = getattr(final, "usage", None)
        if usage is None:
            return

        def _count(name: str) -> int:
            value = getattr(usage, name, 0)
            return int(value) if isinstance(value, (int, float)) else 0

        uncached = _count("input_tokens")
        cache_write = _count("cache_creation_input_tokens")
        cache_read = _count("cache_read_input_tokens")
        log.info(
            "agent usage session=%s prompt_total=%d (uncached=%d cache_write=%d "
            "cache_read=%d) output=%d streamed=%d stop=%s",
            session_id,
            # The full prompt is the SUM of the three input counters, not
            # `input_tokens` alone — that field is only the uncached remainder.
            # Reading it as the total is the classic way to under-report a
            # cached agent's real context size by an order of magnitude.
            uncached + cache_write + cache_read,
            uncached,
            cache_write,
            cache_read,
            _count("output_tokens"),
            streamed,
            getattr(final, "stop_reason", "?"),
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a live turn
        log.debug("usage logging failed — session=%s", session_id, exc_info=True)


def _default_generate(messages: list, *, model: str, system: str, tools: list, config: RunnableConfig | None = None) -> dict:
    """Production model call: raw Anthropic SDK STREAMING (AD-A, literal).

    Forwards each text delta to the SSE layer as a `token` custom event AS IT IS
    PRODUCED (AD-A/AD-B), never buffered-then-sent. Polls the per-request cancel
    flag every delta: a client that drops mid-generation stops us within one
    token, not at the 60s ceiling (AD-E). Returns the normalised assistant
    message (incl. any tool_use blocks) for the state's message list.

    ONE bounded exception to "never buffered" (rule 3's guard, 2026-07-27): when
    the turn's language is unambiguous (`expected_lang` in config), the FIRST
    ~40 characters are held by a _ScriptWindow until the reply's script is
    known. Wrong script -> the generation is aborted before anything was
    emitted and retried ONCE with a corrective system line; the retry streams
    unconditionally. Right script -> the hold releases within the first few
    deltas and everything after it streams exactly as before.
    """
    global _client
    if _client is None:
        _client = _build_client()

    cfg = (config or {}).get("configurable", {})
    cancel = cfg.get("cancel_event")
    writer = _safe_writer()

    session_id = cfg.get("session_id", "?")
    # Trusted context, same channel as db/session_id: "en"/"ar" when this turn's
    # message was wholly one script, None otherwise. Never model-supplied.
    expected_lang = cfg.get("expected_lang")
    # The effort dial, per channel (2026-07-27): a call turn's reply is HEARD,
    # and invisible deliberation before the first word is dead air down a
    # phone — so voice turns run at CALL_AGENT_EFFORT (default "low") while
    # text keeps AGENT_EFFORT. Same trusted-context flag as the voice anchor.
    effort = (
        CALL_AGENT_EFFORT
        if (cfg.get("voice_call") and CALL_AGENT_EFFORT)
        else AGENT_EFFORT
    )

    # PAY THE NOTE, NOT THE RETRY (2026-07-27). A discarded attempt costs a whole
    # extra model call (~1-2s), and the turns that need one are PREDICTABLE: the
    # customer just switched language, so the history's momentum points the other
    # way. When that mismatch is visible up front, inject the corrective note on
    # the FIRST attempt — ~60 tokens against a second generation. Measured live:
    # with the note in place attempt one complies and the guard goes quiet; the
    # retry below remains as the backstop for the residue.
    if _momentum_mismatch(messages, expected_lang):
        names = {"en": "English", "ar": "Arabic"}
        want = names[expected_lang]
        log.info("language switch against momentum; corrective note injected — session=%s", session_id)
        messages = _with_language_note(
            messages,
            (
                f"[System note, not written by the customer: the customer has just "
                f"switched language. Their message above is written wholly in {want} — "
                f"a short answer or a loanword counts as the language it is typed in. "
                f"Write your entire reply in {want}, from its first word.]"
            ),
        )

    sys_blocks = _system_blocks(system)
    for attempt in (1, 2):
        # ONE stripper per attempt (rule 17). Its state — the held ambiguous tail
        # and whether we sit at a line start — is meaningless across generations,
        # and a tail carried into the next one would surface as corruption.
        stripper = MarkdownStripper()
        # ONE window per attempt (rule 3). Downstream of the stripper, so it holds
        # exactly the text that would otherwise be emitted. On the retry it only
        # observes: a second wrong opening streams anyway — a wrong-language reply
        # beats a dead turn — and is logged in _stream_once.
        window = _ScriptWindow(expected_lang, enforce=(attempt == 1))
        try:
            return _stream_once(
                model=model,
                sys_blocks=sys_blocks,
                tools=tools,
                messages=messages,
                cancel=cancel,
                writer=writer,
                session_id=session_id,
                stripper=stripper,
                window=window,
                effort=effort,
            )
        except _WrongLanguage:
            names = {"en": "English", "ar": "Arabic"}
            want = names[expected_lang]
            other = names["ar" if expected_lang == "en" else "en"]
            log.warning(
                "language guard: reply opened in %s against a %s directive; "
                "discarded unseen, retrying — session=%s", other, want, session_id,
            )
            # The corrective rides INSIDE THE LAST USER MESSAGE, not as a system
            # block, and the placement is the lesson of the first live failure:
            # a corrective in `system` renders BEFORE the whole (Arabic-heavy)
            # history, so the model read it, then pages of Arabic, and obeyed
            # the momentum — verified on the VM, where a system-block corrective
            # was defied twice in a row. Appended after the history it is the
            # last thing read before generating. Same bracketed-note shape as
            # chat._VOICE_FAILED_NOTE (assistant prefill and role="system"
            # messages are both rejected by this model tier, so a user-turn
            # note is also the strongest placement the API allows). Bonus:
            # `sys_blocks` stays byte-identical, so the cached prefix survives
            # the retry untouched.
            messages = _with_language_note(
                messages,
                (
                    f"[System note, not written by the customer: your previous "
                    f"attempt at this reply began in {other} and was discarded "
                    f"before the customer saw any of it. The customer's message "
                    f"above is written wholly in {want} — a short answer or a "
                    f"loanword counts as the language it is typed in. Write your "
                    f"entire reply in {want}, from its first word.]"
                ),
            )
    raise AssertionError("unreachable: the retry attempt never raises _WrongLanguage")


def _momentum_mismatch(messages: list, expected: str | None) -> bool:
    """True when the last assistant TEXT in `messages` is in the other script.

    This is the predictor for a first-attempt language violation: the customer
    wrote wholly in `expected`, but the reply the model last gave — the momentum
    it will continue — is in the other language. Assistant messages with no text
    (tool_use-only) are skipped, not treated as evidence. No assistant text at
    all (a fresh conversation) is no mismatch: there is no momentum to fight.
    """
    if expected not in ("en", "ar"):
        return False
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            continue
        if not text.strip():
            continue  # tool_use-only turn; keep looking further back
        has_arabic = bool(_ARABIC_CHAR.search(text))
        if expected == "en":
            return has_arabic
        return not has_arabic and bool(_LATIN_CHAR.search(text))
    return False


def _with_language_note(messages: list, note: str) -> list:
    """A copy of `messages` with `note` appended to the LAST message's content.

    The last message is always role=user here — the graph only generates when the
    next turn is the assistant's — and may carry either a plain string (a customer
    turn) or a block list (a tool_result carrier). Both become a block list with
    one extra text block at the end. Copies, never mutates: `messages` belongs to
    the graph state and the note must not leak into the checkpoint.
    """
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    block = {"type": "text", "text": note}
    if isinstance(content, list):
        last["content"] = list(content) + [block]
    else:
        last["content"] = [{"type": "text", "text": content or ""}, block]
    out[-1] = last
    return out


def _stream_once(
    *, model, sys_blocks, tools, messages, cancel, writer, session_id, stripper, window,
    effort=None,
) -> dict:
    """One streamed model call — the body _default_generate retries around."""
    n_tokens = 0
    sealed = False  # one bubble break per generation, at its first tool_use block
    # Effort is the model's own latency dial and this model defaults it to HIGH
    # when unset — see AGENT_EFFORT in config (calls pass CALL_AGENT_EFFORT via
    # _default_generate). Empty/None omits the parameter.
    effort = AGENT_EFFORT if effort is None else effort
    # The dial exists on Sonnet/Opus tiers only. Haiku rejects it with a 400
    # ("This model does not support the effort parameter" — measured live
    # 2026-07-27, where it silenced every call turn), and Haiku needs no dial:
    # fast IS its default.
    if "haiku" in model:
        effort = ""
    extra: dict = {"output_config": {"effort": effort}} if effort else {}
    with _client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        # Block-wrapped by _default_generate (never at the graph call site) so the
        # injectable `generate` hook keeps its plain-string signature — scripted
        # tests pass a string and never reach this function.
        system=sys_blocks,
        tools=tools,
        # Second breakpoint: the conversation caches too, so a later turn does
        # not re-pay full price for every earlier one.
        messages=_cached_messages(messages),
        **extra,
    ) as stream:
        # Iterate the RAW event stream, not stream.text_stream, for one reason:
        # we need content_block_start to see a tool_use the INSTANT it begins.
        # Text deltas are still forwarded the moment they arrive, so AD-A's
        # literal streaming is untouched — this only adds an earlier signal.
        for event in stream:
            if cancel is not None and cancel.is_set():
                # AD-E: client gone. Exiting the `with` closes the HTTP stream, so
                # the model stops generating server-side too — no wasted tokens.
                log.info("agent ABORTED mid-generation after %d tokens — session=%s", n_tokens, session_id)
                raise StreamAborted("client disconnected during generation")

            etype = getattr(event, "type", None)

            # SEAL THE BUBBLE BEFORE A TOOL CALL (rule 17). A generation that
            # calls a tool may first emit narration ("I need to check which
            # version…"). Left alone that produces a run-on: the narration gets
            # stitched to the post-tool answer with no separator ("…under that
            # name.There are two T2 models…"). So we mark the boundary here, at
            # the first moment the protocol reveals a tool is coming, and the
            # next generation's tokens open a NEW bubble.
            #
            # THIS EVENT DELETES NOTHING, AND THAT IS THE WHOLE POINT. It used to
            # (`text_reset`), on the assumption that pre-tool text is always
            # throwaway narration because a real answer always follows. That
            # assumption is false in both directions and cost two live bugs: a
            # farewell written in the same message that calls close_conversation
            # (nothing follows it — the turn ends), and a full booking recap
            # written before acknowledge_licence_reminder (the next generation
            # had nothing to add and came back empty). Both were complete
            # answers, and both were erased mid-render.
            #
            # Every attempt to PREDICT which case we were in — by tool name, by
            # whether the turn ends, by whether replacement text arrives — got
            # some case wrong, because the distinction is semantic and we do not
            # have it at this point in the stream. Splitting instead of deleting
            # removes the prediction: the run-on is still fixed (different
            # bubbles cannot run together), and text the customer has already
            # read can never be taken away. The residual cost is that genuine
            # narration stays visible as its own short line — which the prompt
            # already discourages, and which reads as a person being helpful
            # rather than as a message vanishing.
            #
            # Suppressing narration BEFORE display remains impossible: the
            # tool_use block arrives AFTER the text, so hiding it would mean
            # buffering every text block until stop_reason is known, which would
            # stop the FINAL answer from streaming and break AD-A.
            if etype == "content_block_start":
                block = getattr(event, "content_block", None)
                if getattr(block, "type", None) == "tool_use" and not sealed:
                    sealed = True
                    if writer:
                        # The window will not fill further before this bubble
                        # seals — settle it on what it holds. A violation here
                        # aborts BEFORE the tool runs: nothing was emitted and
                        # nothing was executed, so the retry starts clean.
                        held = window.flush()
                        if window.violated and window.enforce:
                            raise _WrongLanguage()
                        if held:
                            writer({"type": "token", "text": held})
                            n_tokens += 1
                    if writer and n_tokens:
                        log.info(
                            "sealing bubble after %d pre-tool token(s) — session=%s", n_tokens, session_id
                        )
                        writer({"type": "text_break"})

            if etype == "text" and writer:
                # The SDK's synthetic text event: .text is the incremental delta.
                delta = getattr(event, "text", "") or ""
                if delta:
                    # RULE 17, THE SINGLE CHOKE POINT. Sanitising here — rather
                    # than in chat.py — is what makes screen and transcript
                    # identical by construction: chat.py forwards this same
                    # string to SSE *and* accumulates it into ConversationTurn,
                    # so there is only ever one string to be right about.
                    safe = stripper.feed(delta)
                    if safe:
                        # RULE 3, THE GUARD. Downstream of the stripper so it
                        # holds exactly what would have been shown. Returns ""
                        # while the opening's script is still undecided.
                        out = window.feed(safe)
                        if window.violated and window.enforce:
                            raise _WrongLanguage()
                        if out:
                            writer({"type": "token", "text": out})
                            n_tokens += 1

        # MUST flush before returning: a tail still held in the stripper would
        # otherwise be truncated from this reply or leak into the next
        # generation. See test_markdown_sanitiser.py.
        tail = stripper.flush()
        if tail and writer:
            out = window.feed(tail)
            if out:
                writer({"type": "token", "text": out})
                n_tokens += 1
        # And the window after it: a reply shorter than the window (a bare
        # "تم!") is settled here, on everything it wrote.
        rest = window.flush()
        if window.violated and window.enforce:
            raise _WrongLanguage()
        if rest and writer:
            writer({"type": "token", "text": rest})
            n_tokens += 1

        final = stream.get_final_message()

    if window.violated and not window.enforce:
        # The retry ALSO opened in the wrong language. It streamed regardless —
        # a wrong-language reply beats a dead turn — but never silently.
        log.error(
            "language guard: retry also opened in the wrong language; streamed as-is — session=%s",
            session_id,
        )
    _log_usage(final, session_id=session_id, streamed=n_tokens)
    return {"role": "assistant", "content": [_normalise_block(b) for b in final.content]}


def _fallback_message() -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": FALLBACK_TEXT}]}


def _emit_token(text: str) -> None:
    """Push text to the SSE layer as a `token` event (used so the graceful
    fallback still renders in the UI, even though it bypasses the model)."""
    writer = _safe_writer()
    if writer and text:
        writer({"type": "token", "text": text})


def _is_turn_start(msg: Any) -> bool:
    """True iff `msg` begins a customer turn — a user message that is NOT a
    tool_result carrier.

    The graph's history strictly alternates: user(customer text) → assistant
    (tool_use) → user(tool_result) → assistant(tool_use) → ... → assistant(text),
    then the next user(customer text). A "turn start" is that leading customer
    message: role user, content a plain string (chat.py sends a string) or a block
    list with NO tool_result. A user message whose content carries tool_result
    blocks is the ECHO of a tool round, never a turn start — cutting the window
    there would orphan those results from the tool_use that produced them.
    """
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return True  # a plain-string user message is always a customer turn


def _trim_messages(messages: list, keep: int = TRIM_KEEP) -> list:
    """The last `keep` messages, but never splitting a tool_use/tool_result pair.

    The Anthropic API rejects a message list that starts on an orphaned tool_result
    (a tool_result with no preceding tool_use) or that does not begin on a user
    turn. So we take the last `keep` as a floor, then walk the window START BACKWARD
    to the nearest turn boundary (_is_turn_start). Because a turn boundary is the
    customer message that opens a turn, everything after it — every tool_use and its
    matching tool_result for that turn — is included whole. The window can therefore
    exceed `keep` slightly (to reach a clean boundary); that is correct, not a bug.

    The window always ENDS on a user message here: _agent_node only runs when the
    next thing to produce is an assistant turn (turn start, or just after the tools
    node), so there is never a trailing assistant tool_use whose results are
    dropped. Short histories (< keep) are returned untouched — the common case and
    every scripted test.
    """
    if len(messages) <= keep:
        return messages
    start = len(messages) - keep
    while start > 0 and not _is_turn_start(messages[start]):
        start -= 1
    return messages[start:]


def _agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """One model turn, guarded by the loop bound and the AD-D ceiling.

    Both bounds are checked HERE, before generating, so neither a runaway loop
    nor a slow session ever produces more than a bounded number of generations,
    and the customer gets an honest fallback rather than a hang or a crash.
    """
    active_agent = state.get("agent")
    cfg = config.get("configurable", {})
    iterations = state.get("iterations", 0)
    cancel = cfg.get("cancel_event")
    session_id = cfg.get("session_id", "?")
    log.info("agent iteration=%d — session=%s", iterations, session_id)

    # AD-E: client already gone before this generation — end without calling the
    # model at all (no token burn). Text-only message routes to END next.
    if cancel is not None and cancel.is_set():
        log.info("agent aborted at entry (client gone) — session=%s iteration=%d", session_id, iterations)
        return {"messages": [_fallback_message()], "iterations": iterations + 1}

    # AD-D ceiling is wall-clock from TURN START (deadline_monotonic, stamped in
    # build_config), checked here at every agent entry — INDEPENDENT of the SSE
    # 15s heartbeat (a transport keepalive on queue silence). A chatty stream
    # emitting tokens never fires the ping yet still trips this ceiling, because
    # each tool round-trip re-enters this node and re-checks the clock. The two
    # clocks share no state.
    deadline = cfg.get("deadline_monotonic")
    over_time = deadline is not None and time.monotonic() > deadline
    over_iters = iterations >= MAX_ITERATIONS
    if over_time or over_iters:
        # Graceful dead-end (AD-D). Stream the fallback so the UI shows it.
        _emit_token(FALLBACK_TEXT)
        return {"messages": [_fallback_message()], "iterations": iterations + 1}

    generate: Callable = cfg.get("generate", _default_generate)
    # system_prompt() is computed per turn so it always carries today's date —
    # the model has no clock and needs the anchor to resolve relative dates. It
    # also carries the session's confirmed facts (name, number, chosen car): the
    # date-anchor pattern applied to identity, so a detail given twenty turns ago
    # is re-stated as fact every turn rather than relied on to survive in a history
    # the model no longer fully attends to. Empty for a new customer -> no anchor,
    # byte-identical prompt.
    # Send only the trailing window to the model (the checkpointer keeps the full
    # history). Identity survives the cut via the facts anchor above, not via the
    # old turns the window drops — see _trim_messages and TRIM_KEEP.
    # The bookings anchor (2026-07-27): what this customer ALREADY has, read from
    # the DB on every generation rather than remembered from the transcript. This
    # is the same argument as session_facts one step further — a name given twenty
    # turns ago used to be lost, and so did a booking confirmed six exchanges ago,
    # which is how the agent came to tell a customer it had "only one booking in
    # progress" just after confirming a second. Read here rather than folded into
    # session_facts because a booking CHANGES (it can be moved or cancelled by the
    # other paths — a reminder tap, the console, the WhatsApp handoff), so a value
    # accumulated once at tool time would go stale inside the same conversation.
    #
    # STRICTLY READ-ONLY, and it must stay that way: the tool node owns the
    # transaction and is the only thing here allowed to commit. Both helpers are
    # plain selects (slots.held_by_session filters expired locks in the query
    # instead of releasing them, for exactly this reason).
    #
    # Failure here must never cost the customer a turn: telemetry-grade caution,
    # the rule _log_usage already follows. A broken anchor degrades to the prompt
    # as it was before this existed, which is a worse answer, not a dead chat.
    live_bookings: list | None = None
    held_slot: dict | None = None
    # The recognition anchor (C10, 2026-08-14): the name this browser was last
    # used by. Read ONLY while the session is still unbound — once customer_id
    # exists the facts anchor already carries who we are talking to, and a second
    # anchor saying the same thing is how the agent ends up asking "still Ahmed?"
    # to Ahmed, twenty turns in.
    #
    # A NAME AND NOTHING ELSE (see services/recognition). Whatever lands here is
    # about to be said out loud to whoever is holding the device, so the anchor
    # can only be allowed to hold what is safe to say to a stranger.
    returning: dict | None = None
    try:
        db = cfg.get("db")
        if db is not None:
            customer_id = state.get("customer_id")
            if customer_id is not None:
                live_bookings = booking.for_customer(db, customer_id=customer_id)
            else:
                returning = recognition.lookup(db, token=cfg.get("device_token"))
            held_slot = slots.held_by_session(db, session_id=session_id)
    except Exception:  # noqa: BLE001 — an anchor must not be able to break a turn
        log.exception("bookings anchor failed — continuing without it (session=%s)", session_id)
        live_bookings, held_slot, returning = None, None, None

    # Call turns may run a FASTER model (CALL_AGENT_MODEL, off by default) —
    # first-token time is the audible floor of a spoken reply. Same trusted
    # flag as the voice anchor and the effort dial.
    model_for_turn = (
        CALL_AGENT_MODEL if (cfg.get("voice_call") and CALL_AGENT_MODEL) else AGENT_MODEL
    )
    assistant = generate(
        _trim_messages(state["messages"]),
        model=model_for_turn,
        system=system_prompt(
            agent=active_agent,
            session_facts=state.get("session_facts"),
            current_lang=state.get("turn_lang"),
            live_bookings=live_bookings,
            held_slot=held_slot,
            voice_call=bool(cfg.get("voice_call")),
            returning=returning,
        ),
        # PER-AGENT TOOL BINDING (S7). The allowlist is enforced by what the
        # model is handed, not by asking it to behave: BookingManagement is not
        # told to avoid create_booking, it simply does not have it. A prompt can
        # be argued with; an absent tool cannot.
        #
        # `agent` unset is the un-routed path — the whole registry, exactly as
        # before the split — so every caller that has not moved to the router yet
        # is untouched.
        tools=(
            tool_binding.schemas_for(active_agent) if active_agent else TOOL_SCHEMAS
        ),
        config=config,
    )
    out: dict = {"messages": [assistant], "iterations": iterations + 1}
    # Stamp the turn the question was asked on, ONCE (see AgentState). Written
    # only on the first render so a customer who ignores the question and answers
    # it three turns later still gets through — re-stamping every turn would move
    # the goalposts forward and make the guard unsatisfiable.
    if returning is not None and state.get("recognition_offered_turn") is None:
        out["recognition_offered_turn"] = state.get("customer_turns") or 0
    return out


def _route_from_agent(state: AgentState) -> str:
    """tools while the last assistant turn wants them, else END.

    Equivalent to `stop_reason == "tool_use"`, read structurally off the message
    so we never have to thread stop_reason through state. The fallback message is
    text-only, so a tripped bound falls through to END here.
    """
    last = state["messages"][-1]
    content = last.get("content", []) if isinstance(last, dict) else []
    if last.get("role") == "assistant" and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    ):
        return "tools"
    return END


def _route_from_tools(state: AgentState, config: RunnableConfig) -> str:
    """After the tools node: END if the client dropped (AD-E) or the conversation
    was closed (rule 19), else back to agent.

    On abort the tool node has already stopped before dispatching (no write) and
    synthesised aborted tool_results, so state stays consistent; this edge just
    ends the turn instead of running the agent again and burning a generation.

    On offered options, the QUESTION is already in the stream — offer_options is
    called in the same assistant message that asks it — so the turn is complete
    and the next move belongs to the customer.

    On close, the farewell is ALREADY in the stream — the agent says goodbye in
    the same message that calls close_conversation, so those tokens have reached
    the customer AND STAY THERE: the tool node skips the rule-17 retraction on a
    closing turn precisely because this edge leaves no generation to rewrite them
    in. Routing back to the agent here would buy one more generation after the
    goodbye, which the model would fill: a second sign-off, or worse, a cheerful
    new question in a chat the customer just ended."""
    cancel = config.get("configurable", {}).get("cancel_event")
    if cancel is not None and cancel.is_set():
        return END
    if state.get("closed"):
        return END
    # Chips are up and the question was asked in the same message, so there is
    # nothing left to generate. Routing back to the agent here buys exactly one
    # more sentence, and the model WILL fill it — the same failure the close
    # branch above exists to prevent, in a less obvious costume.
    if state.get("options_offered"):
        return END
    return "agent"


# --------------------------------------------------------------------------- #
# Build + per-turn config
# --------------------------------------------------------------------------- #
def build_graph(checkpointer: Any):
    """Compile the agent graph against a checkpointer (SqliteSaver in prod)."""
    g = StateGraph(AgentState)
    g.add_node("agent", _agent_node)
    g.add_node("tools", tool_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_from_agent, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", _route_from_tools, {"agent": "agent", END: END})
    return g.compile(checkpointer=checkpointer)


def build_config(
    db: Any,
    session_id: str,
    *,
    generate: Callable | None = None,
    deadline_seconds: float | None = AGENT_DEADLINE_SECONDS,
    cancel_event: Any | None = None,
    provided_slot_id: int | None = None,
    expected_lang: str | None = None,
    voice_call: bool = False,
    device_token: str | None = None,
) -> RunnableConfig:
    """Assemble the RunnableConfig that carries trusted context for one turn.

    `db` and `session_id` ride here in `configurable`, NEVER in graph state — so
    they are never checkpointed and never reach the model (the trusted-context
    rule). `thread_id = session_id` keys the checkpointer. `deadline_monotonic`
    is the AD-D ceiling, stamped HERE at turn start so it is wall-clock from the
    turn's beginning regardless of later token/heartbeat activity. `cancel_event`
    is the per-request abort flag (AD-E). `generate` overrides the model call
    (tests inject a scripted one; production leaves it None to use AD-A streaming).
    """
    configurable: dict[str, Any] = {
        "thread_id": session_id,
        "session_id": session_id,
        "db": db,
    }
    if deadline_seconds is not None:
        configurable["deadline_monotonic"] = time.monotonic() + deadline_seconds
    if generate is not None:
        configurable["generate"] = generate
    if cancel_event is not None:
        configurable["cancel_event"] = cancel_event
    if provided_slot_id is not None:
        # Trusted context (per-turn, never checkpointed): a tapped-chip slot_id the
        # tool node pins onto lock_slot. Joins db/session_id/customer_id as
        # executor-injected, never model-supplied.
        configurable["provided_slot_id"] = provided_slot_id
    if device_token:
        # C10 device recognition. Trusted context in the strictest sense: it is a
        # BEARER CREDENTIAL read off an HttpOnly cookie, so it rides in
        # `configurable` (never serialised, never checkpointed, never in metadata)
        # exactly as `db` does — the model must not see it, and it must not end up
        # in a LangSmith trace. The tool node uses it to record the device -> customer
        # link once a customer is identified; the agent node uses it only to ask
        # recognition.lookup for a NAME.
        configurable["device_token"] = device_token
    if voice_call:
        # AD-L: this turn arrived over the call channel, so the reply will be
        # SPOKEN. Drives the voice anchor in system_prompt (register only —
        # tone rules and tools are identical). Trusted context: only the WS
        # gateway sets it; a model cannot talk its way into or out of it.
        configurable["voice_call"] = True
    if expected_lang in ("en", "ar"):
        # Rule 3's guard (per-turn, never checkpointed): the script this turn's
        # reply MUST open in, set only when the customer's message was wholly one
        # script (lang.classify). _default_generate holds the reply's first
        # characters against it and retries once on a mismatch. "mixed" and None
        # never arrive here — an ambiguous turn carries no expectation.
        configurable["expected_lang"] = expected_lang
    # `metadata` and `run_name` are for the TRACE, not the graph — nothing in the
    # agent reads them, and they are inert when tracing is off.
    #
    # WHY session_id HAS TO BE STATED. Every turn builds a fresh graph and
    # checkpointer (chat.py), so every turn is its own ROOT run: a six-turn
    # booking reads as six unrelated traces unless they are grouped. LangSmith
    # groups on a `session_id` in the root run's metadata — and it will NOT
    # arrive on its own from `configurable` below, because langchain-core 1.x
    # narrowed ensure_config's configurable -> metadata copy to `model` and
    # `checkpoint_ns` only (it used to copy every str/int/float/bool, which is
    # what older guidance assumes).
    #
    # METADATA IS SERIALISED AND SENT; `configurable` IS NOT. That is exactly why
    # `db` and `cancel_event` stay below and only this id comes up here — the
    # trusted-context rule (AD-H) is unaffected, and a session uuid is the same
    # identifier already on every audit row.
    return {
        "configurable": configurable,
        "metadata": {"session_id": session_id},
        "run_name": "chat-turn",
    }


def new_sqlite_checkpointer(path: str = ":memory:") -> SqliteSaver:
    """A SqliteSaver over its OWN sqlite connection (separate from the app DB).

    check_same_thread=False: the turn runs on a worker thread (Stage 2). For a
    file-backed checkpoint we enable WAL so concurrent QR-attendee sessions —
    each opening its own connection to the shared file per turn — get concurrent
    readers with serialised writers instead of a single cross-thread connection
    (which sqlite3 forbids). busy_timeout rides out a brief writer overlap. The
    connection never touches the business tables.
    """
    import sqlite3

    conn = sqlite3.connect(path, check_same_thread=False)
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    return SqliteSaver(conn)


__all__ = [
    "AgentState",
    "build_graph",
    "build_config",
    "new_sqlite_checkpointer",
    "StreamAborted",
    "MAX_ITERATIONS",
    "AGENT_DEADLINE_SECONDS",
    "FALLBACK_TEXT",
    "SYSTEM_PROMPT",
]
