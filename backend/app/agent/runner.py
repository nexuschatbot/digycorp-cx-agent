"""run_turn() — one agent turn, transport-neutral.

Extracted verbatim from app/api/chat.py's worker() so the SSE chat route and
the voice-call WebSocket gateway share ONE implementation of the turn
lifecycle: customer transcript write, per-turn language, graph invocation,
event dispatch, card anchoring, agent transcript write, voice-customer
backfill, and the abort/rollback semantics. The route owns transport (thread +
queue + SSE frames, or WS sends); this module owns the turn.

Events are delivered through `on_event(event, data)` in stream order, with the
same names and payloads as CLAUDE.md's SSE contract: token, tool_event,
booking, text_break, closed, done, error. The caller maps them onto its wire
format; nothing here knows what that is.

SESSION-PER-TURN (the heisenbug fix): run_turn opens its OWN SessionLocal(),
uses it for this turn only, and closes it in finally. It is never a
request-scoped Session and never crosses a thread or outlives the turn.

ABORT (AD-E): the caller supplies a threading.Event. When it is set, the agent
node stops within one token and the tool node stops BEFORE any dispatch; the
turn's partial transcript is rolled back, not persisted.
"""

from __future__ import annotations

import json as _json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from app.agent import StreamAborted, build_config, build_graph, new_sqlite_checkpointer
from app.agent.router import route as _route
from app import config as _config
from app.services import takeover as takeover_service
from app.services import turn_trace as _turn_trace
from app import lang as lang_service
from app.config import AGENT_CHECKPOINT_PATH
from app.db import SessionLocal
from app.models import ConversationTurn, Language, Speaker
from app.services import voice as voice_service

log = logging.getLogger("app.agent.runner")


# What the MODEL is shown when a voice note could not be transcribed. Chosen
# over a client-side canned line (the route taken for the idle nudge) because a
# failure mid-conversation deserves Aisha's own voice and the thread's own
# language, which a fixed string cannot supply.
#
# IT IS A NOTE ABOUT THE CUSTOMER, NEVER WORDS PUT IN THEIR MOUTH. Synthesising
# a plausible "sorry, could you repeat that" as if they had typed it would be
# the model inventing a fact about what was said — exactly rule 2's prohibition,
# and the transcript would then record a sentence nobody uttered. The brackets
# and the explicit prohibition are what keep the recovery honest.
#
# The customer's ConversationTurn stores EMPTY text, so the transcript shows the
# player alone: the honest record is that a recording exists and no words were
# recovered from it.
_VOICE_FAILED_NOTE = (
    "[System note, not spoken by the customer: they sent a voice note but it "
    "could not be transcribed. Ask them — in the language you have been using "
    "with them — to try again or type it instead. Do NOT guess or invent what "
    "they might have said.]"
)


def _attach_voice_customer(db, graph, config, session_id: str) -> None:
    """Stamp this session's identified customer onto its voice notes (rule 12).

    Identification happens mid-conversation, usually several turns AFTER the
    first recording — "I want to test drive the T2" comes long before a mobile
    number. Those early recordings are stored with customer_id NULL, and an
    erasure request searching by customer would miss exactly the ones most
    likely to exist. Run every turn (it is a no-op once attached) so the link is
    made as soon as there is a customer to link to.

    Never allowed to break a turn: a customer heard a real answer, and failing
    to backfill a pointer is not a reason to lose it. The rollback path in
    delete_for_customer covers the miss anyway — it matches on session too.
    """
    try:
        snapshot = graph.get_state(config)
        state = snapshot.values if snapshot else {}
        customer_id = state.get("customer_id") if isinstance(state, dict) else None
        if customer_id:
            voice_service.attach_customer(
                db, session_id=session_id, customer_id=customer_id
            )
    except Exception:  # noqa: BLE001 — a backfill must never cost a reply
        log.warning("voice customer backfill failed — session=%s", session_id, exc_info=True)


# Mirrors MAX_RECOMMENDATION_CARDS in the frontend's cards.ts. The two must agree
# or a reload would redraw a different number of cards than the turn originally
# showed — the one thing this feature exists to prevent.
_MAX_RECOMMENDATION_CARDS = 3


