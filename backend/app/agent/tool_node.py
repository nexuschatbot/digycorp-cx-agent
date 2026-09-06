"""The custom tool node (AD-H) — the graph's ToolNode-equivalent.

This is NOT LangGraph's prebuilt ToolNode. It is a thin dispatcher over the
existing REGISTRY (app/tools), kept custom on purpose (AD-H): it lets the tools
stay one source of truth (native Anthropic schemas, not re-authored langchain
tools) and it is the single choke point where three things happen that the
prebuilt node could not do for us —

  1. AUDIT (rule 15, FR-AD-02). Every tool call — all 8 — is logged as a
     `tool_call` event ("the agent asked"), stamped with a per-invocation
     correlation_id. This is the read-audit hook the REGISTRY reserved, moved
     here verbatim; the choke point is the node, exactly as before.

  2. TRUSTED CONTEXT (CLAUDE.md's crux rule). `db` and `session_id` come from
     the graph's RunnableConfig, never from the model — they are absent from
     every tool schema, so Claude cannot forge them. For the two writes that act
     on an identified customer, `customer_id` is resolved SERVER-SIDE from the
     session -> customer_id binding held in graph state (minted by
     find_or_create_customer) and injected too; the model never supplies it.
     A write attempted before any customer is identified returns
     `customer_not_identified` as DATA — never a crash.

  3. CORRELATION. The same correlation_id logged in the `tool_call` event is
     threaded into the injected context the write services read, so their
     semantic audit rows (intent/consent/notification) carry the SAME id. One
     action, several facets, groupable in the console.

Errors are returned as tool_result DATA, never raised: a raised exception would
kill the live turn (CLAUDE.md "Return errors as data, not exceptions").
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from app.models import Actor, AuditType
from app.services import audit, recognition, slots
from app.tools import TOOL_FNS

log = logging.getLogger("app.agent.tool_node")


def _safe_writer():
    """The LangGraph custom-stream writer, or None outside a streaming context
    (graph.invoke in tests emits nothing rather than raising)."""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 — not streaming; no writer
        return None

# The writes that act on an already-identified customer: customer_id is resolved
# from the session binding and injected, never declared in their schema. If no
# customer is identified, _dispatch short-circuits with customer_not_identified —
# so an unidentified write is refused as DATA before it can run.
#
# forget_me is the one where this matters most. A model-supplied customer_id on a
# tool that HARD-DELETES a profile would let a sentence in the chat window choose
# whose data gets erased — an assertion with consequences, which is exactly what
# injecting trusted context exists to prevent.
#
# reschedule_booking / cancel_booking JOINED this set on 2026-07-23 (D1 PDPL
# identity hardening). For them the injected id is not the SUBJECT of the write
# but the OWNERSHIP KEY: the booking service refuses the change unless this
# customer owns the booking, so one session can never act on another customer's
# test drive. The short-circuit above is deliberate too — an unidentified session
# cannot reschedule or cancel at all.
#
# WHAT IDENTIFIES THEM CHANGED ON 2026-08-05, the guard did not. It was full name
# + mobile; it is now the TD reference OR the mobile, either alone
# (identify_by_reference_or_mobile). The ownership key is still a fact resolved
# server-side from a tool RESULT and still never modelled, so everything this set
# protects is unchanged — only the question the customer is asked is.
_INJECT_CUSTOMER_ID = {
    "create_booking",
    "record_consent",
    "save_memory",
    "forget_me",
    "reschedule_booking",
    "cancel_booking",
    # request_human_callback (rule 13): the injected id is the customer to call
    # back. An unidentified session is short-circuited to customer_not_identified
    # — you cannot arrange a callback for someone we cannot reach, which forces
    # find_or_create_customer (name + mobile) before the handoff. This one keeps
    # the NAME requirement after 2026-08-05: a callback is a person ringing a
    # person, and "someone will call you back" needs someone to ask for.
    "request_human_callback",
    # C3: puts a message on somebody's phone, so the recipient is resolved
    # from the session binding and never from the model.
    "send_brochure",
}

# get_booking takes customer_id as the VIEWER (whose eyes is this lookup
# through?), not as the subject, so it stays in its OWN set rather than joining
# _INJECT_CUSTOMER_ID. It is still injected as possibly-None — but as of
# 2026-07-23 the tool itself REFUSES a None viewer (identity_required): reads now
# require an identified session too, and once identified the lookup is scoped to
# that viewer's own bookings. The None case is no longer "a valid narrower view"
# but "not identified yet, go ask" — the tool decides that, not the tool node, so
# the read gate and the write gate stay side by side in the tools layer.
_INJECT_VIEWER_CUSTOMER_ID = {"get_booking"}

# The tools that BIND a session to a customer (2026-08-05). Two, because there
# are two journeys: find_or_create_customer starts a NEW booking (name + mobile),
# identify_by_reference_or_mobile reaches an EXISTING one (TD reference OR mobile,
# either alone, no name). They return the SAME envelope, so everything downstream
# — the binding below, session_facts, the identity anchor, memory recall — reads
# one shape and neither knows nor cares which door the customer came through.
#
# A SET RATHER THAN A SECOND `if`, deliberately: the binding is written twice in
# this file (the customer_id update and _fold_session_facts) and a third time in
# api/call_realtime._note_outcome. A literal tool name repeated at three sites is
# how one of them gets missed when a fourth door opens.
#
# confirm_returning_customer JOINED on 2026-08-14 (C10). Third door, same
# envelope: a returning customer on a recognised browser who has just said yes.
# It is here rather than in a special case precisely because of the note above —
# the fourth site would have been the one that got missed.
_IDENTITY_TOOLS = {
    "find_or_create_customer",
    "identify_by_reference_or_mobile",
    "confirm_returning_customer",
}

# confirm_returning_customer is the only tool that reads the device cookie, and it
# gets it INJECTED for the reason forget_me gets customer_id injected: a
# model-supplied device token would be an assertion about whose browser this is.
# Its input_schema is empty, so there is nothing for the model to supply anyway —
# this set is what makes that true rather than hoped for.
_INJECT_DEVICE_TOKEN = {"confirm_returning_customer"}

# Tools whose SERVICE emits its own semantic audit row(s); they forward the
# correlation_id so those rows share the choke-point tool_call's id. The other
# writes (find_or_create_customer, lock_slot) and all reads do not self-audit, so
# their only facet is the tool_call event — nothing to correlate into a service.
# reschedule_booking / cancel_booking join create_booking here: each writes an
# `intent` (+ notification/consent) row that must group under the tool_call
# (behaviour rule 15, FR-AD-02). As of 2026-07-23 they are ALSO in
# _INJECT_CUSTOMER_ID: the injected id is the ownership key the booking service
# checks before it will move or cancel anything (D1 PDPL identity hardening).
_FORWARD_CORRELATION = {
    "create_booking",
    "send_brochure",
    "record_consent",
    "reschedule_booking",
    "cancel_booking",
    "close_conversation",
    # save_memory writes an `intent` row; forget_me writes an `erasure` row (and,
    # via the consent withdrawal it triggers, a `consent` row too). All must group
    # under their tool_call — an erasure that floats free of the action that
    # caused it is a weaker PDPL record (S-08).
    "save_memory",
    "forget_me",
    # request_human_callback writes an `escalation` row (services/escalation); it
    # must group under its choke-point tool_call like every other write (rule 15).
    "request_human_callback",
}

# The terminal action (rule 19). Named here because the choke point enforces its
# one structural guard: CLEAN-END-ONLY. A close is only ever valid as the sole
# tool call of its turn — batched with any other tool it means the model is
# closing MID-SEQUENCE (still booking, still looking something up), which is the
# one way this action can do damage. Enforced here rather than asked of the
# prompt, for the same reason every other write guard is: the LLM is a
# conversational engine, not a source of correctness. The refusal is DATA, so the
# batched work still runs and the agent simply carries on talking.
_CLOSE_TOOL = "close_conversation"
# Presentation-only, and the SECOND tool that ends the turn (2026-08-14). Its
# question has already streamed as text in the same message, so a generation
# after it would only staple a spare sentence under the chips.
_OPTIONS_TOOL = "offer_options"
# THE TOOLS THAT ALREADY DREW THE ANSWER (browser run, 2026-09-06). Asked to move
# a booking, Aisha called get_available_slots — which draws a row of tappable
# times — and then, in the NEXT generation of the same turn, offer_options with
# "1 PM", "3 PM", "6:30 PM". The customer got the same three times twice, one row
# under the other, both live.
#
# HER OWN SCHEMA ALREADY FORBIDS IT ("Do not use it for times or showrooms
# either: those already draw their own tappable cards from the tools that fetch
# them, and a second set of buttons saying the same thing would be shown twice")
# and the alone-only veto below could not reach it: that tests ONE BATCH, and
# these two calls were an iteration apart.
#
# THE ROW THAT WAS DRAWN, NEVER WHAT SHE WROTE. Comparing "1 PM" against a chip
# reading "1:00 pm" means parsing a model's prose in two languages and two digit
# sets, and options.ts settled that argument for this codebase already: "Text is
# not a contract". A row of times is up or it is not — see _row_was_drawn, which
# reads the RESULT, because the tool having RUN is not the same thing and the
# difference cost a live turn.
#
# VEHICLE CARDS ARE DELIBERATELY ABSENT. A slot chip and a showroom's Select ARE
# the answer to the question being asked; a vehicle card opens a detail panel and
# answers nothing. "Tell me more about the T1 / about the Dashing / compare two"
# under a row of cars is the pattern working as intended (seen in the same run),
# and it must keep working.
#
# ⚠️ THE CASCADE PATH IS NOT COVERED, knowingly. A specialist's
# get_available_slots is replayed by graph._emit_slot_rows and never appears as a
# tool_use in Aisha's own history, so the walk below cannot see it. It fails
# OPEN — exactly today's behaviour — rather than guessing from emit_briefs, which
# would refuse the good pattern above after every cascade.
_AFFORDANCE_TOOLS = frozenset({
    "get_available_slots", "get_showrooms", "nearest_showroom",
})
# The THIRD terminal (2026-09-04), and the only one no tool NAME can express:
# whether the turn is over depends on the RESULT. A tool with nothing for the
# customer to hear — a location we could not place, a licence flag flipped
# server-side — says so by putting this key in its result, and the graph ends
# the turn instead of buying a generation the model can only fill by answering
# for the customer. Producers: location_tools.nearest_showroom (every
# ask_for_landmark branch) and slot_tools.acknowledge_licence_reminder; the key
# is a literal there because app.tools cannot import this module back.
_AWAITING_KEY = "awaiting_customer"
def _row_was_drawn(tool: str, result: object) -> bool:
    """Did THIS result actually put a tappable row on screen?

    THE RESULT, NOT THE CALL, and a live run is why (2026-09-06). The first
    version of this asked only whether the tool had RUN. It ran — and returned
    no times at all ("nothing later today"), so no row was drawn — and the chips
    she offered beside "shall I check from tomorrow?" were refused for competing
    with a row that did not exist. An empty answer is a valid, honest one (see
    get_available_slots' own schema) and leaves the screen with nothing on it.

    Mirrors the frontend's own emptiness rules — extractSlotChips,
    extractShowroomChoices, extractNearestShowroomChoices — because "was a row
    drawn" has exactly one right answer and it is theirs.

    NOT byte-for-byte theirs, and the gap is stated rather than hidden: they also
    drop individual rows missing an id or a name, so a non-empty list of
    malformed rows would draw nothing while this says it drew. That direction
    costs a chip row and nothing else — the refusal still ends the turn — and
    reaching into their per-row rules from here would couple this to a shape that
    is the UI's to change.
    """
    if not isinstance(result, dict):
        return False
    if tool == "get_available_slots":
        return bool(result.get("slots"))
    if tool == "get_showrooms":
        return bool(result.get("showrooms"))
    if tool == "nearest_showroom":
        return result.get("resolved") is True and bool(result.get("nearest"))
    return False


def _drew_its_own_answer_this_turn(state: dict) -> bool:
    """Has a row the customer can TAP already been drawn since they last spoke?

    Reads the same window _said_this_turn does, through the same
    _turn_context_key, so "this turn" has one definition here and not two.
    Failing to read it means "no row" — the veto below then behaves exactly as
    it did before, which is the safe direction for a presentation guard.
    """
    try:
        from app.agent import graph as _graph

        key = _graph._turn_context_key(state)
    except Exception:  # noqa: BLE001 — a chip must never cost a turn
        log.debug("could not read the turn boundary", exc_info=True)
        return False
    if key is None:
        return False

    window = (state.get("messages") or [])[int(key) + 1:]
    # tool_use_id -> tool name, for the affordance calls she made this turn. The
    # results arrive in a LATER message, keyed by that id, so the two halves are
    # matched rather than assumed to be adjacent.
    pending: dict[str, str] = {}
    for msg in window:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if msg.get("role") == "assistant":
            for b in content:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") in _AFFORDANCE_TOOLS
                ):
                    pending[str(b.get("id"))] = b["name"]
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            tool = pending.get(str(b.get("tool_use_id")))
            if tool is None:
                continue
            try:
                result = json.loads(b.get("content") or "null")
            except (TypeError, ValueError):
                continue
            if _row_was_drawn(tool, result):
                return True
    return False


def _tool_result(tool_use_id: str, result: dict, *, is_error: bool = False) -> dict:
    """Anthropic tool_result content block. Result is JSON so Claude reads it as
    data — the tool's own error envelope, not a stack trace."""
    block = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }
    if is_error:
        block["is_error"] = True
    return block


