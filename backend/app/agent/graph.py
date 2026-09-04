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

from app.agent.prompt import SYSTEM_PROMPT, system_prompt_parts, turn_context
from app.agent.sanitise import MarkdownStripper
from app.agent.tool_node import tool_node
# Read-only, for the bookings anchor built in _agent_node. The agent node reads
# these services and never writes through them — see the note at the call site.
from app.services import booking, recognition, slots
from app.config import (
    CALL_AGENT_EFFORT,
    CALL_AGENT_MODEL,
    RETRIEVAL_FIRST,
    SPECULATE_ON_MESSAGE,
    TURN_CONTEXT_IN_MESSAGE,
    SIMPLE_TURN_MODEL,
    AGENT_EFFORT,
    AGENT_MODEL,
    ANTHROPIC_API_KEY,
    AZURE_FOUNDRY_API_KEY,
    CLAUDE_MODEL,
    AZURE_FOUNDRY_RESOURCE,
    LANGSMITH_TRACING,
    USE_DIRECT_ANTHROPIC,
)
from app.agent import tool_binding
from app.tools import TOOL_SCHEMAS, aisha_schemas
from app.tools.brief_tools import CALLED_ALONGSIDE_ERROR

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
# The low-water mark for _window_start: when the history outgrows TRIM_KEEP the
# window is cut back to the last TRIM_LOW messages and then LEFT ALONE until it
# outgrows TRIM_KEEP again. A window that always holds exactly the last 40 moves
# its first message on every turn, and a prefix cache cannot survive a moving
# start: measured live, every turn past the fortieth message re-wrote the whole
# window. 24 is about eight exchanges, well inside what the facts and bookings
# anchors already cover for anything older.
TRIM_LOW = 24

# A graceful, honest dead-end (behaviour rule 13 flavour): no fabricated policy,
# no stack trace, offer a human. The one place a tripped bound surfaces to the
# customer. Stage 2's SSE renders this as ordinary `token` output.
FALLBACK_TEXT = (
    "I'm sorry — I'm having trouble completing that just now. Let me have one of "
    "our specialists follow up with you so nothing falls through the cracks."
)


def _merge_dicts(a: dict | None, b: dict | None) -> dict:
    """Reducer for turn_context: later keys win, earlier keys survive."""
    return {**(a or {}), **(b or {})}


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
    # Per-message context blocks (TURN_CONTEXT_IN_MESSAGE), keyed by the index
    # of the customer message they belong to, as a string so the checkpoint
    # serialiser has nothing to guess about. Merged, never replaced: a block
    # written for turn 3 must be sent identically on turn 30, or the prefix
    # cache misses from that point on — see _attach_context.
    turn_context: Annotated[dict, _merge_dicts]
    # Where the window sent to the model starts (see _window_start). Moves in
    # jumps, not by one each turn, so the prefix stays stable between jumps.
    history_start: int
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
    # HOW MANY CASCADES THIS TURN HAS ALREADY RUN. Per-turn control, reset by
    # the caller exactly like `iterations` and `closed` — "how many times have
    # we gone back on THIS turn", never a property of the session. Counted
    # rather than bounded by MAX_ITERATIONS because the orchestrator node
    # deliberately does not advance that (it calls no model of its own), so a
    # re-briefing spiral is invisible to the loop bound that exists to stop it.
    cascades: int
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

        from app.services.anthropic_http import pooled_http_client

        log.info("agent model provider=anthropic-direct model=%s", AGENT_MODEL)
        # Longer idle keep-alive than the SDK's 5s — see services/anthropic_http.
        # None (dial at 0) hands the SDK its own default client, byte-for-byte
        # the old construction.
        http_client = pooled_http_client()
        if http_client is not None:
            return _traced(Anthropic(api_key=ANTHROPIC_API_KEY, http_client=http_client))
        return _traced(Anthropic(api_key=ANTHROPIC_API_KEY))

    if not AZURE_FOUNDRY_API_KEY or not AZURE_FOUNDRY_RESOURCE:
        raise RuntimeError(
            "AZURE_FOUNDRY_API_KEY / AZURE_FOUNDRY_RESOURCE are not set — the live "
            "agent node needs them on the Foundry path. Set them, or set "
            "USE_DIRECT_ANTHROPIC=true to call Anthropic directly. (Deterministic "
            "tests inject a scripted `generate` and never reach here.)"
        )
    from anthropic import AnthropicFoundry

    from app.services.anthropic_http import pooled_http_client

    log.info("agent model provider=azure-foundry model=%s", AGENT_MODEL)
    http_client = pooled_http_client()
    extra = {"http_client": http_client} if http_client is not None else {}
    return _traced(AnthropicFoundry(
        api_key=AZURE_FOUNDRY_API_KEY,
        resource=AZURE_FOUNDRY_RESOURCE,
        **extra,
    ))


def _safe_writer():
    """The LangGraph custom-stream writer, or None outside a streaming context
    (so graph.invoke in tests emits nothing rather than raising)."""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 — not streaming; no writer to hand back
        return None


def _system_blocks(system: str | tuple[str, str]) -> list[dict]:
    """The system prompt as a cacheable block, plus an uncached volatile tail.

    TWO BLOCKS NOW, AND WHY (2026-08-29). This was one cached block, on the
    reasoning that only the DATE anchor varied — daily, which was fine. The
    volatile anchors that have since moved into the prompt (session_facts, the
    bookings/held-slot anchor, the language anchor) change MID-CONVERSATION,
    and every change rewrote the whole ~30k-token prefix: the log showed
    cache_read=0 with a full cache_write right after a slot tap, twice in one
    session, at 3-5s of ttft each. Callers now pass system_prompt_parts()'s
    (stable, volatile) pair; the breakpoint sits on the stable half — body +
    date anchor — and the tail (~600-900 tokens) rides uncached behind it. A
    plain string still becomes the old single cached block, for callers that
    have nothing volatile.

    Note the API's invalidation hierarchy still applies: a changed tail
    re-reads the tail and everything after it (the conversation delta) at full
    price, but the stable prefix — the bulk of the bill — keeps hitting.

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
    stable, volatile = system if isinstance(system, tuple) else (system, "")
    blocks: list[dict] = [
        {
            "type": "text",
            "text": stable,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    if volatile:
        # Deliberately NO cache_control: this is the per-conversation tail the
        # split exists to keep out of the cached prefix.
        blocks.append({"type": "text", "text": volatile})
    return blocks


# The history breakpoint's cache_control — see _cached_messages for why it is
# an hour and not the API's five-minute default.
_HISTORY_CACHE_CONTROL: dict = {"type": "ephemeral", "ttl": "1h"}


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

    ONE HOUR, LIKE THE SYSTEM PREFIX (2026-09-04). This breakpoint used to take
    the API default of five minutes while the system block above it held for an
    hour, so a customer who paused to think for six minutes came back to a
    prompt whose stable prefix hit and whose whole conversation window was
    re-written. Seen on the test VM: a 9-minute pause cost a 24,684-token
    cache write and a 6.1s first token on a turn that had nothing else wrong
    with it. Nothing else refreshes this entry — the boot-time keep-alive warms
    the system prefix with a throwaway message and never touches a real
    conversation. A longer TTL costs more per write (2x base vs 1.25x) on the
    small per-call delta; measured against a re-read of the window at full
    price after every pause longer than a coffee, it is the cheaper of the two.
    Both breakpoints now carry the same TTL, which also satisfies the API's
    ordering rule that a longer-lived entry never follows a shorter one.
    """
    if not messages:
        return messages

    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")

    if isinstance(content, str):
        # Customer turns arrive as a plain string (chat.py). A one-block list is
        # equivalent on the wire and gives the breakpoint somewhere to live.
        last["content"] = [
            {"type": "text", "text": content, "cache_control": _HISTORY_CACHE_CONTROL}
        ]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = dict(_HISTORY_CACHE_CONTROL)
        last["content"] = blocks

    else:
        return messages  # nothing markable; send it untouched

    return [*head, last]