def _card_anchor(tool: str, output: object) -> dict | None:
    """Which cards did this tool_event render? Two entry shapes, one union:

        {"vehicle_ids": [int], "variant": "compact"|"full"}   # vehicle cards
        {"booking_refs": ["TD-2026-0142"]}                    # booking cards

    Readers key off WHICH KEY IS PRESENT, so the booking shape was additive — a
    row written before booking cards existed still reads correctly.

    IDs ONLY (see ConversationTurn.cards): the reload resolves them through
    GET /api/vehicles/{id} and GET /api/booking/{ref} rather than replaying a
    frozen copy of the card. For bookings that is not merely consistent, it is
    the SECURITY property: a booking card holds a name and a mobile, and
    re-fetching means the PII gate in services/booking._booking_dict is applied
    again on reload. Snapshotting the card into the transcript would have frozen
    a customer's phone number into a table that no gate ever reads.

    Deliberately mirrors extractCards/extractSearchCards in the frontend's
    cards.ts, INCLUDING the shapes that produce nothing — an `error` envelope
    drew no cards live and must draw none on reload. Anything unrecognised returns None, so a tool that grows a new
    output shape silently records no cards instead of inventing them (FR-CX-07).
    """
    if not isinstance(output, dict):
        return None

    if tool == "get_booking":
        bookings = output.get("bookings")
        if not isinstance(bookings, list):
            return None
        refs = [
            b["booking_ref"]
            for b in bookings
            if isinstance(b, dict) and isinstance(b.get("booking_ref"), str)
        ]
        # No cap, unlike recommendations: a customer has one or two live
        # bookings, and truncating the list of YOUR OWN bookings would be a
        # different kind of wrong from truncating a suggestion list.
        return {"booking_refs": refs} if refs else None

    # A booking that was just MADE or MOVED draws the same card as one that was
    # looked up, so it anchors the same way and survives a reload.
    #
    # cancel_booking is deliberately absent: a cancelled booking draws no card,
    # and GET /api/booking/{ref} 404s it anyway — so anchoring one would record a
    # pointer that resolves to nothing on every future reload.
    #
    # BOTH SIDES OF A RESCHEDULE END UP ANCHORED (the create turn and the
    # reschedule turn carry the same ref), which is fine because the reload
    # renders only the LAST row for a ref — see rebuildTurns in the frontend.
    # The earlier card CANNOT be rebuilt in any case: the endpoint returns the
    # booking's current state, so a "historic" card would show today's slot
    # wearing a superseded style, which is worse than not drawing it.
    if tool in ("create_booking", "reschedule_booking"):
        booking = output.get("booking")
        if not isinstance(booking, dict):
            return None
        ref = booking.get("booking_ref")
        return {"booking_refs": [ref]} if isinstance(ref, str) else None

    if tool == "get_vehicle_details":
        vehicle = output.get("vehicle")
        if isinstance(vehicle, dict):
            source = [vehicle]
        elif isinstance(output.get("candidates"), list):
            source = output["candidates"]
        else:
            return None
        variant = "full"
    elif tool == "search_vehicles":
        matches = output.get("matches")
        alternatives = output.get("nearest_alternatives")
        matches = matches if isinstance(matches, list) else []
        alternatives = alternatives if isinstance(alternatives, list) else []
        source = (matches or alternatives)[:_MAX_RECOMMENDATION_CARDS]
        variant = "compact"
    else:
        return None

    ids = [
        v["vehicle_id"]
        for v in source
        if isinstance(v, dict) and isinstance(v.get("vehicle_id"), int)
    ]
    return {"vehicle_ids": ids, "variant": variant} if ids else None



# THE LISTS A VEHICLE CARD IS DRAWN FROM, per tool. Mirrors _card_anchor above
# and extractSearchCards in the frontend's cards.ts — the three of them must
# agree about where the cars live, or a filtered payload and its anchor drift.
_VEHICLE_CARD_LISTS = {
    "search_vehicles": ("matches", "nearest_alternatives"),
    "get_vehicle_details": ("candidates",),
}