def _bound_tools(state: dict, cfg: dict) -> frozenset[str] | None:
    """Exactly the tool names the model was handed this turn, or None.

    THE SAME SOURCE AS THE SCHEMAS, deliberately, and it must stay that way: two
    lists that are supposed to agree will not, and the failure is silent in the
    direction that matters. Mirrors graph._agent_node's `tools=` expression
    branch for branch.

    None on the unrouted single-agent path — it holds the whole registry, so an
    allowlist would be every key in it and the check is pure cost.
    """
    from app.orchestrator import turn as _turn

    if _turn.enabled(voice_call=bool(cfg.get("voice_call"))):
        from app.tools import aisha_schemas

        return frozenset(s["name"] for s in aisha_schemas())

    agent = state.get("agent")
    if agent:
        from app.agent import tool_binding

        return frozenset(tool_binding.tools_for(agent))
    return None


def _dispatch(
    db,
    session_id: str,
    name: str,
    tool_input: dict,
    customer_id,
    correlation_id: str,
    device_token: str | None = None,
    fns: dict | None = None,
    allow: frozenset | set | None = None,
) -> dict:
    """Run one tool with trusted context injected; return its result as DATA.

    Never raises: unknown tools, unidentified writes, and tool exceptions all
    come back as error envelopes the agent can recover from conversationally.

    `fns` overrides the lookup table for callers whose tool surface is not the
    single agent's. Sub-agents pass SUBAGENT_TOOL_FNS, which is TOOL_FNS plus
    the five reads their cards declare — see app/tools/__init__.py for why those
    cannot simply join REGISTRY while the single-agent path is live. The DEFAULT
    is unchanged, so every injection, guard and audit rule below applies
    identically whoever is calling: this widens what can be looked up, never
    what may be done.

    `allow` IS THE BINDING, ENFORCED (found live, 2026-08-29). The architecture
    said "a prompt can be argued with; an absent tool cannot" — and that was not
    true here, because a tool was only ever absent from the SCHEMA LIST handed to
    the model, never from this dispatch table. Aisha on the multi-agent path is
    granted no commercial write, but her prompt is the ported single-agent
    journey prose and it tells her to call lock_slot and create_booking. She
    emitted the tool_use anyway. THIS FUNCTION RAN IT: `lock_slot` succeeded,
    inventory was held, and the orchestrator — the component that supposedly
    owns every commercial write — was never involved.

    A model emitting a tool it was not offered is not exotic; it is what a long
    prompt insisting on that tool reliably produces. So the allowlist has to be
    checked where the call is EXECUTED, not only where the schemas are chosen.
    None means "no restriction" and keeps every existing caller byte-identical.
    """
    if allow is not None and name not in allow:
        # DATA, NOT AN EXCEPTION, like every other refusal here. Aisha recovers
        # by doing it the way she is meant to — for a commercial write, that is
        # emit_briefs and letting the orchestrator decide.
        log.warning("refusing %r: not bound to this caller", name)
        return {
            "error": "tool_not_available",
            "tool": name,
            "detail": (
                f"{name} is not one of your tools on this turn. If it is a "
                "booking action — holding a slot, creating, moving or "
                "cancelling — use emit_briefs and let the specialists propose "
                "it; you do not perform those yourself."
            ),
        }

    fn = (fns if fns is not None else TOOL_FNS).get(name)
    if fn is None:
        return {"error": "unknown_tool", "tool": name}

    if name in _INJECT_CUSTOMER_ID and customer_id is None:
        # A write before any customer is identified. Data, not a crash — the
        # agent recovers by asking for one identifier and calling the identity
        # tool for the journey it is on (2026-08-05: there are two of them, and
        # naming only one here sent the model to ask for a name it does not need
        # in order to cancel a booking).
        return {
            "error": "customer_not_identified",
            "detail": (
                "No customer is identified in this conversation yet. To move or "
                "cancel an EXISTING booking, call "
                "identify_by_reference_or_mobile with their booking reference "
                "or their mobile, whichever they have — either alone is enough "
                "and no name is needed. To make a NEW booking, call "
                "find_or_create_customer with their name and mobile."
            ),
        }

    injected: dict = {}
    if name in _INJECT_CUSTOMER_ID:
        injected["customer_id"] = customer_id  # server-side, never modelled
    if name in _INJECT_VIEWER_CUSTOMER_ID:
        # May be None (unidentified session) — that is a valid, narrower view,
        # not an error. Server-side either way: the model cannot claim to be
        # someone in order to widen it.
        injected["viewer_customer_id"] = customer_id
    if name in _FORWARD_CORRELATION:
        injected["correlation_id"] = correlation_id
    if name in _INJECT_DEVICE_TOKEN:
        # May be None — the voice-call channel has no browser and no cookie. The
        # tool returns device_not_recognised, which is the honest answer and one
        # the agent already knows how to carry conversationally.
        injected["device_token"] = device_token

    try:
        return fn(db, session_id, **injected, **tool_input)
    except Exception as exc:  # noqa: BLE001 — a tool must never kill the turn
        return {"error": "tool_execution_failed", "tool": name, "detail": str(exc)}