def _prefixes_to_warm() -> list[tuple[str, list]]:
    """Every (label, tools, system) THE LIVE PATH CAN ACTUALLY READ — no more.

    GATED ON THE SAME FLAG THE SERVING PATH READS (2026-09-01). This used to
    warm the single-agent prefixes (`full` and the two journeys) UNCONDITIONALLY
    and add the multi-agent pair beside them — so with MULTI_AGENT_ENABLED on,
    three of five Sonnet writes (~83k tokens a boot, at the 1h-TTL 2x write
    rate) warmed keys graph's own binding selection (see the `tools=` ternary
    in _agent_node) makes unreachable: every multi-agent call sends
    aisha_schemas(), never TOOL_SCHEMAS or a journey binding. That is the
    warm-a-key-nothing-reads bug this function was already patched for once —
    the comment below records the first occurrence — happening again on the
    other side of the flag. Measured on the test VM: 14 restarts since Aug 31
    made prewarm the single largest line on the bill, bigger than every
    conversation combined.

    DEDUPED BY THE SCHEMA LIST ITSELF on the single-agent side: two journeys
    binding identical tools share one cache entry, and warming it twice would
    pay the write twice for nothing.
    """
    out: list[tuple[str, list, object]] = []

    multi_agent = False
    try:
        from app.orchestrator import turn as _turn

        multi_agent = _turn.enabled()
    except Exception:  # noqa: BLE001 — a warm-up must never break a boot
        log.warning("could not read the multi-agent flag — warming the single-agent set")

    if not multi_agent:
        out.append(("full", TOOL_SCHEMAS, system_prompt_parts()))
        seen = {tuple(s["name"] for s in TOOL_SCHEMAS)}
        try:
            from app.agent import tool_binding

            for agent in getattr(tool_binding, "AGENTS", ()) or ():
                schemas = tool_binding.schemas_for(agent)
                key = tuple(s["name"] for s in schemas)
                if key in seen:
                    continue
                seen.add(key)
                out.append((agent, schemas, system_prompt_parts(agent=agent)))
        except Exception:  # noqa: BLE001 — a warm-up must never break a boot
            log.warning("could not enumerate per-agent tool bindings — warming the full set only")
        return out

    # THE PREFIX A LIVE CHAT TURN ACTUALLY READS (found 2026-08-31). This
    # function varied the TOOLS correctly and passed system_prompt_parts() with
    # no arguments for every one of them — the un-routed single-agent body. Since
    # S7 the router picks a journey body and T5.1 appends DELEGATING, so the
    # three prefixes hashed differently and NOT ONE of them was what a live turn
    # sent. The warm-up spent an API call per boot writing an entry nothing could
    # read, and every first customer still paid the cold write it exists to
    # avoid.
    #
    # `aisha:unrouted` IS LIVE TOO (2026-09-01): a first turn the router settles
    # as FALLBACK with no previous agent runs with agent=None, whose stable text
    # is the un-routed body plus DELEGATING — a third multi-agent prefix that
    # was served daily and never warmed, so the very first customer of a boot
    # often paid the one cold write prewarm exists to prevent.
    try:
        from app.tools import aisha_schemas as _aisha_schemas

        out.append((
            "aisha:unrouted",
            _aisha_schemas(),
            system_prompt_parts(multi_agent=True),
        ))
        for journey in ("testdrive", "bookingmanagement"):
            out.append((
                f"aisha:{journey}",
                _aisha_schemas(),
                system_prompt_parts(agent=journey, multi_agent=True),
            ))
    except Exception:  # noqa: BLE001
        log.warning("could not enumerate the multi-agent prefixes", exc_info=True)

    # AND THE SPECIALISTS' OWN. Measured 2026-08-31: a sub-agent step on a cold
    # prefix ran 5,368ms against 3,458ms warm — ~1.9s, paid by the first customer
    # to reach each agent in any hour. Their prompt is the card body plus a
    # DATE-granular anchor, exactly what run_step assembles, so warming it here
    # writes the same key that loop will read.
    try:
        from datetime import date as _date

        from app.agent.prompt import _DATE_ANCHOR, _relative_dates
        from app.agents import loader as _loader
        from app.orchestrator import subagent as _sub

        for agent in ("sales", "testdrive", "finance"):
            body = (
                _loader.body_for(agent)
                + _DATE_ANCHOR.format(**_relative_dates(_date.today()))
                + "\n\n"
                + _sub.FINDINGS_INSTRUCTION
                + _sub._proposals_clause(agent)
            )
            out.append((f"subagent:{agent}", _sub._schemas_for(agent), body))
    except Exception:  # noqa: BLE001
        log.warning("could not enumerate the sub-agent prefixes", exc_info=True)
    return out


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

    ONE PREFIX PER TOOL BINDING, NOT ONE OVERALL (T2.5). The cached prefix is
    `tools -> system`, so a DIFFERENT tool set is a different cache entry. The
    router already binds narrower tool sets per journey, and Phase 5 gives every
    sub-agent its own — warming a single prefix leaves the rest cold, and the
    first customer after each deploy pays the 1.25x write for whichever binding
    they happen to hit. That is exactly the moment this function exists to avoid.
    """
    try:
        client = _build_client()
        warmed = 0
        for label, schemas, system in _prefixes_to_warm():
          # ONE PREFIX AT A TIME. A single try around the whole loop meant the
          # first failure abandoned every prefix after it — which is exactly
          # what happened when the Haiku warms were added below: they 400'd and
          # took the specialists' entries with them, silently, leaving the boot
          # log looking like a partial success.
          try:
            model = CLAUDE_MODEL if label.startswith("subagent:") else AGENT_MODEL
            # THE DIAL IS SONNET/OPUS ONLY, the same rule _stream_once applies
            # and for the same measured reason: Haiku answers a request carrying
            # `effort` with a 400, "This model does not support the effort
            # parameter". Mirrored rather than shared because the shapes differ
            # — that one is streaming with a writer, this one is max_tokens=0.
            effort = "" if "haiku" in model else AGENT_EFFORT
            client.messages.create(
                # The specialists run on the cheap tier and a cache entry is
                # per MODEL — warming theirs against the agent model would
                # write a key their own calls can never read.
                model=model,
                max_tokens=0,
                system=_system_blocks(system),
                tools=schemas,
                messages=[{"role": "user", "content": "warmup"}],
            # Mirror the live calls' effort (AGENT_EFFORT) so this warmup is
            # request-shaped exactly like the traffic it warms the cache for —
            # a shape difference here would risk writing an entry the real
            # calls cannot read, which is this function's entire purpose.
                **({"output_config": {"effort": effort}} if effort else {}),
            )
            warmed += 1
            log.info("prompt cache pre-warmed — binding=%s tools=%d model=%s",
                     label, len(schemas), model)
          except Exception:  # noqa: BLE001 — one prefix, never the boot
            log.warning("could not pre-warm binding=%s; its first customer pays "
                        "the cache write", label, exc_info=True)
        log.info(
            "prompt cache pre-warmed — %d prefix(es), model=%s effort=%s",
            warmed, AGENT_MODEL, AGENT_EFFORT or "default",
        )
    except Exception:  # noqa: BLE001 — the demo must start even if this fails
        log.warning(
            "prompt cache pre-warm failed; first turn will pay the cache write",
            exc_info=True,
        )


def _log_usage(
    final: Any,
    *,
    session_id: str,
    streamed: int,
    elapsed_ms: int | None = None,
    ttft_ms: int | None = None,
    writer=None,
) -> None:
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
        timing = ""
        if elapsed_ms is not None:
            timing = f" {elapsed_ms}ms" + (
                f" (ttft {ttft_ms}ms)" if ttft_ms is not None else " (no text)"
            )
        log.info(
            "agent usage session=%s prompt_total=%d (uncached=%d cache_write=%d "
            "cache_read=%d) output=%d streamed=%d stop=%s%s",
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
            timing,
        )
        # ONTO THE STREAM AS WELL AS THE LOG (2026-09-01). turn_trace has
        # carried tokens_in/tokens_out columns since T3.5 and every row was
        # NULL — the numbers lived only in journald, so the token audit that
        # should have been one SQL query took ssh archaeology. The runner
        # accumulates these per turn and hands them to turn_trace.record.
        if writer is not None:
            writer({
                "type": "usage",
                "prompt_total": uncached + cache_write + cache_read,
                "output": _count("output_tokens"),
                "cache_read": cache_read,
            })
    except Exception:  # noqa: BLE001 — telemetry must never break a live turn
        log.debug("usage logging failed — session=%s", session_id, exc_info=True)


def start_cache_keepalive(minutes: int):
    """Re-run prewarm_cache every `minutes` on a daemon thread; returns the
    Event that stops it. On a still-live entry the re-warm is a cache READ of
    the prefix, which is what refreshes the 1h TTL — the cold write only ever
    happens once, at boot or after midnight rolls the date anchor."""
    import threading

    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(minutes * 60):
            prewarm_cache()

    threading.Thread(target=_loop, name="prompt-cache-keepalive", daemon=True).start()
    log.info("prompt cache keep-alive every %d min", minutes)
    return stop


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

    # WHERE THE SECONDS GO, and until 2026-08-29 nothing recorded it. The usage
    # line below says what a call COST but never how long it took, so a slow
    # turn could only be attributed by subtracting the sub-agent timings from a
    # stopwatch. Two numbers, because they have different cures: `ttft` is the
    # prefill on a ~23k prompt and is what the customer experiences as dead
    # air, while the remainder is generation and scales with output tokens.
    _call_started = time.monotonic()
    _first_token_at: list[float] = []

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
        # Speculative selection (2026-09-02): while an emit_briefs input is
        # still STREAMING, every completed brief string is offered to
        # orchestrator/speculate, which starts the embedding/judge selection
        # in parallel with the rest of Aisha's typing. Selection only — no
        # events, no writes — so a generation the language guard discards
        # wastes at most a Haiku read. `spec_buf` is None except inside an
        # emit_briefs block.
        spec_buf: list[str] | None = None

        # Iterate the RAW event stream, not stream.text_stream, for one reason:
        # we need content_block_start to see a tool_use the INSTANT it begins.
        # Text deltas are still forwarded the moment they arrive, so AD-A's
        # literal streaming is untouched — this only adds an earlier signal.
        for event in stream:
            # FIRST BYTE OF ANYTHING, not first token forwarded to the customer.
            # A generation whose text is all sealed away before a tool call
            # still had to prefill, and that prefill is the wait — attributing
            # it to "no text" would hide the very turns that feel slowest.
            if not _first_token_at:
                _first_token_at.append(time.monotonic())

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
                spec_buf = (
                    [] if getattr(block, "name", None) == "emit_briefs" else None
                )
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

            if etype == "input_json" and spec_buf is not None:
                # The SDK's synthetic input event: .partial_json is the delta
                # of the tool input being typed. Each completed brief inside
                # it starts its selection now instead of after the generation.
                part = getattr(event, "partial_json", "") or ""
                if part:
                    spec_buf.append(part)
                    from app.orchestrator import speculate as _speculate

                    _speculate.offer(session_id, "".join(spec_buf))

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
    _elapsed_ms = int((time.monotonic() - _call_started) * 1000)
    _ttft_ms = (
        int((_first_token_at[0] - _call_started) * 1000) if _first_token_at else None
    )
    _log_usage(
        final, session_id=session_id, streamed=n_tokens,
        elapsed_ms=_elapsed_ms, ttft_ms=_ttft_ms, writer=writer,
    )
    return {"role": "assistant", "content": [_normalise_block(b) for b in final.content]}


def _offer_message_selection(state: AgentState, session_id: str, *, slot_pinned: bool) -> None:
    """Start selecting a specialist for the customer's message. Never raises.

    THE SAME GATE AS THE PRE-FETCH, on purpose: a greeting, a name-and-number,
    a chip tap or a bare "yes" produces no brief a judge could help with, and
    each judge call is a Haiku read we would otherwise not have paid. What
    passes the gate is a question, and a question is what gets briefed.
    """
    try:
        text = _last_user_text(state).strip()
        if not text or len(text.split()) < 3:
            return
        from app.agent.router import prefilter as _prefilter

        if _prefilter.is_bare_greeting(text) or slot_pinned or _looks_like_answer(text):
            return
        from app.orchestrator import speculate as _speculate

        _speculate.offer_message(session_id, text)
    except Exception:  # noqa: BLE001 — a head start must never cost the turn
        log.warning("message-level speculation failed", exc_info=True)


def _speculate_take(session_id: str, *, first_generation: bool = True) -> dict:
    """This session's speculative selections, or {} — never a broken cascade.

    `first_generation` is False for a cascade that follows a tool round: the
    message-level selection was judged on the customer's whole message, and a
    brief written AFTER a tool ran is about whatever is left of it, which may
    be a different specialist. That entry is dropped; brief-level ones stay.
    """
    try:
        from app.orchestrator import speculate as _speculate

        return _speculate.take(session_id, include_message=first_generation)
    except Exception:  # noqa: BLE001
        return {}


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

    NO LONGER ON THE LIVE PATH (2026-09-02): _agent_node slices from the
    remembered _window_start instead, because a window that always ends at
    "the last 40" moves its first message every turn and defeats the prefix
    cache. Kept for the callers and tests that want the plain "last N" cut.

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


def _window_start(messages: list, prev_start: int) -> int:
    """Where the window sent to the model begins — stable between jumps.

    _trim_messages keeps the LAST `keep` messages, which means the window's
    first message changes on every turn once the history is long enough, and a
    prefix cache is only ever as good as its first differing byte: past the
    fortieth message every turn re-wrote the whole window (measured live). So
    the start is remembered in state and only moves when the window has
    outgrown TRIM_KEEP, jumping forward to the last TRIM_LOW messages — walked
    back to a turn boundary exactly as _trim_messages does, so a tool_use is
    never split from its result. Between jumps the prefix is byte-stable and
    the conversation reads from the cache.
    """
    n = len(messages)
    if n - prev_start <= TRIM_KEEP:
        return prev_start
    start = n - TRIM_LOW
    while start > prev_start and not _is_turn_start(messages[start]):
        start -= 1
    return start


def _turn_context_key(state: AgentState) -> str | None:
    """The index of this turn's customer message, as the string key the
    context block is stored under — the last turn start in the history."""
    msgs = state.get("messages") or []
    for i in range(len(msgs) - 1, -1, -1):
        if _is_turn_start(msgs[i]):
            return str(i)
    return None


def _attach_context(messages: list, blocks: dict) -> list:
    """The message list with each stored context block appended to its customer
    message as a trailing text block. COPIES — never mutates graph state.

    The block goes LAST in the message so _cached_messages' moving breakpoint
    lands on it, and it is attached on every later call as well: the cache is a
    prefix cache, so a block sent on turn 3 has to be sent, byte for byte, on
    every request after it. A message with no stored block is sent untouched,
    which keeps a fresh customer's request identical to what it always was.
    """
    if not blocks:
        return messages
    out = list(messages)
    for key, text in blocks.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if not text or i < 0 or i >= len(out) or not _is_turn_start(out[i]):
            continue
        msg = dict(out[i])
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            content = list(content)
        else:
            continue
        content.append({"type": "text", "text": text})
        msg["content"] = content
        out[i] = msg
    return out


def _is_simple_turn(state: AgentState) -> bool:
    """Whether the customer's message is one the cheap tier may answer.

    DETERMINISTIC OR NOT AT ALL. The only rule today is the router prefilter's
    bare-greeting check — reused, not re-implemented, so the router and this
    dial can never disagree about what a greeting is. Anything the rule cannot
    prove simple runs on the agent model; a failure here must cost nothing but
    the discount.
    """
    try:
        from app.agent.router import prefilter as _prefilter

        text = ""
        for m in reversed(state.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    text = " ".join(
                        b.get("text") or ""
                        for b in c
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                break
        return bool(text) and _prefilter.is_bare_greeting(text)
    except Exception:  # noqa: BLE001 — the discount is optional, the turn is not
        log.warning("simple-turn check failed; using the agent model", exc_info=True)
        return False


def _last_user_text(state: AgentState) -> str:
    """The customer's message this turn, or "" — factored from _is_simple_turn."""
    for m in reversed(state.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    b.get("text") or ""
                    for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            break
    return ""


# The pre-fetch keeps the volatile tail small: four passages cover a fact
# question (the search's own floor guarantees each collection its best), and
# a hard character cap keeps a long brochure section from tripling the
# uncached tail.
# Five, not four, with a floor of ONE per collection (2026-09-02): the pre-fetch
# now spans three collections (brochures, knowledge, catalogue), and the index's
# default floor of two would hand every slot to the reservations and none to the
# global ranking. One reserved slot each still guarantees the catalogue's best
# chunk a place beside the brochure that mentions the car on every page.
# Six (2026-09-02, measured): with the vehicle hint in the query the right
# catalogue passage is in the top five 94% of the time and in the top six 98%.
_PREFETCH_K = 6
_PREFETCH_FLOOR = 1
_PREFETCH_MAX_CHARS = 4000


# Messages that are answers, not questions — the pre-fetch has nothing to find
# for them and every passage it attaches is history the model must ignore.
# Measured on the test VM's 144 pre-fetch-triggering customer messages: these
# four rules skip 38 and not one of them was a question. There is no chip-tap
# signal to use instead: option chips send their label as plain typed text by
# design (frontend/src/options.ts), so the shape of the text is all there is.
_PHONE_RE = re.compile(r"\d[\d\s-]{7,}\d")
_PREFILL_TEXTS = frozenset({
    "i’d like to book a test drive.",
    "i'd like to book a test drive.",
    "i’d like to reschedule or cancel my booking.",
    "i'd like to reschedule or cancel my booking.",
})
_ANSWER_OPENERS = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "sure", "fine", "alright",
    "no", "nope", "not", "thanks", "thank", "perfect", "great", "good",
    "نعم", "أيوه", "ايوه", "تمام", "طيب", "أكيد", "اكيد", "ماشي", "لا", "لأ", "شكرا", "شكراً", "ممتاز",
})