def _without_drawn(tool: str, output: object, drawn: set[int]) -> dict | None:
    """`output` with cars already shown this turn removed, or None if none are left.

    FOUND LIVE, 2026-08-31. One Sales brief fired three searches — "7 seater",
    "family SUV", "5 seats" — and each result drew its own row: [7,8,1], [8,6,7],
    [1,3,2]. Nine cards for one question, with three cars shown twice, under a
    reply that described three. The customer was being shown the specialist's
    SEARCHING rather than its answer.

    The row-level guard could not help: it suppresses a row only when EVERY car
    in it was already drawn, and the second row introduced one new car, so it
    rendered whole and repeated two.

    FILTERED ON BOTH SIDES, WHICH IS THE WHOLE POINT. runner.py warned that
    "filtering cars out of a tool payload would redraw a different row on reload
    than the turn showed live" — true when only one side is filtered. The
    caller emits THIS payload and anchors the row re-derived from it, so screen
    and reload are built from the same list and cannot disagree.

    NOTHING IS INVENTED. Entries are dropped, never added or reordered, so what
    renders is still what the tool returned — a subset of it.

    A single-vehicle get_vehicle_details is left alone: it has one car and
    nothing to filter, and the row-level guard already suppresses a repeat.
    """
    if not isinstance(output, dict) or not drawn:
        return output if isinstance(output, dict) else None
    keys = _VEHICLE_CARD_LISTS.get(tool)
    if not keys:
        return output
    out = dict(output)
    changed = False
    for key in keys:
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        kept = [
            v for v in rows
            if not (isinstance(v, dict) and v.get("vehicle_id") in drawn)
        ]
        if len(kept) != len(rows):
            changed = True
            out[key] = kept
    return out if changed else output


@dataclass
class TurnResult:
    """What a finished turn amounts to, for callers that need more than events.

    The SSE route ignores it (its client already saw every event). The call
    gateway reads `reply`/`lang`/`turn_id` to synthesise speech for the reply it
    just streamed as text, and `closed` to end the call after the sign-off.
    """

    status: str  # "done" | "aborted" | "error" | "refused" (human takeover)
    reply: str = ""
    lang: str | None = None
    turn_id: int | None = None
    closed: bool = False


def _journey_agent(routing, previous_agent: str | None) -> str | None:
    """The agent whose prompt and tools this turn should use.

    "fallback" IS NOT AN AGENT. It is the router saying it could not tell, and
    there is no fallback prompt or fallback tool set to hand it — asking for one
    raises. What a clarify-or-escalate turn actually needs is the widest
    capability we have, so it degrades to UNROUTED: the combined prompt and the
    whole registry, which is exactly the single-agent system that handled every
    one of these turns before the split. Escalation (rule 13) lives in the shared
    base and works either way.

    Staying with `previous_agent` when there is one is the same instinct as the
    sticky gate: a customer mid-booking who says something unclassifiable is
    still mid-booking.
    """
    from app.agent.router.policy import FALLBACK

    agent = getattr(routing, "agent", None) if routing else None
    if agent and agent != FALLBACK:
        return agent
    return previous_agent


def _attribute_session(db, graph, config, session_id: str) -> None:
    """Stamp this session's identified customer onto its TRANSCRIPT (T4.6).

    THE EXACT COUNTERPART OF _attach_voice_customer ABOVE, deliberately written
    to the same shape and called from the same place, because it solves the same
    problem for a different table: identification happens mid-conversation, and
    the turns before it are as much the customer's words as the ones after.

    THE GAP THIS CLOSES was named in services/erasure.py's own docstring — "a
    conversation where the customer was identified but no audited tool call ever
    ran is therefore unreachable, and its transcript would survive". It could
    not be fixed there without a schema change; ConversationTurn.customer_id is
    that change.

    Idempotent and cheap: only ever touches rows where customer_id is null.

    Never allowed to break a turn, for the same reason as its sibling — a
    customer heard a real answer, and failing to backfill a pointer is not a
    reason to lose it.
    """
    try:
        snapshot = graph.get_state(config)
        state = snapshot.values if snapshot else {}
        customer_id = state.get("customer_id") if isinstance(state, dict) else None
        if not customer_id:
            return
        from sqlalchemy import update

        from app.models import ConversationTurn as _CT

        db.execute(
            update(_CT)
            .where(_CT.session_id == session_id, _CT.customer_id.is_(None))
            .values(customer_id=customer_id)
        )
    except Exception:  # noqa: BLE001 — a backfill must never cost a reply
        log.warning("could not attribute transcript for %s", session_id, exc_info=True)