def _recognition_refusal(state: dict) -> dict | None:
    """Why confirm_returning_customer may not bind yet (C10), or None to allow it.

    The one question this answers is whether an ANSWER can exist yet. The agent
    node stamps `recognition_offered_turn` with the customer-turn count at the
    moment it first rendered the question; a reply necessarily arrives on a LATER
    customer turn, so the bind is allowed exactly when the count has moved on.

    TWO REFUSALS, NOT ONE, because they are different mistakes and the recovery
    from each is different — a single message would send the model to ask a
    question it has no name for:

      * NOBODY TO RECOGNISE. No question was ever rendered, so this browser is
        linked to no one. The model called the tool on spec. It must say nothing
        about devices or recognition at all.
      * ASKED, NOT YET ANSWERED. The question went out on this very turn and the
        model answered it itself — the failure that actually fired in testing,
        on the word "hello". It stays refused across the turn's tool loops,
        because `customer_turns` only moves when a human speaks, so a model that
        is refused and simply calls again gets the same answer.

    A missing counter reads as 0, which fails closed for a checkpoint written
    before these fields existed: such a session just asks again next turn.
    """
    offered = state.get("recognition_offered_turn")
    if offered is None:
        return {
            "error": "nobody_to_recognise",
            "detail": (
                "This browser is not linked to anyone, so there is nothing to "
                "confirm and nobody to ask about. Do not mention recognition, "
                "devices or having seen them before — say none of it. Greet them "
                "as a new customer and carry on normally."
            ),
        }
    if (state.get("customer_turns") or 0) > offered:
        return None
    return {
        "error": "not_asked_yet",
        "detail": (
            "You have not asked them yet, or they have not answered. Ask in one "
            "short line whether they are still the person we recognised, let "
            "them reply, and call this only if they say yes. Until then you do "
            "not know who they are: do not greet them by name, do not mention "
            "their bookings or their car, and answer whatever they actually "
            "asked you."
        ),
    }