def _looks_like_answer(text: str) -> str | None:
    """Why this message is an answer rather than a question, or None."""
    low = re.sub(r"\s+", " ", text.strip().lower())
    if low in _PREFILL_TEXTS or low.startswith(("i'll take ", "i’ll take ")):
        return "chip"
    if _PHONE_RE.search(low):
        return "phone"
    words = low.split()
    if words and "?" not in low and len(words) <= 5 and words[0].strip(",.!") in _ANSWER_OPENERS:
        return "affirmation"
    try:
        from app.retrieval import vehicle_mentions

        stripped = re.sub(r"\b(the|a|petrol|hybrid|please|one|model)\b", " ", low)
        stripped = re.sub(r"[^\w\s-]", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        found = vehicle_mentions.mentions(stripped)
        if found and stripped in vehicle_mentions.aliases_for(found[-1]):
            return "vehicle name only"
    except Exception:  # noqa: BLE001
        pass
    return None


def _prefetch_passages(state: AgentState, *, slot_pinned: bool = False) -> str | None:
    """RETRIEVAL_FIRST: the local index's best passages for this message.

    LOCAL, ~50ms, NO MODEL, NO TOKENS — measured live, the cascade this
    short-circuits spent five model calls (~14s) executing this exact lookup
    for a warranty question. Formatted the way search_knowledge formats its
    passages, because the prompt presents them as precisely that: a tool
    result delivered early. Returns None — prompt byte-identical — for a
    bare greeting or answer-shaped message, on any failure, and always when
    the flag is off at the call site. Never raises: the turn matters, the
    shortcut does not.
    """
    try:
        text = _last_user_text(state).strip()
        if not text or len(text.split()) < 3:
            return None
        from app.agent.router import prefilter as _prefilter

        if _prefilter.is_bare_greeting(text):
            return None
        # A slot-chip tap arrives with its id pinned (ChatRequest.slot_id) —
        # the one chip the backend can recognise structurally.
        if slot_pinned:
            return None
        why = _looks_like_answer(text)
        if why:
            log.info("retrieval prefetch skipped: %s", why)
            return None
        from app.retrieval import index as _index
        from app.retrieval import vehicle_mentions
        from app.tools.knowledge_tools import FENCE_CLOSE, FENCE_OPEN

        # THE VEHICLE HINT. A message that names no car is almost always about
        # the car the conversation is already about; tell the index which one.
        # See retrieval/vehicle_mentions for the measurement behind this.
        query = text
        if not vehicle_mentions.mentions(text):
            current = vehicle_mentions.last_mentioned(state.get("messages") or [])
            if current:
                query = f"{text} {current}"
        hits = _index.search(
            query,
            collections=_index.PREFETCH_COLLECTIONS,
            k=_PREFETCH_K,
            floor=_PREFETCH_FLOOR,
        )
        if not hits:
            return None
        parts: list[str] = []
        total = 0
        for chunk, _score in hits:
            block = f"{FENCE_OPEN % chunk.citation}\n{chunk.body}\n{FENCE_CLOSE}"
            if total + len(block) > _PREFETCH_MAX_CHARS and parts:
                break
            parts.append(block)
            total += len(block)
        return "\n\n".join(parts) or None
    except Exception:  # noqa: BLE001 — the shortcut must never cost a turn
        log.warning("retrieval prefetch failed; proceeding without", exc_info=True)
        return None


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
    _prep_t0 = time.perf_counter()
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
    _reads_ms = int((time.perf_counter() - _prep_t0) * 1000)

    # Call turns may run a FASTER model (CALL_AGENT_MODEL, off by default) —

    # first-token time is the audible floor of a spoken reply. Same trusted
    # flag as the voice anchor and the effort dial.
    #
    # SIMPLE chat turns may too (SIMPLE_TURN_MODEL, off by default; OPEN-ISSUES
    # #2): a bare greeting pays the agent model's full 2,401ms fixed cost to
    # say hello. "Simple" is decided by the router prefilter's deterministic
    # rule and nothing looser — see config.SIMPLE_TURN_MODEL for why the
    # vocabulary is that narrow. Checked on the CUSTOMER's message: a
    # tool-round re-entry sees a tool_result there, extracts no text, and
    # falls back to the agent model, which is the conservative direction.
    if cfg.get("voice_call") and CALL_AGENT_MODEL:
        model_for_turn = CALL_AGENT_MODEL
    elif not cfg.get("voice_call") and SIMPLE_TURN_MODEL and _is_simple_turn(state):
        log.info("simple turn — model=%s session=%s", SIMPLE_TURN_MODEL, session_id)
        model_for_turn = SIMPLE_TURN_MODEL
    else:
        model_for_turn = AGENT_MODEL
    # RETRIEVAL_FIRST: the local index's passages for this message, first
    # generation of the turn only — the compose call after a cascade already
    # holds its evidence and must not pay the tail twice. None everywhere the
    # flag is off (the shipped default), which keeps the prompt byte-identical.
    _prefetch_t0 = time.perf_counter()
    passages = (
        _prefetch_passages(state, slot_pinned=cfg.get("provided_slot_id") is not None)
        if RETRIEVAL_FIRST and _multi_agent(cfg) and iterations == 0
        else None
    )
    _prefetch_ms = int((time.perf_counter() - _prefetch_t0) * 1000)

    # SPECULATE_ON_MESSAGE (2026-09-03): the specialist selection starts NOW,
    # on the customer's own words, and runs under the whole generation below.
    # The brief-level offer in _stream_once fires on the brief's closing quote,
    # which is the last few bytes of the tool input — 9ms p50 before the
    # generation ends, so the judge still ran serially. First generation of a
    # chat turn only: a re-entry after a tool has no customer message to read.
    if SPECULATE_ON_MESSAGE and _multi_agent(cfg) and iterations == 0:
        _offer_message_selection(
            state, session_id, slot_pinned=cfg.get("provided_slot_id") is not None
        )
    # TURN_CONTEXT_IN_MESSAGE (2026-09-02): facts, bookings and passages travel
    # BEHIND the history, as a trailing block on this turn's customer message,
    # and stay there for the rest of the session. Rendered once per turn (the
    # compose call re-reads the same bytes, which is the point) and stored in
    # state under the message's index so later turns re-attach it unchanged.
    in_message = TURN_CONTEXT_IN_MESSAGE
    context_key = _turn_context_key(state)
    context_blocks = dict(state.get("turn_context") or {})
    new_context: str | None = None
    if in_message and iterations == 0 and context_key is not None:
        new_context = turn_context(
            session_facts=state.get("session_facts"),
            live_bookings=live_bookings,
            held_slot=held_slot,
            retrieved_passages=passages,
        )
        if new_context:
            context_blocks[context_key] = new_context
    outgoing = (
        _attach_context(state["messages"], context_blocks)
        if in_message else state["messages"]
    )
    prev_start = state.get("history_start") or 0
    start = _window_start(outgoing, prev_start)
    # Everything between entering the node and the model call, itemised. The
    # prefetch is a CPU embedding under a process-wide lock and had never been
    # timed outside a docstring; the DB reads run on every generation,
    # including the compose call. See runner's "turn timing" line for the
    # turn-level view these roll up into.
    log.info(
        "agent node prep session=%s iteration=%d total=%dms (reads=%dms prefetch=%dms)",
        session_id, iterations, int((time.perf_counter() - _prep_t0) * 1000),
        _reads_ms, _prefetch_ms,
    )
    assistant = generate(

        outgoing[start:],
        model=model_for_turn,
        system=system_prompt_parts(
            agent=active_agent,
            # Without this Aisha reads the SINGLE-agent body on the multi-agent
            # path — the ported prose that tells her to call lock_slot and
            # compare_vehicles, with nothing saying she no longer holds them.
            multi_agent=_multi_agent(cfg),
            session_facts=state.get("session_facts"),
            current_lang=state.get("turn_lang"),
            live_bookings=live_bookings,
            held_slot=held_slot,
            voice_call=bool(cfg.get("voice_call")),
            returning=returning,
            retrieved_passages=None if in_message else passages,
            context_in_message=in_message,
        ),
        # PER-AGENT TOOL BINDING (S7). The allowlist is enforced by what the
        # model is handed, not by asking it to behave: BookingManagement is not
        # told to avoid create_booking, it simply does not have it. A prompt can
        # be argued with; an absent tool cannot.
        #
        # `agent` unset is the un-routed path — the whole registry, exactly as
        # before the split — so every caller that has not moved to the router yet
        # is untouched.
        #
        # THE MULTI-AGENT BINDING (T5.1) sits in FRONT of both, and only when
        # the flag is on for this channel: Aisha keeps her card's conversational
        # tools plus emit_briefs, and holds no catalogue, slot or finance read
        # at all — those are what the specialists are for. With the flag off
        # this expression is byte-identical to what it was, which is what keeps
        # the cached prefix alive on the live path.
        tools=(
            aisha_schemas() if _multi_agent(cfg)
            else tool_binding.schemas_for(active_agent) if active_agent
            else TOOL_SCHEMAS
        ),
        config=config,
    )
    out: dict = {"messages": [assistant], "iterations": iterations + 1}
    if new_context:
        out["turn_context"] = {context_key: new_context}
    if start != prev_start:
        out["history_start"] = start
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
        # emit_briefs is answered by the ORCHESTRATOR, not the tools node: it
        # has no function to dispatch, and the cascade owns a transaction the
        # tool node must not be inside. Checked first so a message carrying it
        # never reaches tool_node, which would return unknown_tool and strand
        # the turn on a delivered opening line.
        if _brief_block(state) is not None:
            return "orchestrator"
        return "tools"
    return END


def _multi_agent(cfg: dict) -> bool:
    """Whether THIS turn fans out. Channel-aware, and cheap enough to re-ask.

    Voice is excluded inside turn.enabled() rather than here, so there is one
    answer to "is the fan-out on" and not two that can drift.
    """
    from app.orchestrator import turn as _turn

    return _turn.enabled(voice_call=bool(cfg.get("voice_call")))


def _brief_block(state: AgentState) -> dict | None:
    """The emit_briefs tool_use in the last assistant message, if there is one."""
    last = state["messages"][-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return None
    for b in last.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "emit_briefs":
            return b
    return None


def _stream_sink(writer):
    """Adapt the LangGraph writer to the (kind, payload) shape forward_events wants.

    THE ONE ADAPTER THAT MUST NOT BE WRONG. The writer takes a single dict whose
    `type` names the event; turn.forward_events calls its sink with (kind,
    payload). Get this backwards and every sub-agent tool_event is dropped —
    which does not raise, does not log, and shows up only as a reply with all
    its vehicle and slot cards missing, reading as a styling bug.
    """
    def _post(kind: str, payload: dict) -> None:
        if writer is None:
            return
        writer({"type": kind, **(payload or {})})

    return _post


def _known_entities(state: AgentState, cfg: dict | None = None) -> set[str]:
    """Entities this session already established, so a follow-up runs alone.

    Without this, "book it for Saturday" looks like it needs a Sales step to
    produce the vehicle_id the customer settled on ten turns ago — and the
    TestDrive brief comes back blocked on a question nobody asked.

    SESSION_FACTS IS NOT THE ONLY EVIDENCE, and believing it was cost a whole
    cascade per reschedule (found live, 2026-08-31). `_agent_node` reads the
    customer's live bookings and held slot from the DB on EVERY generation and
    renders them into the prompt, so Aisha opened the turn by saying "you have
    a booking, TD-2026-7QHB, the Jetour G700, at Jetour Al Quoz" — and then the
    orchestrator blocked her TestDrive brief for want of a booking_ref, because
    only tool_node and _fold_cascade_facts write session_facts and neither had
    run. The customer waited through a dead cascade, Aisha re-briefed with the
    reference spelled into the text (which run.plan_turn detects), and the same
    answer arrived two model calls later.

    READ, NOT REMEMBERED, for exactly the reason the bookings anchor is read
    fresh each generation rather than folded into session_facts: a booking
    CHANGES — the console, a reminder tap and the WhatsApp handoff can all move
    it — so a value cached at tool time goes stale inside the same conversation.

    NAMES ONLY, NEVER VALUES. This returns the SET of entities that exist, which
    is all `order.resolve` needs to decide what may run; the values still come
    from the steps that produce them. So nothing here can put a stale booking
    reference in front of a sub-agent.

    Failure is degradation, never a dead turn — the same telemetry-grade caution
    the anchor itself follows. A failed read leaves `known` exactly as it was
    before this existed.
    """
    from app.orchestrator import order as _order

    facts = state.get("session_facts") or {}
    producible: set[str] = set()
    for agent in ("sales", "testdrive", "finance"):
        producible |= _order.produces(agent)
    # NAME -> VALUE, not just the names (found live, 2026-08-31). Seeding the
    # ordering with names alone unblocked Finance and then handed it an empty
    # context, so it was asked to price a trim it had never been told. See
    # run.execute. `order.resolve` iterates this for its names exactly as it
    # did when this was a set, so the ordering is unchanged.
    known = {k: facts[k] for k in producible if facts.get(k) is not None}

    cfg = cfg or {}
    db = cfg.get("db")
    if db is None:
        return known
    try:
        customer_id = state.get("customer_id")
        # NAMES ONLY FOR THESE TWO, deliberately, and the None is the point: a
        # booking can be moved or cancelled by the console, a reminder tap or
        # the WhatsApp handoff, so the VALUE read here can be stale by the time
        # a specialist reads it. Asserting the name unblocks the ordering; the
        # value still comes from the step that looks it up. setdefault so a
        # fresher value already folded into session_facts is never overwritten.
        if (
            "booking_ref" in producible
            and customer_id is not None
            and booking.for_customer(db, customer_id=customer_id)
        ):
            known.setdefault("booking_ref", None)
        if "slot_id" in producible and slots.held_by_session(
            db, session_id=cfg.get("session_id", "")
        ):
            known.setdefault("slot_id", None)
    except Exception:  # noqa: BLE001 — an anchor must not be able to break a turn
        log.exception(
            "could not read live bookings for the ordering — continuing without them "
            "(session=%s)", cfg.get("session_id", "?"),
        )
    return known


def _execute_proposals(result, state: AgentState, cfg: dict) -> list[dict]:
    """Perform what the sub-agents ASKED for, or say why not (T4.2).

    THE ONLY PLACE A COMMERCIAL WRITE HAPPENS on this path. Sub-agents hold no
    write tool at all — that is what makes running them on a cheap model safe —
    so holding a slot or creating a booking arrives here as a REQUEST and leaves
    as a decision. Three gates, none of them redundant:

        the CARD    says which actions this agent may ask for  (subagent.py)
        the MACHINE says which are valid right now             (booking_state)
        the SERVICE says whether it is actually permitted      (unchanged)

    The third is the one that was load-bearing before any of this existed and
    still is: create_booking refuses without the licence acknowledgement whoever
    calls it, and this module never relaxes that.

    IT COMMITS. write.execute deliberately does not — an audit row must live or
    die with the write it describes — so the transaction is closed here, once,
    after every proposal in the turn has been decided.
    """
    from app.orchestrator import booking_state, write

    proposals = [
        write.Proposal(action=p["action"], args=p.get("args") or {}, agent=p.get("agent"))
        for step in result.steps for p in (step.proposals or [])
    ]
    if not proposals:
        return [], {}

    # HOLDS BEFORE BOOKINGS, ALWAYS (found live 2026-09-02). A sub-agent asked
    # to book a slot it must first hold sometimes proposes them create-first,
    # and execute_all stops at the first refusal — so create_booking was
    # refused for want of a hold and the hold beside it never ran. Hold and
    # lock go to the front, preserving order otherwise; the only valid sequence
    # is hold-then-create, so this can reorder nothing that was already right.
    _HOLD_FIRST = {"hold_slot", "lock_slot"}
    proposals.sort(key=lambda p: 0 if (p.action or "") in _HOLD_FIRST else 1)

    db = cfg.get("db")
    session_id = cfg.get("session_id", "?")
    customer_id = state.get("customer_id")
    if db is None:
        log.error("proposals with no db — refusing all (session=%s)", session_id)
        return [{"action": p.action, "status": "refused",
                 "detail": "no database session"} for p in proposals], {}

    try:
        machine = booking_state.read(db, session_id, customer_id=customer_id)
        allowed = machine.allowed
    except Exception:  # noqa: BLE001 — an unreadable state refuses, never guesses
        log.exception("could not read booking state — refusing writes (session=%s)", session_id)
        return [{"action": p.action, "status": "refused",
                 "detail": "could not establish where this booking had got to"}
                for p in proposals], {}

    # THE GATE, RE-DERIVED PER PROPOSAL. A hold and a booking in one batch move
    # the state between them — the hold locks the slot, which is exactly the
    # precondition the booking's gate checks — so the set read once above goes
    # stale mid-batch. This closure re-reads it from the same session (execute
    # flushes, so the pending hold is visible); a failure degrades to the
    # snapshot. See write.execute_all.
    def _allow_now() -> set[str]:
        return booking_state.read(db, session_id, customer_id=customer_id).allowed

    # AND WHAT TO DO IF IT SAYS NO. The gate's own refusal names the actions that
    # ARE valid, which tells Aisha nothing she can act on — a missing licence
    # acknowledgement is not deducible from a list of four tool names, and told
    # only that, she reported "a technical issue" and escalated a booking that
    # was one step from done (2026-09-03). next_step() has rendered the sentence
    # since T4.3; this is the call it was waiting for.
    def _next_step_now() -> str:
        return booking_state.next_step(
            booking_state.read(db, session_id, customer_id=customer_id)
        )

    results = write.execute_all(
        db, session_id, proposals,
        customer_id=customer_id, allow=allowed, allow_for=_allow_now,
        next_step_for=_next_step_now,
    )

    if any(r.ok for r in results):
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("commit failed — rolling back (session=%s)", session_id)
            db.rollback()
            return [{"action": r.action, "status": "failed",
                     "detail": "the change could not be saved"} for r in results], {}
    else:
        # Nothing was written, but a refused proposal may still have left the
        # session dirty (a service that flushed before its guard said no).
        db.rollback()

    # WHAT THE CUSTOMER ACTUALLY CHOSE (found live, 2026-08-31, and it cost a
    # booking). session_facts held vehicle_id 7 — the X70 FL Plus, the car SALES
    # LED WITH on the first turn — while the customer had gone on to pick the
    # cheaper X70 FL, vehicle 8, and held a slot for it. Nothing ever corrected
    # the note: _harvest_entities takes the FIRST vehicle_id out of a ranked
    # search, which is "the one it recommended", and a customer changing their
    # mind writes nothing.
    #
    # tool_node HAS ALWAYS DONE THIS RIGHT and the multi-agent path walked round
    # it. _fold_session_facts calls slots.facts_for_slot on a successful
    # lock_slot, reading the car off the locked Slot ROW — the authoritative
    # record of what was chosen. But a hold reaches the DB through write.execute
    # here, not through tool_node._dispatch, so that fold never ran and the
    # stale note survived to be handed to a specialist as established fact.
    #
    # Read off the ROW, never from the proposal's arguments: the same rule 2
    # grounding facts_for_slot was written for. A model that named the wrong
    # slot cannot write the wrong car into the facts, because the row it landed
    # on is what gets read.
    write_facts: dict = {}
    out = []
    for r in results:
        row = {"action": r.action, "status": r.status, "detail": r.detail}
        # WHAT AISHA MAY SAY. The reference and the slot she needs to read a
        # confirmation back, and nothing else from the write's internals.
        #
        # THE SERVICES NEST THESE UNDER "booking" (found live, 2026-08-31):
        # create/reschedule/cancel all return {"booking": _booking_dict(...)},
        # and reading only the top level here meant an EXECUTED create_booking
        # reached Aisha with no reference at all — she told the customer it
        # hadn't come through while the card drew from a follow-up get_booking.
        inner = r.result.get("booking")
        inner = inner if isinstance(inner, dict) else {}
        for key in ("booking_ref", "slot_id", "start_human", "start_iso", "showroom_name"):
            val = r.result.get(key)
            if val is None:
                val = inner.get(key)
            if val is not None:
                row[key] = val
        out.append(row)
        # The booking event the UI draws its card from, same as tool_node emits.
        # The slot a write settled on, whichever shape it came back in:
        # lock_slot nests it under `slot`, the booking services under `booking`.
        if r.ok:
            nested = r.result.get("slot")
            nested = nested if isinstance(nested, dict) else {}
            slot_id = row.get("slot_id") or nested.get("slot_id")
            if slot_id is not None:
                try:
                    write_facts.update(slots.facts_for_slot(db, slot_id) or {})
                except Exception:  # noqa: BLE001 — a fact must not cost a write
                    log.warning("could not read facts for slot %s", slot_id, exc_info=True)

        booked_ref = r.result.get("booking_ref") or inner.get("booking_ref")
        if r.ok and booked_ref:
            writer = _safe_writer()
            if writer:
                writer({"type": "booking", "booking_ref": booked_ref})
    log.info("orchestrator writes — session=%s %s%s", session_id,
             [(r.action, r.status) for r in results],
             f" facts={sorted(write_facts)}" if write_facts else "")
    return out, write_facts


def _fold_cascade_facts(state: AgentState, result) -> dict | None:
    """Carry what the cascade established into session_facts (found live).

    WHY THIS IS NOT OPTIONAL. Entities thread between STEPS inside one turn —
    Sales produces a vehicle_id and TestDrive receives it — but before this they
    were discarded when the turn ended. `_known_entities` reads session_facts,
    and only tool_node writes it, so a sub-agent's findings never got there.

    The live failure: turn 1 established the T2, turn 3 asked for slots at Al
    Quoz, and order.resolve correctly blocked TestDrive because vehicle_id was
    unknown *again*. Aisha retried five times, apologised, and escalated. Every
    component behaved exactly as designed and the booking still could not happen,
    because the memory between them was missing.

    STILL TOOL-DERIVED, so rule 2 holds. subagent._harvest_entities reads these
    off the tool RESULTS rather than the model's prose, which is the whole reason
    that function exists — a model-asserted vehicle_id must never reach the facts
    anchor and be re-stated to the customer as established.

    Returns the FULL merged dict, matching tool_node's contract: session_facts
    uses last-value semantics, so a partial write would silently drop identity.
    """
    entities = {k: v for k, v in (result.entities or {}).items() if v is not None}
    if not entities:
        return None

    facts = dict(state.get("session_facts") or {})
    # Only what the facts anchor and the ordering actually consume. A sub-agent
    # may return a great deal; this is not a general-purpose scratchpad, and
    # anything landing here is re-stated to the customer every single turn.
    for key in ("vehicle_id", "variant_id", "slot_id", "booking_ref", "vehicle_name"):
        if entities.get(key) is not None:
            facts[key] = entities[key]

    # The car's NAME, for the anchor's sentence. Taken from a successful step's
    # findings rather than the entity map, which carries ids only.
    if "vehicle_name" not in facts:
        for step in result.steps:
            name = (step.findings or {}).get("name") if step.ok else None
            if name and step.agent == "sales":
                facts["vehicle_name"] = name
                break

    return facts if facts != (state.get("session_facts") or {}) else None



# HOW MANY VEHICLE CARDS ONE ANSWER DRAWS. Mirrors runner._MAX_RECOMMENDATION_
# CARDS and cards.ts — the three must agree, and this is now the one that
# decides; the other two cap a payload that already fits.
MAX_CARDS = 3


def _select_cards(result) -> list[dict]:
    """The vehicles this answer shows, chosen ONCE from everything it searched.

    FOUND LIVE, 2026-08-31. Asked for "one with seven seats, one that is fuel
    efficient, and one that can go off-road", the specialist ran a search per
    clause and every result drew its own row — nine cards for one question. Two
    fixes narrowed it and neither reached the cause:

      the row trim  dropped cars a later row repeated. But a later search
                    RETURNS THE EARLIER CARS AT THE TOP ("trimming row [7,5,9]
                    to [9]"), so what survived was each search's TAIL — its
                    answer to its own clause was deleted as a duplicate.
      the strip cap  then took the first three of what was left, which came
                    entirely from search one. The other clauses got nothing.

    Together they showed X70 FL Plus, T1 i-DM and X50 under a reply naming X70
    FL Plus, T1 i-DM and T2 i-DM.

    ROUND-ROBIN, AND DEDUPE AFTERWARDS. Each search answers a different part of
    the question, so each gets its top hit before any gets a second. On that
    turn it yields 7, 5, 3 — X70 FL Plus, T1 i-DM, T2 i-DM — which is exactly
    what Aisha named. Not luck: she and the ranker read the same evidence and
    answer the same clauses, and it was the dedupe running BEFORE the selection
    that pulled them apart.

    CHOSEN HERE, WHERE THE ANSWER IS COMPOSED, not in the renderer. The strip
    used to be decided in the frontend, so by the time it had three Aisha had
    already written about a different three and could not have known. Selecting
    here lets the same list reach her prose (see compose_input's `cards_shown`)
    and the screen, so they cannot disagree.

    SELECTED, NEVER INVENTED. Entries are the tool's own rows, in the tool's own
    order — the same operation runner._without_drawn performs within one result.
    """
    per_search: list[list[dict]] = []
    for step in result.steps:
        if not step.ok:
            continue
        for item in step.evidence or []:
            if item.get("tool") != "search_vehicles":
                continue
            out = item.get("result")
            if not isinstance(out, dict):
                continue
            rows = out.get("matches")
            if not isinstance(rows, list) or not rows:
                rows = out.get("nearest_alternatives")
            if isinstance(rows, list) and rows:
                per_search.append([r for r in rows if isinstance(r, dict)])

    chosen: list[dict] = []
    seen: set = set()
    for rank in range(max((len(rows) for rows in per_search), default=0)):
        for rows in per_search:
            if rank >= len(rows):
                continue
            vid = rows[rank].get("vehicle_id")
            if vid is None or vid in seen:
                continue
            seen.add(vid)
            chosen.append(rows[rank])
            if len(chosen) >= MAX_CARDS:
                return chosen
    return chosen


def _orchestrator_node(state: AgentState, config: RunnableConfig) -> dict:
    """Answer an emit_briefs call by running the cascade (T5.1-T5.6).

    THIS IS A TOOL RESULT, NOT A SECOND CONVERSATION. The findings come back as
    the tool_result for Aisha's own emit_briefs call, so her NEXT generation —
    the ordinary post-tools edge — is the composition step. That reuses the
    streaming, the script guard, the sanitiser and the card anchors exactly as
    they are, rather than growing a second reply path that would have to
    re-implement all four and drift from them.

    IT NEVER RAISES. Every failure is a tool_result Aisha can speak to: a
    cascade that dies must produce "I could not check that just now", never a
    turn that ends with the opening line already delivered and nothing after it.
    """
    import dataclasses
    import json as _json

    from app import config as _config
    from app.agent import profile as _profile
    from app.db import SessionLocal
    from app.orchestrator import run as _turn_run
    from app.orchestrator import subagent, turn as _turn
    from app.orchestrator.judge import judge as _judge

    cfg = config.get("configurable", {})
    session_id = cfg.get("session_id", "?")
    block = _brief_block(state)
    last = state["messages"][-1]
    tool_uses = [
        b for b in (last.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]

    # Composition needs time too, and the ceiling is wall-clock from turn start
    # (AD-D). What is left after the reserve is what the steps may share.
    deadline = cfg.get("deadline_monotonic")
    remaining = (
        max(deadline - time.monotonic(), 1.0) if deadline is not None
        else float(AGENT_DEADLINE_SECONDS)
    )

    # THE CEILING (2026-08-31). See config.MAX_CASCADES_PER_TURN for the
    # measurement. Checked BEFORE anything is dispatched, so a refused round
    # costs nothing but the generation that asked for it.
    #
    # IT IS A TOOL_RESULT, NOT AN ERROR, and the wording matters: Aisha has an
    # answer in hand from the earlier round(s) and the right move is to give it,
    # not to apologise for a limit the customer cannot see. Telling her the
    # findings are already above her is what makes that possible — the evidence
    # from the first cascade is still in her context.
    cascades_so_far = state.get("cascades") or 0
    if _config.MAX_CASCADES_PER_TURN and cascades_so_far >= _config.MAX_CASCADES_PER_TURN:
        log.info(
            "cascade ceiling reached (%d) — refusing a further round, session=%s",
            cascades_so_far, session_id,
        )
        body = {
            "findings": [], "unavailable": [], "partial": False,
            "nothing_worked": False,
            "detail": (
                "You have already asked the specialists on this turn and their "
                "findings are above. Answer the customer from what you have "
                "now. If something is genuinely missing, say so plainly or ask "
                "them one short question — do not look it up again."
            ),
        }
        return {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.get("id"),
             "content": _json.dumps(body if b is block else CALLED_ALONGSIDE_ERROR,
                                    default=str)[:16000]}
            for b in tool_uses
        ]}]}

    payload: dict
    facts: dict | None = None
    try:
        briefs = list((block.get("input") or {}).get("briefs") or []) if block else []
        if not briefs:
            payload = {
                "findings": [], "unavailable": [], "partial": False,
                "nothing_worked": True,
                "detail": "No briefs were sent, so nothing was looked up.",
            }
        else:
            prof = _profile.from_state(state)
            as_dict = {
                k: v for k, v in dataclasses.asdict(prof).items() if v not in (None, "", [])
            }
            # THE CUSTOMER IS TOLD THE SPECIALISTS ARE RUNNING (2026-09-04).
            # Measured over 33 cascades on the test VM: Aisha emitted no opening
            # line before her briefs in 24 of them, so the screen showed an
            # empty bubble for the whole 10-30s cascade — and the UI's own
            # "working" label only appears on a tool_event, which on this path
            # arrives seconds later or (for the curated card tools) never.
            # This is the one moment the backend KNOWS a cascade is starting,
            # and it costs nothing to say so. Telemetry-shaped, like the
            # `cascade` chunk below: it carries a count, never a brief.
            try:
                _w0 = _safe_writer()
                if _w0:
                    _w0({"type": "cascade_start", "briefs": len(briefs)})
            except Exception:  # noqa: BLE001 — a status hint never breaks a turn
                log.debug("could not forward cascade_start", exc_info=True)
            _t_known = time.perf_counter()
            _known = _known_entities(state, cfg)
            _t_cascade = time.perf_counter()
            result = _turn.execute(
                briefs,

                subagent.make_runner(
                    cfg["db"], session_id,
                    customer_id=state.get("customer_id"),
                    profile=as_dict or None,
                    # A SESSION PER STEP, which is what lets a wave of briefs
                    # run at once — see make_runner. cfg["db"] is still the
                    # session the WRITES use (_execute_proposals, below); the
                    # read-only sub-agents get their own and never commit.
                    session_factory=SessionLocal,
                ),
                known=_known,
                judge=_judge,

                deadline_seconds=remaining,
                parent_writer=_stream_sink(_safe_writer()),
                # Selections started while Aisha was still typing the briefs
                # (see _stream_once and orchestrator/speculate). take() pops,
                # so a discarded generation's offers cannot leak into a later
                # cascade.
                # The agent node has already counted its generation, so 1 here
                # means "the brief came from this turn's first generation".
                preselected=_speculate_take(
                    session_id, first_generation=(state.get("iterations") or 0) <= 1
                ),
            )
            _t_compose = time.perf_counter()
            payload = _turn.compose_input(result)
            # THE STRIP, DECIDED ONCE (see _select_cards). Emitted here rather

            # than per search, and named in the payload so her prose and the
            # screen are anchored to the same three cars. Ahead of the compose
            # generation, so the row is queued before her first token — which is
            # when the UI commits it, so this costs nothing on the clock.
            _cards = _select_cards(result)
            if _cards:
                payload["cards_shown"] = [
                    {"vehicle_id": v.get("vehicle_id"), "name": v.get("name")}
                    for v in _cards
                ]
                _w = _safe_writer()
                if _w:
                    _w({
                        "type": "tool_event", "tool": "search_vehicles",
                        "input": {}, "output": {"matches": _cards},
                    })
            _t_writes = time.perf_counter()
            payload["actions"], _write_facts = _execute_proposals(result, state, cfg)
            facts = _fold_cascade_facts(state, result)
            # The node's own cost around the specialists: entity snapshot,
            # the cascade (specialist calls included), payload and cards, then
            # the writes. Booking state is re-derived from the DB several
            # times across these; this line is what says whether it matters.
            _t_end = time.perf_counter()
            log.info(
                "orchestrator overhead session=%s known=%dms cascade=%dms "
                "compose=%dms writes=%dms",
                session_id,
                int((_t_cascade - _t_known) * 1000),
                int((_t_compose - _t_cascade) * 1000),
                int((_t_writes - _t_compose) * 1000),
                int((_t_end - _t_writes) * 1000),
            )

            if _write_facts:
                # A HELD SLOT OUTRANKS A SEARCH RESULT, and that ordering is the
                # whole fix. _fold_cascade_facts carries what the specialists
                # LOOKED UP — for Sales, the first row of a ranked list, i.e.
                # what it recommended. These are read off the row the customer
                # actually landed on. When they disagree, the choice wins.
                facts = {**(facts or state.get("session_facts") or {}), **_write_facts}

            # THE CASCADE'S OWN ROUTING, on its way to turn_trace.steps — which
            # was null on every row ever written because nothing carried it out
            # of this node. run.trace_steps has built the structure since T3.5;
            # runner.record() has accepted a `steps=` argument the whole time;
            # the two were never introduced. It rides the custom stream because
            # that is the only channel out of a graph node that runner already
            # reads, and it carries NO BRIEF TEXT (see trace_steps) — the
            # decision and its cost, never the customer's words.
            try:
                _w = _safe_writer()
                if _w:
                    _w({"type": "cascade", "steps": _turn_run.trace_steps(result)})
            except Exception:  # noqa: BLE001 — telemetry never breaks a turn
                log.debug("could not forward cascade trace", exc_info=True)
            log.info(
                "orchestrator — session=%s briefs=%d ok=%d partial=%s",
                session_id, len(briefs),
                sum(1 for st in result.steps if st.ok), result.partial,
            )
    except Exception:  # noqa: BLE001 — the opening line is already delivered
        log.exception("orchestrator failed — session=%s", session_id)
        payload = {
            "findings": [], "unavailable": [], "partial": False,
            "nothing_worked": True,
            "detail": "The specialists could not be reached for this question.",
        }

    results: list[dict] = []
    for b in tool_uses:
        body = payload if b is block else CALLED_ALONGSIDE_ERROR
        results.append({
            "type": "tool_result",
            "tool_use_id": b.get("id"),
            "content": _json.dumps(body, default=str)[:16000],
        })

    # `iterations` is NOT advanced here. The bound counts GENERATIONS, and this
    # node calls no model of its own — the sub-agents run under their own
    # per-step budget and iteration ceiling. Counting the cascade against
    # Aisha's loop bound would cost her the composition turn it exists to
    # protect.
    out: dict = {
        "messages": [{"role": "user", "content": results}],
        # Counted here rather than in the edge, so a cascade that FAILED still
        # counts: a turn that cannot reach the specialists must not be free to
        # keep trying, which is the spiral in its other costume.
        "cascades": cascades_so_far + 1,
    }
    if facts is not None:
        out["session_facts"] = facts
    return out


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
    # The fan-out. Present in the graph on every path — reachable only when a
    # generation actually emits briefs, which only happens when Aisha was handed
    # emit_briefs, which only happens behind MULTI_AGENT_ENABLED. An unreachable
    # node costs nothing; a second compiled graph would have to be kept in step
    # with this one forever.
    g.add_node("orchestrator", _orchestrator_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent", _route_from_agent,
        {"tools": "tools", "orchestrator": "orchestrator", END: END},
    )
    g.add_conditional_edges("tools", _route_from_tools, {"agent": "agent", END: END})
    # Straight back to Aisha: the findings are a tool_result, and her next
    # generation composes the reply from them. No conditional — a cascade that
    # produced nothing still needs her to say so.
    g.add_edge("orchestrator", "agent")
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