def run_turn(
    session_id: str,
    message: str,
    *,
    on_event: Callable[[str, dict], None],
    cancel: threading.Event,
    slot_id: int | None = None,
    audio_id: str | None = None,
    voice_failed: bool = False,
    kickoff: bool = False,
    voice_call: bool = False,
    device_token: str | None = None,
    generate: Callable | None = None,
    session_factory: Callable | None = None,
) -> TurnResult:
    """Run one agent turn synchronously, emitting events as they happen.

    Blocking by design — the graph is sync (raw Anthropic SDK per AD-A, sync
    SQLAlchemy services), so callers run this on a worker thread and bridge
    `on_event` to their transport. `on_event` is called from that thread.

    `generate` and `session_factory` exist for the offline tests only: a
    ScriptedModel through build_config's injection seam, and a test-owned
    session factory. Production callers pass neither.
    """
    # Resolved at CALL time, not in the signature: a def-time default would
    # freeze the real SessionLocal into the function object and put it beyond
    # the reach of the tests' monkeypatch.
    db = (session_factory or SessionLocal)()
    # One id for this turn, stamped before anything can fail, so a trace row and
    # a LangSmith run can be lined up against each other later.
    trace_id = uuid.uuid4().hex
    turn_started = time.perf_counter()
    assistant_chunks: list[str] = []
    # Card anchors for THIS turn, in the order they were rendered (rule 18).
    # Accumulated beside the text and written onto the same agent turn below,
    # so the reload redraws them in the same places.
    card_rows: list[dict] = []
    # Anchors already drawn this turn — see the tool_event branch below.
    drawn_anchors: set[str] = set()
    # Vehicles already carded this turn, across tools and variants.
    drawn_vehicle_ids: set[int] = set()
    # Cascade routing for turn_trace.steps — see the "cascade" branch below.
    cascade_steps: list[dict] = []
    turn_closed = False
    user_lang = lang_service.detect(message)
    # What the MODEL reads. Identical to the transcript except on a failed
    # voice note, where the transcript is honestly empty and the model gets
    # a note ABOUT that rather than invented words (see _VOICE_FAILED_NOTE).
    graph_message = _VOICE_FAILED_NOTE if voice_failed else message
    try:
        # HUMAN TAKEOVER GATE (2026-08-31). Once an admin has taken this session
        # over, the AI is out of it for good — on EVERY channel, which is why
        # the gate lives here in the shared turn and not in the chat route. The
        # customer client switches its sends to the takeover WebSocket, so this
        # is the belt behind that: a straggler POST (an old tab, the voice
        # gateway) must not run the graph, write a turn, or burn a token.
        # Nothing is persisted; the WS path owns takeover messages.
        if takeover_service.state(db, session_id=session_id)["taken_over"]:
            on_event("taken_over", {})
            return TurnResult(status="refused")

        # Transcript: the user turn, in native script (distinct from checkpoint).
        # SKIPPED on a handoff kickoff — the "message" is a server-authored
        # directive, not the customer's words, so there is no customer bubble
        # to record; only Aisha's greeting (the agent turn below) is stored.
        if not kickoff:
            db.add(ConversationTurn(
                session_id=session_id, speaker=Speaker.customer,
                lang=Language(user_lang), text=message,
                # Null for a typed turn; the recording's id for a spoken one.
                audio_id=audio_id,
                # customer_id is DELIBERATELY NOT SET HERE. This row is written
                # before the graph runs, so nobody has been identified yet this
                # turn — and a conversation usually opens anonymous anyway.
                # _attribute_session below back-fills the whole session the
                # moment there is somebody to attribute it to (T4.6).
            ))
            db.commit()

        # Rule 3: this turn's language, computed ONCE and carried two ways.
        # As state (`turn_lang`) it drives the system-prompt anchor — the
        # instruction. As config (`expected_lang`) it arms the script guard in
        # _default_generate — the enforcement: the reply's opening is held
        # until its script is known, and a mismatch is discarded unseen and
        # retried. `classify`, not `detect`: only a wholly-one-script
        # message carries an expectation; "mixed" keeps the rule 3 judgement
        # call and arms nothing. None — no anchor, no guard — when `message`
        # is not the customer's words: a kickoff directive is server-authored
        # English that may instruct an Arabic greeting, and a failed voice
        # note is empty, so neither is evidence of the customer's language.
        turn_lang = (
            None
            if (kickoff or voice_failed)
            else lang_service.classify(message)
        )

        # Per-turn graph + WAL checkpointer connection (thread_id = session_id
        # resumes prior turns). iterations reset to 0; customer_id (absent from
        # input) persists from the checkpoint, so a returning turn stays identified.
        graph = build_graph(new_sqlite_checkpointer(AGENT_CHECKPOINT_PATH))

        # ROUTING (S7). Which journey agent owns this turn.
        #
        # READ THE PREVIOUS AGENT FIRST — the sticky rule is worthless without
        # it. A bare "yes" carries no intent of its own; it belongs to whoever
        # asked the question last turn, and that fact lives in the checkpoint.
        #
        # SKIPPED ON A NON-CUSTOMER MESSAGE, for the same reason turn_lang is: a
        # kickoff directive is server-authored English and a failed voice note is
        # empty. Neither is evidence of intent, and classifying them would move
        # the active agent on a turn the customer never spoke. Skipping keeps
        # whatever the checkpoint already held.
        previous_agent = None
        prior_facts: dict = {}
        routing = None
        router_ms = None
        if _config.ROUTER_ENABLED:
            try:
                snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
                previous_agent = (snapshot.values or {}).get("agent")
                prior_facts = (snapshot.values or {}).get("session_facts") or {}
            except Exception:
                log.warning("could not read prior agent from checkpoint", exc_info=True)

            if not (kickoff or voice_failed):
                _routed_at = time.perf_counter()
                routing = _route(
                    message,
                    current_agent=previous_agent,
                    session_facts=prior_facts,
                    # Bookings MADE IN THIS CONVERSATION — already in
                    # session_facts. Deliberately NOT a fresh
                    # booking.for_customer read: that is a DB round-trip every
                    # turn to sharpen a hint the active agent already carries,
                    # and the router sits on the voice hot path. If turn_trace
                    # shows follow-ups misrouting for want of it, add the read
                    # then — measured, not assumed.
                    live_bookings=list(prior_facts.get("booked_refs") or []),
                )
                router_ms = int((time.perf_counter() - _routed_at) * 1000)
        config = build_config(
            db, session_id, cancel_event=cancel,
            provided_slot_id=slot_id, expected_lang=turn_lang,
            voice_call=voice_call,
            device_token=device_token,
            generate=generate,
        )
        for chunk in graph.stream(
            {
                "messages": [{"role": "user", "content": graph_message}],
                "iterations": 0,
                # Per-turn control, reset like `iterations` (rule 19): this is
                # "did a close land on THIS turn", not "is this session over".
                "closed": False,
                # Per-turn like `closed` (C-options, 2026-08-14): "did chips go
                # up on THIS turn", never a sticky property of the session.
                "options_offered": False,
                # Per-turn like the two above (2026-08-31): how many times this
                # turn has already gone back to the specialists. Reset here,
                # enforced in _orchestrator_node against MAX_CASCADES_PER_TURN.
                "cascades": 0,
                # Rule 3 (see the comment above build_config): drives the
                # system-prompt anchor. Per-turn, reset like `iterations`.
                "turn_lang": turn_lang,
                # C10's recognition guard. NOT per-turn control like the two
                # above: the reducer is operator.add, so this 1 ACCUMULATES into
                # a running count of customer turns. That is the whole mechanism
                # — it advances only when a human speaks, never on a tool loop,
                # which is what lets _may_confirm_recognition tell "they
                # answered" apart from "the model asked and answered itself".
                "customer_turns": 1,
                # S7: selects BOTH the prompt body and the tool allowlist, from
                # one value, so the two can never disagree. None when routing is
                # off or the turn is not the customer's words — the graph then
                # falls through to the combined prompt and the whole registry,
                # exactly as before the split.
                "agent": _journey_agent(routing, previous_agent),
            },
            config,
            stream_mode="custom",
        ):
            kind = chunk.get("type")
            if kind == "token":
                assistant_chunks.append(chunk["text"])
                on_event("token", {"text": chunk["text"]})
            elif kind == "tool_event":
                anchor = _card_anchor(chunk["tool"], chunk["output"])
                # THE SAME CARD IS NEVER DRAWN TWICE IN ONE TURN (found live,
                # 2026-08-29: a sales sub-agent called search_vehicles twice on
                # one brief — two different queries, the same two cars back —
                # and the customer got the X70 FL Plus and X70 FL rendered
                # twice, one pair directly under the other).
                #
                # KEYED ON THE ANCHOR, NOT THE TOOL OR ITS INPUT, because the
                # anchor IS what gets drawn: two different searches that land on
                # the same vehicles produce one card, and one search that finds
                # different vehicles produces two. Suppressing the event as well
                # as the row is deliberate — the row governs what a RELOAD
                # rebuilds, the event governs what is on screen now, and letting
                # those two disagree is what makes a duplicate card survive a
                # refresh in only one of them.
                #
                # Non-card tools (`anchor is None`) are untouched: they drive
                # slot chips, option chips and the working label, where a repeat
                # is meaningful.
                if anchor is not None:
                    key = _json.dumps(anchor, sort_keys=True)
                    if key in drawn_anchors:
                        log.info(
                            "suppressing duplicate %s card — session=%s",
                            chunk["tool"], session_id,
                        )
                        continue
                    # THE SAME VEHICLE IS NEVER DRAWN TWICE IN ONE TURN either
                    # (found live, 2026-08-29, second shape): a sales sub-agent
                    # searched — three compact cards — then called
                    # get_vehicle_details on its candidates, and each detail
                    # event re-drew a car the customer was already looking at,
                    # one row under another. Different anchors (variant and
                    # grouping differ), same cars — so the exact-key check
                    # above never fires. Suppressed only when EVERY vehicle in
                    # the row has already been drawn this turn: a row that
                    # introduces even one new car still renders whole, because
                    # filtering cars out of a tool payload would redraw a
                    # different row on reload than the turn showed live.
                    ids = anchor.get("vehicle_ids")
                    if ids and set(ids) <= drawn_vehicle_ids:
                        log.info(
                            "suppressing re-drawn vehicle card(s) %s from %s — session=%s",
                            ids, chunk["tool"], session_id,
                        )
                        continue
                    # A ROW THAT REPEATS SOME OF WHAT IS ALREADY ON SCREEN is
                    # trimmed to the cars it actually adds (see _without_drawn).
                    # The payload and the anchor are BOTH re-derived from the
                    # filtered output, so a reload rebuilds exactly what the
                    # customer saw.
                    if ids and (set(ids) & drawn_vehicle_ids):
                        trimmed = _without_drawn(
                            chunk["tool"], chunk["output"], drawn_vehicle_ids
                        )
                        re_anchored = _card_anchor(chunk["tool"], trimmed)
                        if re_anchored is None:
                            log.info(
                                "suppressing %s row — nothing left after trimming "
                                "%s — session=%s", chunk["tool"], ids, session_id,
                            )
                            continue
                        log.info(
                            "trimming %s row %s to %s — session=%s",
                            chunk["tool"], ids, re_anchored.get("vehicle_ids"),
                            session_id,
                        )
                        chunk = {**chunk, "output": trimmed}
                        anchor, ids = re_anchored, re_anchored.get("vehicle_ids")
                        key = _json.dumps(anchor, sort_keys=True)
                        if key in drawn_anchors:
                            continue
                    drawn_vehicle_ids.update(ids or [])
                    drawn_anchors.add(key)
                    card_rows.append(anchor)
                event = {
                    "tool": chunk["tool"], "input": chunk["input"], "output": chunk["output"],
                }
                # WHICH SPECIALIST DREW THIS CARD. Present only on the
                # multi-agent path — turn.forward_events stamps it so a card
                # cannot be attributed to Aisha — and omitted entirely
                # otherwise, so the single-agent payload stays byte-identical.
                if chunk.get("agent"):
                    event["agent"] = chunk["agent"]
                on_event("tool_event", event)
            elif kind == "cascade":
                # Which specialist took which brief, how it was chosen, how long
                # it took and whether it worked. Appended rather than assigned:
                # a turn can run more than one cascade, and the routing view
                # should show every hop, not the last one.
                cascade_steps.extend(chunk.get("steps") or [])
            elif kind == "booking":
                on_event("booking", {"booking_ref": chunk["booking_ref"]})
            elif kind == "text_break":
                # Rule 17: a bubble boundary, NOT a deletion. This used to
                # clear the accumulator, which is how a booking recap and a
                # farewell each ended up persisted as an empty agent turn —
                # blank on reload — after being erased on screen.
                #
                # A blank line is the whole fix for the run-on this guards
                # against ("…under that name.There are two T2 models…"): the
                # transcript keeps both halves, separated. The UI seals the
                # bubble on the same event, so screen and transcript still
                # agree — one thread, two paragraphs, nothing lost either way.
                if assistant_chunks:
                    assistant_chunks.append("\n\n")
                on_event("text_break", {})
            elif kind == "closed":
                turn_closed = True
                on_event("closed", {})

        # AD-E: an abort can land in the token loop (StreamAborted, caught
        # below) OR gracefully in the tool node (graph ends normally with
        # aborted tool_results). Unify both: if the client is gone, do NOT
        # persist a partial assistant transcript or push `done` to nobody.
        if cancel.is_set():
            log.info("chat aborted by client (graph stopped) — session=%s", session_id)
            db.rollback()
            return TurnResult(status="aborted", closed=turn_closed)

        # Graph finished. Transcript: the assistant turn (accumulated stream).
        reply = "".join(assistant_chunks)
        reply_lang = lang_service.detect(reply) if reply else user_lang
        # `cards` rides along with the text (rule 18): the same turn that said
        # "here's the T2" also records that it showed the T2's card, so a
        # reload rebuilds the thread exactly as the customer left it.
        agent_turn = ConversationTurn(
            session_id=session_id, speaker=Speaker.agent,
            lang=Language(reply_lang), text=reply,
            cards=card_rows or None,
        )
        db.add(agent_turn)
        # If this turn is where the session became identified, link the
        # recordings made before it to that customer, so "forget me" can
        # find them. No-op on every turn after the first that identifies.
        _attach_voice_customer(db, graph, config, session_id)
        # THE SAME MOVE FOR TRANSCRIPTS (T4.6), and it belongs right here for
        # the same reason: a conversation opens anonymous and becomes
        # identified partway through, so the turns BEFORE that moment are the
        # customer's words too. Back-filling them is what lets "forget me"
        # reach a transcript from a session no audited tool call ever touched —
        # the gap services/erasure.py named and could not close without a
        # schema change.
        _attribute_session(db, graph, config, session_id)
        db.commit()
        # `turn_id` is what read-aloud points at (2026-07-21). Read AFTER the
        # commit, because that is when the row has an id — and sent on `done`
        # rather than earlier because a turn only becomes readable once it is
        # finished and persisted. The client never sends Aisha's words back
        # to be spoken; it names the turn and the server looks them up.
        # TELEMETRY LAST, and never in the way. This is written after the
        # customer already has their answer and after the transcript is
        # committed, and services/turn_trace swallows its own errors — a full
        # disk or a locked table must not cost anyone a booking.
        _turn_trace.record(
            db,
            trace_id=trace_id,
            session_id=session_id,
            routing=routing,
            previous_agent=previous_agent,
            channel="call" if voice_call else "chat",
            user_message=None if kickoff else message,
            detected_lang=user_lang,
            customer_id=(prior_facts or {}).get("customer_id"),
            tool_calls=[row.get("tool") for row in card_rows if row.get("tool")],
            # THE CASCADE, which `routed_to` cannot show. That column carries
            # the LEGACY journey router (prefilter/classifier/sticky) and says
            # "testdrive" for a turn sales handled end to end — true of every
            # row written before today. These are the specialists that actually
            # ran. None when the turn never delegated, so single-agent rows are
            # unchanged.
            steps=cascade_steps or None,
            agent_response=reply,
            router_latency_ms=router_ms,
            latency_ms=int((time.perf_counter() - turn_started) * 1000),
        )
        on_event("done", {"lang": reply_lang, "turn_id": agent_turn.id})
        return TurnResult(
            status="done", reply=reply, lang=reply_lang,
            turn_id=agent_turn.id, closed=turn_closed,
        )

    except StreamAborted:
        # AD-E: the client dropped and the graph stopped cleanly. Nothing to
        # emit (they are gone); the transcript for this partial turn is skipped.
        log.info("chat aborted by client — session=%s (graph stopped, no run-on)", session_id)
        db.rollback()
        return TurnResult(status="aborted", closed=turn_closed)
    except Exception:
        log.exception("chat turn failed — session=%s", session_id)
        db.rollback()
        on_event("error", {"message": "Something went wrong on our side. Please try again."})
        return TurnResult(status="error", closed=turn_closed)
    finally:
        db.close()