def _is_catalogue_read(name: str) -> bool:
    from app.tools import AISHA_CATALOGUE_READ_TOOLS

    return name in AISHA_CATALOGUE_READ_TOOLS


def _aisha_catalogue_reads(cfg: dict) -> bool:
    """Whether Aisha's own catalogue results establish facts this turn.

    True only on the multi-agent chat path with config.AISHA_CATALOGUE_READS on
    — the one path where she holds the reads AND a later TestDrive step will
    ask order.resolve whether vehicle_id is known. Voice and the single agent
    book directly and never consult `known`, so their facts stay byte-identical.
    """
    from app import config
    from app.orchestrator import turn as _turn

    return bool(config.AISHA_CATALOGUE_READS) and _turn.enabled(
        voice_call=bool(cfg.get("voice_call"))
    )


def _name_for_vehicle(node: object, vehicle_id: object) -> str | None:
    """The `name` on the first dict carrying this vehicle_id, or None."""
    if isinstance(node, dict):
        if node.get("vehicle_id") == vehicle_id and isinstance(node.get("name"), str):
            return node["name"]
        for value in node.values():
            hit = _name_for_vehicle(value, vehicle_id)
            if hit:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _name_for_vehicle(item, vehicle_id)
            if hit:
                return hit
    return None


def _merged_search_strip(results: list) -> list:
    """One strip of cars out of SEVERAL searches, deduped, first-seen order.

    Mirrors graph._select_cards, which does exactly this for the cascade —
    same cap, same rule, same reason. See the call site for what goes wrong
    without it.
    """
    from app.agent.graph import MAX_CARDS

    merged: list = []
    seen: set = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        rows = result.get("matches") or result.get("nearest_alternatives") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            vid = row.get("vehicle_id")
            if not isinstance(vid, int) or vid in seen:
                continue
            seen.add(vid)
            merged.append(row)
            if len(merged) >= MAX_CARDS:
                return merged
    return merged


def _fold_catalogue_facts(name: str, result: object, facts: dict) -> dict:
    """Aisha's OWN search / details / compare result establishes the car.

    FOUND LIVE ON THE FIRST REAL RUN (2026-09-03). With AISHA_CATALOGUE_READS
    on, she searched, compared and offered a slot herself — and then her
    TestDrive brief came back `blocked: needs booking_ref|vehicle_id`, twice,
    until the cascade ceiling and an apology. The Sales STEP used to be the
    thing that put vehicle_id into session_facts (graph._fold_cascade_facts
    reading subagent._harvest_entities off the step's tool results); with no
    Sales step, nothing did. This is that same fold applied to the same tool
    results at the place they now arrive: the harvest is the specialist's own
    (first row of a ranked search wins, offers-not-facts respected), and the
    name is read off the row the id came from — tool result only, never prose.
    """
    if not isinstance(result, dict):
        return facts
    from app.orchestrator.subagent import _harvest_entities

    found = _harvest_entities("sales", [result], [name])
    if not found:
        return facts
    updated = dict(facts)
    for key in ("vehicle_id", "variant_id"):
        if found.get(key) is not None:
            updated[key] = found[key]
    vehicle_name = _name_for_vehicle(result, found.get("vehicle_id"))
    if vehicle_name:
        updated["vehicle_name"] = vehicle_name
    return updated


def _with_cards_shown(result: dict, strip: list) -> dict:
    """`result` plus the cars the card row ACTUALLY DREW, as id and name.

    FOUND LIVE, session 9841adc7 on 2026-09-04. Asked for "any other option",
    Aisha wrote about the Jetour T1 i-DM while the row on screen still showed
    the X70 FL Plus, X50 and T2 Luxury Plus — the three she had already shown.
    The T1 i-DM is a PHEV and her own search had passed fuel=petrol, so it was
    never in the result at all; she had it from the retrieval passages. The
    customer read a paragraph about one car beside pictures of three others.

    THE CASCADE ALREADY SOLVED THIS AND THIS PATH NEVER GOT IT. compose_input
    hands the specialists' answer back with `cards_shown`, and aisha/v2.md tells
    her to lead with those cars — but that key is built only in
    _orchestrator_node, so when she searches the catalogue HERSELF
    (config.AISHA_CATALOGUE_READS) nothing tells her what is on screen. This is
    the same fact, delivered on the same tool result that drew the row.

    TAKEN FROM THE STRIP, NOT FROM `matches`. The row is what the customer can
    see, and it is capped and deduped (_merged_search_strip / MAX_CARDS) — so
    naming `matches` here would claim pictures that were never drawn, which is
    the defect wearing the other hat.

    COPIES, NEVER MUTATES: the same dict is streamed to the browser, and the
    card anchor and the reload row are both derived from it.
    """
    shown = [
        {"vehicle_id": row.get("vehicle_id"), "name": row.get("name")}
        for row in strip
        if isinstance(row, dict) and row.get("vehicle_id") is not None
    ]
    if not shown:
        return result
    return {**result, "cards_shown": shown}


def _fold_session_facts(db, name: str, result: object, facts: dict) -> dict:
    """Fold ONE tool RESULT into session_facts — the date-anchor pattern applied to
    identity (2026-07-24). Returns a NEW dict; the caller replaces its running copy.

    ONLY tool RESULTS are read here, never the model's arguments and never the
    prompt, so session_facts is grounded exactly like every other fact (rule 2).
    "Latest result wins", but a present value never clobbers a good one with a
    blank: a re-identify that returns a null Arabic name must not erase the English
    name we already hold, so each field is written only when the result carries it.
    """
    if not isinstance(result, dict):
        return facts

    # Identity: name + number + title, from either identity tool's `customer`.
    if name in _IDENTITY_TOOLS and isinstance(result.get("customer"), dict):
        c = result["customer"]
        updated = dict(facts)
        if c.get("customer_id") is not None:
            updated["customer_id"] = c["customer_id"]
        chosen_name = c.get("name_en") or c.get("name_ar")
        if chosen_name:
            updated["name"] = chosen_name
        chosen_title = c.get("title") or c.get("title_ar")
        if chosen_title:
            updated["title"] = chosen_title
        if c.get("mobile"):
            updated["mobile"] = c["mobile"]
        return updated

    # The chosen car/showroom/daypart, read off the locked Slot row (the result's
    # own dict does not carry the vehicle) via the slots service (AD-I).
    if name == "lock_slot" and result.get("locked") is True:
        slot_id = (result.get("slot") or {}).get("slot_id")
        new = slots.facts_for_slot(db, slot_id)
        return {**facts, **new} if new else facts

    # A booking CREATED in this conversation (2026-08-03). The bookings anchor is
    # re-read from the DB on EVERY generation and headed "what this customer has",
    # so a booking confirmed seconds ago arrives in front of the model in exactly
    # the same words as one made three weeks ago — the anchor has no notion of when
    # a booking came into existence. Live, that made the agent disown its own work:
    # it read two refs it had itself just created as bookings the customer walked in
    # with, and explained the coincidence to itself as duplicate detection, i.e.
    # invented a mechanism to account for a fact it had been handed wrong (rule 2).
    # Authorship is knowable at exactly one moment, here, so it is recorded here.
    #
    # `created` is the discriminator and `idempotent` deliberately is not: an
    # idempotent result means the booking ALREADY existed and we merely matched it,
    # which is precisely the case that must not be claimed as this session's work.
    #
    # This does not reopen the "read bookings from the DB, not from facts" decision
    # in the anchor above: the DB still owns every mutable field (a booking can be
    # moved or cancelled by the console, a reminder tap or the WhatsApp handoff).
    # Only WHO MADE IT is accumulated, which is the one property that cannot change
    # once true.
    if name == "create_booking" and result.get("created") is True:
        ref = (result.get("booking") or {}).get("booking_ref")
        if not ref:
            return facts
        booked = list(facts.get("booked_refs") or [])
        if ref not in booked:
            booked.append(ref)
        return {**facts, "booked_refs": booked}

    # A confirmed erasure (or the idempotent already-erased): the customer is gone,
    # so everything we carried about them goes too. This is the IN-TURN half of rule
    # 12's guarantee — services/erasure deletes the checkpoint thread mid-turn, but
    # the graph re-saves the checkpoint at end of turn, so clearing the LIVE state
    # here is what stops these facts being re-persisted for the session.
    if name == "forget_me" and result.get("forgotten") is True:
        return {}

    return facts


def tool_node(state: dict, config: RunnableConfig) -> dict:
    """Execute every tool_use block in the last assistant message.

    Reads trusted context (db, session_id) from `config` and the session's
    customer_id binding from `state`; returns the tool_result message plus any
    updated binding. Commits once at the end — the tool node is the executor that
    owns the transaction (booking.create and friends flush but never commit), so
    a turn's writes and their audit rows land or roll back together.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    session_id = cfg["session_id"]
    cancel = cfg.get("cancel_event")  # AD-E per-request abort flag
    # The slot_id from a tapped chip (Option B). Executor-injected trusted context
    # like db/session_id/customer_id — it PINS lock_slot's id so the model's prose
    # parse can never pick a different slot. It does NOT force the lock: it only
    # replaces the id, then lock_slot's own availability guard runs and may still
    # return slot_no_longer_available (a slot taken between chip render and tap).
    provided_slot_id = cfg.get("provided_slot_id")
    # The device cookie for THIS request (C10). Trusted context like db/session_id
    # — read off an HttpOnly cookie by the API layer, never modelled, never
    # checkpointed. None on the voice-call channel, which has no browser.
    device_token = cfg.get("device_token")
    writer = _safe_writer()

    # The session -> customer_id binding. Read at entry; may be updated below the
    # moment find_or_create_customer returns, so a (rare) same-turn
    # identify-then-write still injects the freshly-minted id.
    customer_id = state.get("customer_id")

    # session_facts (2026-07-24): the confirmed facts the prompt anchor injects.
    # Keep the pre-turn copy so a failed commit can roll the facts back with the
    # writes they describe; the running copy accumulates each tool result below.
    original_facts = dict(state.get("session_facts") or {})
    session_facts = dict(original_facts)

    last = state["messages"][-1]
    tool_uses = [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]

    # Terminal-turn flag (rule 19). Set by a successful close_conversation and
    # read by the graph's post-tools edge, which ENDs the turn instead of running
    # the agent again. Without it the model gets a generation AFTER its farewell
    # and fills the silence — a second goodbye stapled to the first.
    closed = False
    # Per-tool "this result had nothing for the customer" (see _AWAITING_KEY),
    # collected here and collapsed into one terminal after the loop.
    awaiting: list[bool] = []
    # Terminal-turn flag for offer_options, kept separate from `closed` because
    # the two mean different things: the conversation is still very much open —
    # we are waiting on an answer — it is only THIS TURN that has nothing left to
    # say. Conflating them would disable the composer on a question.
    options_offered = False

    # SEVERAL SEARCHES IN ONE GENERATION DRAW ONE STRIP, NOT ONE EACH (found
    # live 2026-09-03). Aisha ran search_vehicles twice for a single question —
    # once for plug-in hybrids, once without — and each tool_event drew its own
    # row: four cards under one answer, where the product rule is at most three.
    # The cascade already solved this for specialists (turn.CURATED plus
    # graph._select_cards); with the catalogue reads back in her own hands the
    # same question needs the same answer. ONLY when there is more than one
    # search in the batch: a single search still streams exactly as it did.
    curated_searches: list = []
    curating = (
        _aisha_catalogue_reads(cfg)
        and sum(1 for b in tool_uses if b.get("name") == "search_vehicles") > 1
    )
    # WHAT THE CUSTOMER CAN SEE, TOLD TO THE ONE WHO WRITES THE SENTENCE
    # (2026-09-06) — see _with_cards_shown for the live failure. Gated on the
    # SAME flag as `curating` above, so the single-agent path's tool results
    # stay byte-identical: that path is the kill switch and its card selection
    # is deliberately frozen (OPEN-ISSUES #4).
    #
    # A curated batch cannot be annotated inside the loop — the strip is only
    # known once every search in the generation has run — so those entries are
    # remembered by position and rebuilt after it.
    annotating = _aisha_catalogue_reads(cfg)
    curated_slots: list[tuple[int, str, dict]] = []

    results: list[dict] = []
    for idx, block in enumerate(tool_uses):
        # (0) ABORT BEFORE EACH DISPATCH (AD-E). A client that dropped mid-turn
        # must NOT trigger a write — for create_booking that would mean booking a
        # customer who is already gone, a row that outlives the conversation. So
        # we check BEFORE auditing or dispatching, and stop. We synthesise an
        # aborted tool_result for this and every remaining tool_use so the
        # checkpoint stays consistent (each tool_use keeps a tool_result) while
        # NOTHING is written and NOTHING is audited for the un-dispatched tools.
        if cancel is not None and cancel.is_set():
            log.info(
                "tool node ABORTED before dispatching %s (+%d pending) — session=%s, NOTHING written",
                block.get("name", "?"), len(tool_uses) - idx - 1, session_id,
            )
            for pending in tool_uses[idx:]:
                results.append(
                    _tool_result(
                        pending.get("id", ""),
                        {"error": "aborted", "detail": "client disconnected before dispatch"},
                        is_error=True,
                    )
                )
            break

        name = block.get("name", "")
        tool_input = block.get("input", {}) or {}
        tool_use_id = block.get("id", "")
        correlation_id = uuid4().hex

        # (0b) PIN THE SLOT (Option B). On a turn carrying a tapped-chip slot_id,
        # override lock_slot's id with the pinned one BEFORE auditing and
        # dispatching — the customer's prose ("Monday 11 AM at Al Quoz") is for
        # humans, this id is the truth. Scoped to lock_slot; nothing else, no
        # other turn. The override swaps the id only — the availability guard in
        # slots.lock still runs (stale-slot cases fail gracefully as data).
        pinned = name == "lock_slot" and provided_slot_id is not None
        if pinned:
            tool_input = {**tool_input, "slot_id": provided_slot_id}

        # (1) AUDIT — the choke-point tool_call, for every dispatched tool. "The
        # agent asked" is the fact here; the result is a separate concern. The
        # audited input is the EFFECTIVE input (pinned id included), so grounding
        # stays provable; slot_id_pinned marks a chip tap vs a typed selection.
        audit.record(
            db,
            session=session_id,
            actor=Actor.agent,
            type=AuditType.tool_call,
            payload={"tool": name, "input": tool_input, **({"slot_id_pinned": True} if pinned else {})},
            correlation_id=correlation_id,
        )

        # (2) RESOLVE the result — dispatch with trusted context injected, or
        # return the appropriate error-as-data. Never raises past here.
        #
        # CLEAN-END-ONLY (rule 19): a close batched with other tool calls is the
        # model closing mid-sequence. Refuse it as data and let its siblings run
        # — the conversation stays open and the agent keeps talking, which is
        # exactly the recovery we want.
        #
        # TWO tools the choke point can veto, as of 2026-08-14 (C10); everything
        # else it merely dispatches. Both vetoes are the same shape and there for
        # the same reason: a rule the prompt states but cannot enforce, where
        # getting it wrong is visible to the customer. Adding a third should feel
        # expensive — this is a choke point, not a rules engine.
        if name == _OPTIONS_TOOL and len(tool_uses) > 1:
            # ALONE-ONLY, exactly like the close below and for the same reason:
            # offer_options ENDS the turn, so batching it with real work would
            # strand that work unspoken — the tool would run and the agent would
            # never get the generation in which to tell the customer about it.
            # Refused as data; the siblings run and the turn continues normally.
            result = {
                "error": "options_not_alone",
                "detail": (
                    "Offer the choices on their own, once you have asked the "
                    "question in words and there is nothing else to do. Finish "
                    "the other work first, then ask, then offer."
                ),
            }
        elif name == _OPTIONS_TOOL and _drew_its_own_answer_this_turn(state):
            # THE SAME RULE, ONE ITERATION WIDER (see _AFFORDANCE_TOOLS). The
            # times or the branches are already on screen as chips the customer
            # can tap; a second row saying the same thing is the duplicate a
            # browser run put in front of a customer. Refused as DATA and logged;
            # the question she asked in words stands, with the row under it.
            log.info(
                "offer_options REFUSED (a tappable row is already up) — session=%s",
                session_id,
            )
            result = {
                "error": "answer_already_offered",
                "detail": (
                    "The times or branches you fetched are already on screen as "
                    "buttons the customer can tap, so there is nothing to add "
                    "here — a second row would show the same answers twice. Ask "
                    "anything else you need in words."
                ),
                # AND THE TURN STILL ENDS (live run, 2026-09-06, the first cut of
                # this veto). A successful offer_options IS the terminal —
                # options_offered ends the turn and the customer answers. Refusing
                # it took that terminal away, and the generation it handed back
                # had exactly one thing left to fill it: she wrote "Yes please"
                # in her own bubble, answering her own question, and acted on it.
                # That is the failure _AWAITING_KEY exists for (graph.AgentState),
                # so the refusal carries the marker the success carried: the
                # question is on screen, the row is under it, the next move is
                # the customer's.
                _AWAITING_KEY: True,
            }
        elif name == _CLOSE_TOOL and len(tool_uses) > 1:
            log.info(
                "close_conversation REFUSED (batched with %d other tool call(s)) — session=%s",
                len(tool_uses) - 1, session_id,
            )
            result = {
                "closed": False,
                "reason": "close_not_alone",
                "detail": (
                    "A conversation can only be closed on its own, once everything "
                    "else is finished. Complete the other work first; close only "
                    "when the customer is done and you have said goodbye."
                ),
            }
        elif name == "confirm_returning_customer" and _recognition_refusal(state):
            # C10's structural guard (see AgentState.customer_turns). Refused as
            # DATA with the recovery spelled out, the same way an unidentified
            # write is refused above — the model carries on conversationally and
            # the customer never sees a failure.
            result = _recognition_refusal(state)
        else:
            result = _dispatch(
                db, session_id, name, tool_input, customer_id, correlation_id,
                device_token=device_token, allow=_bound_tools(state, cfg),
            )

        # (2b) Mint / refresh the session binding the moment identity is known.
        if name in _IDENTITY_TOOLS and isinstance(result, dict) and "customer" in result:
            customer_id = result["customer"].get("customer_id", customer_id)

        # (2c) A close that landed ends the turn (see `closed` above). An
        # idempotent repeat counts too: the conversation is over either way.
        if name == _CLOSE_TOOL and isinstance(result, dict) and result.get("closed") is True:
            closed = True

        # (2c-ii) Chips were drawn, so the turn is done: the question is already
        # in the stream and the customer's answer is the next thing that should
        # happen. A refused call (batched, or too few options) sets nothing and
        # the turn carries on as normal.
        if name == _OPTIONS_TOOL and isinstance(result, dict) and result.get("offered") is True:
            options_offered = True

        # (2c-iii) DID THIS RESULT LEAVE ANYTHING TO SAY (2026-09-04). Recorded
        # per tool, judged for the batch below: one silent result among four
        # informative ones means nothing, but a batch where EVERY result is
        # silent is a turn with no reply left in it.
        awaiting.append(bool(isinstance(result, dict) and result.get(_AWAITING_KEY) is True))

        # (2d) Fold this result into the session facts the anchor injects next turn
        # (identity from find_or_create_customer, the car from lock_slot, cleared by
        # a confirmed forget_me). Results only, never the model's args (rule 2).
        session_facts = _fold_session_facts(db, name, result, session_facts)
        if _is_catalogue_read(name) and _aisha_catalogue_reads(cfg):
            # The car she just looked up is now established, so a TestDrive
            # brief later this session is not blocked for want of vehicle_id
            # — see _fold_catalogue_facts.
            session_facts = _fold_catalogue_facts(name, result, session_facts)

        # (3) STREAM the tool activity (AD-B): tool_event for the console/UI, and
        # a booking event ONLY when a booking was NEWLY created — an idempotent
        # replay returns a ref too, but re-firing `booking` would re-freeze an
        # already-frozen timer or read as a second booking in the UI.
        if writer:
            if curating and name == "search_vehicles":
                # Held back for the single merged strip emitted after the loop.
                curated_searches.append(result)
            else:
                writer({"type": "tool_event", "tool": name, "input": tool_input, "output": result})
            if name == "create_booking" and isinstance(result, dict) and result.get("created") is True:
                ref = (result.get("booking") or {}).get("booking_ref")
                if ref:
                    writer({"type": "booking", "booking_ref": ref})
            # And a `closed` event on a genuine close (rule 19) — the signal the
            # UI needs to disable the composer the moment the agent says goodbye.
            # Same discipline as `booking` above: fired on the real transition
            # only, never on an idempotent replay.
            if name == _CLOSE_TOOL and isinstance(result, dict) and result.get("closed") is True:
                if not result.get("already_closed"):
                    writer({"type": "closed"})

        # THE MODEL'S COPY MAY CARRY `cards_shown`; THE BROWSER'S NEVER DOES.
        # The streamed event above is what draws the row and what the reload
        # anchor is derived from, so it is left exactly as the service returned
        # it — the annotation is for the generation that writes the reply.
        model_result = result
        if annotating and name == "search_vehicles" and isinstance(result, dict) and writer:
            if curating:
                curated_slots.append((len(results), tool_use_id, result))
            else:
                model_result = _with_cards_shown(result, _merged_search_strip([result]))

        results.append(_tool_result(tool_use_id, model_result, is_error=isinstance(result, dict) and "error" in result))

    # (4) REMEMBER THIS BROWSER (C10). Once per turn, at the end, rather than
    # inside the identity branch above — because the two facts it needs arrive in
    # either order and usually the wrong one: find_or_create_customer identifies
    # them on turn 3 and record_consent grants memory on turn 9, so a link written
    # at identification time would be refused for want of consent and never
    # retried. Running it after every tool turn makes the order irrelevant: the
    # first turn where BOTH hold is the one that writes the row.
    #
    # recognition.link is consent-gated and never raises — no consent, no row, and
    # a failure costs the customer nothing but a stranger's greeting next visit.
    # The one strip, chosen across every search this generation made. Shaped
    # like graph._select_cards' synthetic event so the console, the card
    # anchor in runner and cards.ts all read it exactly as they read a search.
    if writer and curated_searches:
        strip = _merged_search_strip(curated_searches)
        # EVERY SEARCH IN THE BATCH IS TOLD THE SAME STRIP, because one row is
        # drawn for all of them — see _with_cards_shown. Rebuilt by position so
        # each tool_use keeps the tool_result it already had if the strip is
        # empty (a search that matched nothing draws no row and claims none).
        for index, use_id, raw in curated_slots:
            annotated = _with_cards_shown(raw, strip)
            if annotated is not raw:
                results[index] = _tool_result(
                    use_id, annotated, is_error="error" in raw
                )
        if strip:
            writer({
                "type": "tool_event",
                "tool": "search_vehicles",
                "input": {},
                "output": {"matches": strip},
            })

    if device_token and customer_id is not None:
        recognition.link(
            db, token=device_token, customer_id=customer_id, session_id=session_id
        )

    # THE TURN IS OVER IF THERE IS NOTHING LEFT TO SAY (2026-09-04). Three
    # conditions, and all three earn their place:
    #   * every dispatched tool came back silent — one informative sibling and
    #     the model has a real answer to give, so it gets its generation;
    #   * the batch ran to completion — an abort left the tail undispatched, and
    #     the cancel edge ends that turn anyway;
    #   * she has already spoken this turn, so ending here leaves a question on
    #     screen rather than an empty bubble the client drops. Reuses the same
    #     _said_this_turn the already-said block is built from — one definition
    #     of "what she has said since the customer spoke", not two. Failing to
    #     read it means "silent", which is today's behaviour: the turn carries on.
    awaiting_customer = False
    if awaiting and len(awaiting) == len(tool_uses) and all(awaiting):
        try:
            from app.agent import graph as _graph

            awaiting_customer = bool(_graph._said_this_turn(state))
        except Exception:  # noqa: BLE001 — never cost a turn over a terminal
            log.debug("could not read what was said this turn", exc_info=True)

    # Executor owns the transaction: commit the turn's writes + audit rows once.
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — surface as data, never crash the turn
        db.rollback()
        results = [
            _tool_result(
                b.get("id", ""),
                {"error": "commit_failed", "detail": str(exc)},
                is_error=True,
            )
            for b in tool_uses
        ]
        # The writes those facts describe were rolled back, so the facts must not
        # advance either — revert to the pre-turn snapshot.
        session_facts = original_facts
        # Every result is now an error the model has never seen. That IS
        # something to say, so the turn must not end here.
        awaiting_customer = False

    content: list = list(results)
    # WHAT SHE HAS ALREADY SAID THIS TURN, riding behind the tool results
    # (2026-09-04). Seen twice in browser runs: "Just remember your licence.
    # May we send you WhatsApp reminders?" sealed and sent, the licence tool
    # ran, and the generation reading THIS message asked the question again.
    # The rule against it sits ~10k tokens up the prompt; the sentences
    # themselves, right here where she reads the result, are what she acts
    # on. The orchestrator hands the same list back with its findings. Only
    # on the multi-agent path, so the single-agent contract is byte-stable,
    # and only when there is something sealed to repeat.
    try:
        from app.agent import graph as _graph
        from app.orchestrator import turn as _turn

        if _turn.enabled(voice_call=bool(cfg.get("voice_call"))):
            said = _graph._said_this_turn(state)
            if said:
                content.append({"type": "text", "text": _graph.already_said_block(said)})
    except Exception:  # noqa: BLE001 — a reminder must never cost a turn
        log.debug("could not attach the already-said block", exc_info=True)

    return {
        "messages": [{"role": "user", "content": content}],
        "customer_id": customer_id,
        "closed": closed,
        "options_offered": options_offered,
        "awaiting_customer": awaiting_customer,
        "session_facts": session_facts,
    }



__all__ = ["tool_node"]
