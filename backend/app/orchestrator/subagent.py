"""Running one sub-agent on Haiku (T5.2, bound at Phase 9).

WHAT THIS IS. `turn.execute` has always taken an injected `runner_fn` so the
whole cascade could be tested offline with a stub. This is the real one: it runs
ONE brief through Haiku with that agent's prompt and its READ-ONLY tools, and
returns structured findings.

IT REUSES `tool_node._dispatch` RATHER THAN CALLING TOOLS DIRECTLY, and that is
the load-bearing choice in this file. `_dispatch` injects `db`, `session_id` and
`customer_id` from trusted context, refuses a write from an unidentified session,
and turns every failure into an error envelope instead of an exception. A
sub-agent reaching TOOL_FNS itself would bypass all of that — and the one thing a
sub-agent must never be able to do is name its own customer.

READ-ONLY IS ENFORCED TWICE. The card declares reads only (asserted by
evals/differential.py), and this refuses to bind a write even if a card ever
gained one. Belt and braces, because the whole reason a sub-agent may run on a
cheaper model is that a bad generation cannot write.

IT RETURNS DATA, NEVER PROSE. The sub-agent prompts carry no persona, no Arabic
register and no output rules — those live in base/ and aisha/ alone. What comes
back is the tool results, which Aisha composes into the sentence a customer
reads. A sub-agent that started writing customer-facing prose would be a second
voice with none of the guardrails.

EVERY TOOL EVENT IS FORWARDED to the parent stream. Miss that and the cards
silently vanish from the UI with no error anywhere — see turn.forward_events.
"""

from __future__ import annotations

import json
import re
import logging
import threading
import time
import uuid
from datetime import date

log = logging.getLogger("app.orchestrator.subagent")

# What a sub-agent is told to produce. Short and blunt: it is appended to a
# prompt that already carries the agent's own rules, and its only job is to stop
# a model that has just read pages of conversational guidance from answering
# conversationally.
FINDINGS_INSTRUCTION = """\
YOU ARE NOT TALKING TO THE CUSTOMER. Another assistant is. Your job is to gather \
facts for her using your tools and then stop.

Call the tools you need. When you have what the request asked for, reply with a \
single line of JSON and nothing else, in this shape:

{"findings": {...}, "note": "one short sentence for the assistant, not the customer"}

Put the ids the next step will need in `findings` (vehicle_id, variant_id, \
slot_id and so on) alongside the facts. If you could not find something, say so \
in `note` and leave it out of `findings` rather than guessing it.

KEEP `findings` SHORT. Everything your tools returned is passed on in full \
already — you do not need to copy prices, specifications, tables or lists out \
of a tool result, and re-typing them wastes the customer's time and risks \
changing a number. Put the ids, anything you WORKED OUT that is not already in \
the results, and nothing else.

Do not greet, do not apologise, do not write to the customer, and do not format \
anything. One JSON object.
"""

# Appended ONLY for an agent whose card declares `proposes`. A sub-agent that
# cannot propose anything must not be told the vocabulary exists — an agent
# offered a booking verb it may not use will eventually reach for it, and every
# such attempt is a refusal the customer waits through.
#
# PROPOSING IS NOT DOING, and the wording has to carry that or the model reports
# success. It asks; orchestrator/write.py decides; the service guards beneath
# still refuse independently. A sub-agent that said "I've booked it" in its note
# would put a lie in front of Aisha with nothing to catch it.
PROPOSALS_INSTRUCTION = """\

ASKING FOR SOMETHING TO BE DONE. You cannot change anything yourself — you have \
no tools that write. What you CAN do is ASK, by adding a `proposals` list beside \
your findings:

{"findings": {...}, "proposals": [{"action": "hold_slot", "args": {"slot_id": 12}}], "note": "..."}

The actions you may ask for are: %s

The args each action takes — use these exact names: \
hold_slot {"slot_id"}; \
create_booking {"vehicle_id", "slot_id"}; \
reschedule_booking {"ref" or "mobile", "new_slot_id" — the slot to MOVE TO, \
already held}; \
cancel_booking {"ref" or "mobile"}.

An entry is a REQUEST, not a result. It may be refused — because the step before \
it has not happened, because the slot went, because a rule says not yet. So never \
write in your note that something HAS been done, booked, held or cancelled. Say \
what you asked for. Somebody else decides, and the customer is told by somebody \
else.

Only propose what the request actually asked for, and leave `proposals` out \
entirely when it asked for nothing.

Never judge a phone number, a country code or an id yourself — whether a value \
is acceptable is decided by the write that receives it, not by you. Propose \
with the values the request gives you.

IF THE BRIEF ASKS FOR AN ACTION AND YOU ARE NOT PROPOSING IT, SAY WHY in your \
note — "the customer has not chosen a slot yet", "the slot named in the brief \
is no longer free". An action you describe without proposing does not happen: \
prose about booking is not a booking, and a step that was asked to act and \
neither proposes nor explains is an incomplete answer.
"""

# Appended to the brief ONLY when this session holds no slot and the agent can
# propose one (`hold_slot` on its card). See the dispatch site in run_step for
# the live incident this forbids; the summary is: an accepted-but-unlocked slot
# has no held_by_you protection, so a re-query can omit it while it is still
# open, and "absent from a 3-slot answer" is not "taken".
_NO_HOLD_BRIEF = (
    "No slot is held by this conversation yet. If the customer has already "
    "accepted a specific offered time, propose hold_slot for that exact "
    "slot_id first instead of re-checking it with get_available_slots: that "
    "query returns at most 3 slots, so an open slot can be absent from its "
    "answer, and absence does NOT mean taken — never report a slot the "
    "customer accepted as unavailable because a re-query did not list it. "
    "When you do query availability, carry the customer's stated date and "
    "daypart."
)


_pooled_client = None
_pooled_lock = threading.Lock()


def _client():
    """One pooled client for the process, built on first use.

    THIS USED TO BUILD A FRESH CLIENT PER STEP. A new anthropic.Anthropic() is
    a new TLS handshake to the API, which the router classifier measured at
    ~1.5s p50 against ~250ms on a reused connection and pooled for that reason
    (router/classifier._get_client). Aisha's own client is pooled the same way
    (graph._default_generate). The specialists were the one caller still paying
    the handshake — on every step of every cascade. The SDK client is
    thread-safe, so the parallel waves share it; the lock only keeps two
    first-callers from building it twice.
    """
    global _pooled_client
    if _pooled_client is None:
        with _pooled_lock:
            if _pooled_client is None:
                from app.agent.graph import _build_client

                _pooled_client = _build_client()
    return _pooled_client


# TOOL DESCRIPTIONS REWRITTEN FOR THE SPECIALIST SURFACE (2026-09-04). The
# schemas are shared with Aisha, and get_booking's description opens with
# "FIRST identify the customer with identify_by_reference_or_mobile" — a tool
# no specialist holds. Seen live: a testdrive step obeyed it, was refused as
# unbound, and spent the round. On this path identity is Aisha's job and is
# done before any brief is written; the specialist's version says so. Only the
# description changes — name and input_schema are byte-identical — so what
# the model can CALL is untouched.
_SUBAGENT_DESCRIPTIONS: dict[str, str] = {
    "get_booking": (
        "Look up an existing test-drive booking so it can be described, moved "
        "or cancelled. Read-only — it changes nothing.\n\n"
        "The customer has ALREADY been identified by the assistant who briefed "
        "you; you hold no identification tool and must not try. Pass EITHER "
        "the mobile OR the TD reference (TD-YYYY-XXXX) exactly as the brief "
        "gives it — the tool normalises spacing, dashes and case.\n\n"
        "Read the response:\n"
        "- 'bookings' is the list of THEIR ACTIVE bookings (a confirmed or "
        "rescheduled test drive). Each has the vehicle, the slot (with a "
        "human-readable day, date and time in 'start_human'), the location "
        "(showroom name, or the doorstep service area), the status, and the "
        "booking_ref.\n"
        "- An EMPTY list means they have no active booking under those details. "
        "Report that in your note — do NOT invent one.\n"
        "- More than one booking means they have several live; put them all "
        "in your findings.\n"
        "- {'error': 'identity_required'} means the session is not identified "
        "yet. Say so in your note and stop — the assistant will identify them "
        "and brief you again. Do not retry.\n"
        "- {'error': 'invalid_uae_mobile'}: the number could not be read; say "
        "so in your note.\n"
        "Only ever describe a booking from what this tool returned."
    ),
}


def _schemas_for(agent: str) -> list[dict]:
    """The agent's card tools, filtered to reads. Never returns a write."""
    from app.agents import loader
    from app.tools import SUBAGENT_REGISTRY, WRITE_TOOLS

    declared = list(loader.frontmatter(agent).get("tools") or [])
    out = []

    for name in declared:
        if name in WRITE_TOOLS:
            # Should be impossible — differential.py fails the build first — but
            # this is the last gate before a cheap model gets a write.
            log.error("refusing to bind write tool %r to sub-agent %s", name, agent)
            continue
        entry = SUBAGENT_REGISTRY.get(name)
        if entry is None:
            # A CARD NAMING A TOOL THAT DOES NOT EXIST IS A BUILD ERROR, not a
            # thing to skip. This used to `continue` silently, and it cost the
            # finance agent every one of its three tools: it ran with an empty
            # `tools` list, could call nothing, and reported "blocked" as though
            # the dependency ordering had stopped it. Nothing failed, nothing
            # logged, and the smoke test read as a pass.
            # tests/test_subagent_tools.py fails the build on this now; the
            # error here is the runtime half of the same guard.
            log.error(
                "card for %s declares unknown tool %r — the agent will run "
                "without it", agent, name,
            )
            continue
        schema = entry["schema"]
        if name in _SUBAGENT_DESCRIPTIONS:
            schema = {**schema, "description": _SUBAGENT_DESCRIPTIONS[name]}
        out.append(schema)
    return out



def _proposes(agent: str) -> list[str]:
    """What this agent's card says it may ASK for. Empty for most agents."""
    from app.agents import loader
    from app.orchestrator.write import PROPOSABLE

    declared = list(loader.frontmatter(agent).get("proposes") or [])
    out = []
    for name in declared:
        if name not in PROPOSABLE:
            # A card asking for something write.py cannot perform. Reported
            # rather than skipped: differential.py fails the build on it, and
            # this is the runtime half of the same guard.
            log.error("card for %s proposes unknown action %r", agent, name)
            continue
        out.append(name)
    return out


def _proposals_clause(agent: str) -> str:
    actions = _proposes(agent)
    if not actions:
        return ""
    return PROPOSALS_INSTRUCTION % ", ".join(actions)


# The write tools take `ref`, but the card prose and the model both reach for
# `booking_ref` — the name every tool RESULT uses. Read, don't insist: an alias
# is not a different request. Ints arrive as strings for the same reason (the
# model copies them out of prose), and write.execute passes args straight to the
# tool function, so they are coerced here or not at all.
# Self-contained document reads a specialist never chains a further tool call
# from: the result IS the answer, so a step whose every call is one of these
# settles straight after its tool round instead of paying a ~2,187ms Haiku
# summary round (see the settle gate in run_step). compare_vehicles and
# get_finance_options are deliberately NOT here — see the comment at the gate.
TERMINAL_READS: frozenset[str] = frozenset({
    "search_knowledge",
    "get_colours",
    "get_accessories",
})

_PROPOSAL_ALIASES = {"booking_ref": "ref", "reference": "ref"}
_PROPOSAL_INTS = ("slot_id", "new_slot_id", "vehicle_id", "variant_id")


# WHAT MAKES TWO ASKS THE SAME ASK — the SUBJECT of the write, not every
# argument spelling. Found live 2026-08-31: a specialist both CALLED
# create_booking (intercepted below, carrying the tool's full arguments) and
# WROTE a proposal for the same booking (carrying fewer). Keyed on the whole
# argument dict those are two different keys, so both survived, both
# "executed", and the customer got two identical booking cards under one
# reply. The DATA was never wrong — booking.create's guard 2 is idempotent and
# returns the existing booking for the slot — but two `executed` actions draw
# two cards, and a customer cannot tell a redraw from a double booking.
#
# THE SUBJECT IS WHAT THE WRITE ACTS ON. A slot holds at most one active
# booking, which is the very thing that idempotency guard relies on, so two
# create_bookings naming one slot cannot be two different intentions. A move to
# a DIFFERENT slot has a different subject and is correctly kept as its own ask.
_PROPOSAL_IDENTITY: dict[str, tuple[str, ...]] = {
    "lock_slot": ("slot_id",),
    "create_booking": ("slot_id",),
    "reschedule_booking": ("ref", "new_slot_id"),
    "cancel_booking": ("ref",),
}


def _proposal_key(item: dict) -> str:
    """The dedupe key for one proposal: its action and what it acts on.

    FALLS BACK TO THE WHOLE ARGUMENT DICT when the subject is not fully known.
    A proposal missing its slot_id would otherwise collapse to a bare action
    name and swallow a genuinely different ask — and merging two requests is a
    worse failure than performing a duplicate one, which the service guards
    already make safe.
    """
    from app.orchestrator.write import _ACTION_TO_TOOL

    action = item.get("action")
    args = item.get("args") or {}
    # hold_slot and lock_slot are one action under two names; write.py already
    # owns that mapping, so it is read from there rather than restated.
    canonical = _ACTION_TO_TOOL.get(action, action)
    subject = _PROPOSAL_IDENTITY.get(canonical or "")
    if subject and all(args.get(key) is not None for key in subject):
        return json.dumps(
            [canonical, [args.get(key) for key in subject]],
            sort_keys=True, default=str,
        )
    return json.dumps([canonical, args], sort_keys=True, default=str)


def _proposal_args(raw: dict) -> dict:
    """One tool_use's input, in the shape write.execute will actually call with."""
    out: dict = {}
    for key, value in (raw or {}).items():
        out[_PROPOSAL_ALIASES.get(key, key)] = value
    for key in _PROPOSAL_INTS:
        value = out.get(key)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            out[key] = int(value.strip())
    return out


# A MOVE NEEDS THE SLOT HELD FIRST, and the card says so ("lock the slot with
# lock_slot, then call reschedule_booking"). Measured live 2026-08-31: the
# specialist asked for the reschedule alone, the service guard refused it —
# "the new slot is not held by this conversation" — Aisha apologised, tried
# again, and the second attempt asked for both. The customer waited 35 seconds
# for a move that could have gone in one pass.
#
# COMPLETING THE ASK, NOT INVENTING ONE. The slot is the one the customer
# picked and the specialist named; the hold is the step the card already
# describes and the card already permits (`proposes: [hold_slot, ...]`). This
# reads the two-step action out of a one-step request the same way
# _harvest_entities reads ids out of tool results — deterministically, once,
# rather than by asking the model again.
#
# ORDER MATTERS AND write.execute_all HONOURS IT: proposals run in sequence and
# stop at the first refusal, so a hold that fails cannot leave a booking moved
# to a slot nobody owns.
_NEEDS_HELD_SLOT = {"reschedule_booking": "new_slot_id", "create_booking": "slot_id"}


def _finalise_proposals(db, session_id: str, agent: str, proposed: list[dict]) -> list[dict]:
    """Dedupe what was asked, prepend the hold a move needs, drop what the card
    may not ask for. ONE function for both settle paths (2026-09-04), so a step
    that settles on its ask and a step that settles on its summary cannot
    disagree about what a proposal has to pass."""
    deduped: list[dict] = []
    seen_keys: set[str] = set()
    for item in proposed:
        # FIRST WINS, AND THE INTERCEPTS ARE FIRST BY CONSTRUCTION. An
        # intercepted tool_use carries the tool's own argument names, in full,
        # as the model actually tried to call it; the JSON proposal is the same
        # ask re-expressed and usually thinner.
        key = _proposal_key(item)
        if key in seen_keys:
            log.info("%s asked for %s twice; keeping the first", agent, item.get("action"))
            continue
        seen_keys.add(key)
        deduped.append(item)
    if not deduped:
        return []
    deduped = _with_required_hold(db, session_id, agent, deduped)
    return _validate_proposals(agent, deduped)


# THE ASKS THAT END A STEP. A specialist that has asked for the booking, the
# move or the cancellation has nothing left to look up — the intercept's own
# tool_result tells it "report your findings now". hold_slot is deliberately
# NOT here: the card's own prose says "lock the slot, then call
# reschedule_booking", so a lone hold may be the first half of a two-part ask
# and the step must keep its next round to make the second.
_TERMINAL_ASKS: frozenset[str] = frozenset({
    "create_booking", "reschedule_booking", "cancel_booking",
})

# A tool_use spelled the TOOL's way for an action the card spells its own way.
# write.py maps hold_slot -> lock_slot for execution; this is the read direction,
# and it is one entry because only one action has two names. Found in the
# testdrive prose, which says "lock the slot with lock_slot" while the card
# proposes `hold_slot` — so a lock_slot tool_use missed the intercept and was
# refused as an unbound tool, costing the round and the hold.
_TOOL_SPELLINGS: dict[str, str] = {"lock_slot": "hold_slot"}


def _with_required_hold(db, session_id: str, agent: str, proposals: list[dict]) -> list[dict]:

    """Prepend the hold a move or a booking needs, when the slot is not held yet."""
    if not proposals or "hold_slot" not in set(_proposes(agent)) or db is None:
        return proposals
    try:
        from app.services import slots as _slots

        held = _slots.held_by_session(db, session_id=session_id) or {}
        held_id = held.get("slot_id")
    except Exception:  # noqa: BLE001 — never let this cost the step its proposal
        log.warning("could not read the held slot; leaving proposals as asked", exc_info=True)
        return proposals

    out: list[dict] = []
    for item in proposals:
        key = _NEEDS_HELD_SLOT.get(item.get("action") or "")
        wanted = (item.get("args") or {}).get(key) if key else None
        if wanted is not None and wanted != held_id:
            already = any(
                p.get("action") == "hold_slot"
                and (p.get("args") or {}).get("slot_id") == wanted
                for p in out
            )
            if not already:
                log.info(
                    "%s asked to %s slot %s without holding it; asking for the hold first",
                    agent, item.get("action"), wanted,
                )
                out.append({"action": "hold_slot", "args": {"slot_id": wanted}})
        out.append(item)
    return out


def _derived_proposals(db, session_id: str, step, customer_id) -> list[dict]:
    """The write the brief asked for, when server state already determines it.

    WHY THIS EXISTS (measured 2026-09-04, over every sub-agent step in the test
    VM's journal): 25 of 64 replies — 39% — were discarded by _parse_findings
    because they did not fit `max_tokens`. Parsed replies run to 691 characters;
    the failures start at 563 and reach 1,126. For a READ that costs a note and
    nothing else, exactly as the ceiling's own comment promises: entities are
    re-read from the tool results and `_evidence` carries them whole. A PROPOSAL
    has no such second source — it exists only in the model's text — so the same
    truncation costs the whole booking.

    It did, twice in one turn. A customer with a held slot, an acknowledged
    licence and recorded consent watched Aisha say she was "having a little
    trouble completing the booking" and escalate to a human, while a correct
    create_booking sat inside the unterminated note nobody could parse.

    NOTHING HERE IS INFERRED. At that moment the server already held every part
    of the answer, and had done before the model was called:

      * WHICH WRITE — select.action_owners reads it off the brief
        deterministically. It had already run: the step logged layer=fast_lane,
        which is that lane.
      * WHETHER IT MAY HAPPEN — booking_state.read, the same derived state the
        write gate enforces. `create` means a slot locked BY THIS SESSION with
        licence_ack set, which is the recap-yes and the licence reminder both
        done.
      * WHICH SLOT — that held slot.
      * WHICH CAR — the slot's own row. booking.create refuses any other
        (vehicle_slot_mismatch), so there is nothing to choose.
      * WHICH CUSTOMER — never from here. write.py injects it.

    COMPLETING THE ASK, NOT INVENTING ONE — the same principle as
    _with_required_hold above, and the same reason: read the determined action
    out of the state deterministically, once, rather than asking the model
    again. The ask is Aisha's brief, and Aisha is the one who spoke to the
    customer.

    EVERY GUARD STILL RUNS. This returns a REQUEST on the ordinary path:
    _validate_proposals checks it against the card, booking_state.allowed gates
    it in write.execute_all, and booking.create independently re-checks the
    lock, the licence and the vehicle. Duplicates are impossible by
    construction — create flips the slot from `locked` to `booked`, and a
    second attempt fails its `locked_by_session` check.

    CREATE ONLY. reschedule_booking and cancel_booking name a booking the state
    machine does not determine (which one, moved to when), so a derived version
    would be a guess. This is a recovery for the one write whose every argument
    is already known, not a general licence to act without the model.
    """
    if db is None or not session_id:
        return []
    try:
        from app.orchestrator import booking_state as _booking_state
        from app.orchestrator.select import action_owners

        if "create_booking" not in set(_proposes(step.agent)):
            return []
        owners = action_owners(getattr(step, "brief", "") or "")
        if step.agent not in owners.get("create_booking", set()):
            return []

        state = _booking_state.read(db, session_id, customer_id=customer_id)
        if state.slot_id is None or "create_booking" not in state.allowed:
            return []

        from app.services import slots as _slots

        vehicle_id = (_slots.facts_for_slot(db, state.slot_id) or {}).get("vehicle_id")
        if vehicle_id is None:
            return []

        log.warning(
            "%s proposed nothing for a create_booking brief; deriving it from "
            "the held slot %s (vehicle %s) — session=%s",
            step.agent, state.slot_id, vehicle_id, session_id,
        )
        return [{
            "action": "create_booking",
            "args": {"vehicle_id": vehicle_id, "slot_id": state.slot_id},
        }]
    except Exception:  # noqa: BLE001 — a recovery must never cost the turn
        log.warning("could not derive the booking proposal", exc_info=True)
        return []


def _validate_proposals(agent: str, raw: list[dict]) -> list[dict]:
    """Keep only what this agent's card actually permits it to ask for.

    THE CARD IS THE ALLOWLIST, not the prompt. A model that invents an action —
    or borrows one from another agent's vocabulary — has its request dropped
    here, before write.py ever sees it. write.py refuses it again; neither gate
    is redundant, because this one names the AGENT and that one names the STATE.
    """
    allowed = set(_proposes(agent))
    out = []
    for item in raw or []:
        action = str(item.get("action") or "").strip()
        if action not in allowed:
            log.warning("dropping %r proposed by %s (not on its card)", action, agent)
            continue
        args = item.get("args")
        out.append({
            "action": action,
            "args": dict(args) if isinstance(args, dict) else {},
            "agent": agent,
        })
    return out


# WHAT A TOOL OFFERS IS NOT WHAT THE CUSTOMER CHOSE (found live, 2026-08-31).
#
# get_available_slots returns a LIST of times nobody has picked yet, and the
# walk below takes the first `slot_id` it meets anywhere. On a reschedule that
# ran get_booking then get_available_slots, run.execute's last-step-wins fold
# put slot 477 — 5 PM, the first row of the offer — into session_facts, over
# the top of the booking's real slot. The customer had chosen 6:30 PM.
#
# THE FIRST ROW IS THE RIGHT ANSWER FOR A SEARCH AND THE WRONG ONE FOR AN OFFER,
# and that is the whole distinction. search_vehicles is RANKED — its first row
# is the one the agent recommended, which is why the walk works that way and
# must keep working that way (see the docstring below). A list of free times
# carries no recommendation at all: the customer picks, and until they do
# nothing is established. So availability is excluded from `slot_id` by name,
# rather than the walk being made cleverer.
_OFFERS_NOT_FACTS: dict[str, frozenset[str]] = {
    "get_available_slots": frozenset({"slot_id"}),
}


def _harvest_entities(
    agent: str, tool_results: list[dict], tools: list[str] | None = None
) -> dict:
    """Pull the entities this agent DECLARES IT PRODUCES out of its tool results.

    WHY THIS EXISTS, FOUND BY RUNNING IT FOR REAL. The findings instruction asks
    the model to put the ids the next step needs into its JSON. Live, Haiku
    returned `{"results": ..., "search_performed": true}` for a Sales brief whose
    card declares `produces: [vehicle_id, variant_id]` — so nothing threaded
    forward, and the TestDrive step that depended on it ran with an empty
    context and found nothing.

    ASKING HARDER IS THE WRONG FIX, and this repo has learned that twice already
    (see agent/sanitise.py on rule 17). The ids are not something the model needs
    to remember: they are sitting in the tool result it just received. Reading
    them is deterministic and done once.

    FIRST OCCURRENCE WINS, and it is the right choice: a search returns a ranked
    list, and the first row is the one it recommended. Later steps get the car it
    actually led with rather than the last one it happened to mention.
    """
    from app.orchestrator.order import produces

    wanted = produces(agent)
    if not wanted:
        return {}

    found: dict = {}

    def _walk(node, harvestable: frozenset):
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in harvestable
                    and key not in found
                    and isinstance(value, (int, str))
                ):
                    found[key] = value
                _walk(value, harvestable)
        elif isinstance(node, list):
            for item in node:
                _walk(item, harvestable)

    # `tools` is parallel to `tool_results` — both are appended in the same
    # branch of the dispatch loop. Omitted (the unit tests, and any caller that
    # has only the bare results), nothing is excluded and this behaves exactly
    # as it did.
    for index, result in enumerate(tool_results):
        tool = tools[index] if tools and index < len(tools) else None
        _walk(result, wanted - _OFFERS_NOT_FACTS.get(tool, frozenset()))
        if wanted <= set(found):
            break
    return found


# The longest prose reply measured across the 2026-08-31 runs was 1,088 chars.
# Twice that, so the fallback below carries a whole answer and still cannot be
# used to push an unbounded string into the composer's prompt.
PROSE_NOTE_MAX_CHARS = 2000


def _prose_note(text: str) -> dict:
    """A reply with no usable JSON, kept as a note rather than thrown away.

    MEASURED 2026-08-31, ACROSS 47 SUB-AGENT STEPS: 14 of them (30%) returned a
    reply with no JSON object in it at all, and every byte was discarded here.
    That produced a step marked `ok` with `findings={}` — which compose_input
    puts in NEITHER `findings` (it is empty) nor `unavailable` (it succeeded).
    Aisha therefore saw a lookup that returned nothing, with no reason given,
    and did the only sensible thing: she asked again. One live turn spent 22
    seconds briefing the same specialist three times, each round answering in
    prose, each answer binned, until MAX_CASCADES_PER_TURN stopped it.

    SO THE PROSE IS CARRIED, NOT PARSED. It rides `_note`, which already exists
    for exactly this — free text the model wrote — and which DELEGATING already
    tells Aisha to read as what a specialist ASKED FOR, never as what was done.
    Nothing is promoted to a finding and no entity is invented: an unparsed
    sentence is not a fact, and only `actions` marked executed are ever true.
    """
    text = (text or "").strip()
    return {"_note": text[:PROSE_NOTE_MAX_CHARS]} if text else {}


def _parse_findings(text: str) -> dict:
    """Pull the JSON object out of the reply. Tolerant, but never invents.

    A model that has just read a long conversational prompt sometimes wraps its
    JSON in a sentence. Extracting it is fine; GUESSING at findings when there is
    no JSON is not — so a reply we cannot parse yields no findings and no
    entities. It does not yield NOTHING, though: see _prose_note for the 22
    seconds that distinction cost live.
    """
    text = (text or "").strip()
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return _prose_note(text)
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return _prose_note(text)
    if not isinstance(parsed, dict):
        return _prose_note(text)
    findings = parsed.get("findings")
    if isinstance(findings, dict):
        out = dict(findings)
    else:
        # THE WRAPPER IS OPTIONAL, BECAUSE THE MODEL TREATS IT AS OPTIONAL.
        # Measured live 2026-08-29: a Sales step wrote 1158 chars of valid JSON
        # with the results at the TOP level — no `findings` wrapper — and every
        # byte was discarded here, six seconds of generation for nothing. The
        # same shape is in _harvest_entities' docstring from an earlier run, so
        # this is the second time it has cost something.
        #
        # ASKING HARDER IS THE WRONG FIX (agent/sanitise.py, rule 17, and the
        # findings instruction has already been sharpened once). The reply IS
        # the findings when it is not wrapped in one — reading it that way is
        # deterministic, and the reserved keys stay reserved so a model cannot
        # smuggle a proposal in by omitting the wrapper.
        out = {
            k: v for k, v in parsed.items()
            if k not in ("note", "proposals", "findings")
        }
    if parsed.get("note"):
        out["_note"] = str(parsed["note"])
    # Carried under a reserved key so it rides the existing runner contract
    # rather than widening it; run.py lifts it off into StepResult.proposals so
    # it never reaches Aisha's compose payload as if it were a fact.
    proposals = parsed.get("proposals")
    if isinstance(proposals, list) and proposals:
        out["_proposals"] = [p for p in proposals if isinstance(p, dict)]
    return out


def _order_produces(agent: str) -> set:
    from app.orchestrator.order import produces

    return produces(agent)


_BRIEF_VEHICLE_RE = re.compile(r"vehicle_id\W{0,3}(\d+)")


def _reconcile_vehicle(known: dict, brief: str, db) -> dict:
    """The brief's own vehicle_id replaces a stale one in `known`.

    Aisha writes the id she read off the card the customer tapped, and it is
    the only place the customer's CHOICE reaches this step — a chip tap is not
    a tool result, so session_facts never learns about it. Deterministic:
    exactly one distinct id named in the brief, or nothing changes. When the
    id changes, the name is re-read from the catalogue row (or dropped), so
    the specialist is never handed the old car's name beside the new car's id.
    Never raises; a failed lookup only drops the name.
    """
    ids = {int(m) for m in _BRIEF_VEHICLE_RE.findall(brief or "")}
    if len(ids) != 1:
        return known
    (named,) = ids
    if known.get("vehicle_id") in (None, named):
        return known
    out = dict(known)
    log.info("brief names vehicle_id %s; established %s set aside", named, out["vehicle_id"])
    out["vehicle_id"] = named
    out.pop("vehicle_name", None)
    out.pop("variant_id", None)
    try:
        if db is not None:
            from app.models import VehicleModel

            row = db.get(VehicleModel, named)
            if row is not None and getattr(row, "name", None):
                out["vehicle_name"] = row.name
    except Exception:  # noqa: BLE001 — a name is a nicety, the id is the fact
        log.warning("could not read the name for vehicle_id %s", named, exc_info=True)
    return out


def run_step(
    step,
    context: dict,
    *,
    db,
    session_id: str,
    customer_id=None,
    emit=None,
    timeout: float = 20.0,
    max_iterations: int = 6,
    model: str | None = None,
    profile: dict | None = None,
) -> dict:
    """Run one brief on Haiku and return its findings. Never raises.

    The signature matches what `turn.execute` passes its runner, so this drops
    in where the stub was.
    """
    from app import config
    from app.agent import graph as _graph
    from app.agent import tool_node
    from app.agent.prompt import _DATE_ANCHOR, _relative_dates
    from app.agents import loader
    from app.tools import SUBAGENT_TOOL_FNS

    started = time.monotonic()
    model = model or config.CLAUDE_MODEL
    correlation_id = uuid.uuid4().hex

    # THE DATE ANCHOR, and it is not optional (found live, 2026-08-29). A
    # sub-agent's prompt is its card body and nothing else, so it had no idea
    # what day it was: asked for "slots at Deira on Saturday" it resolved
    # Saturday to 11 Jan 2025, looked up that date, and reported "no slots
    # available" — a wrong answer that reads exactly like a right one, because
    # the date it used never appears in the findings.
    #
    # The single agent has been immune since prompt.py:510 appends this same
    # anchor: Python owns the calendar and the model only reads it out. The
    # sub-agents were split off that path and inherited none of it. Any agent
    # that can be asked about a day needs it, so it goes on ALL of them rather
    # than on TestDrive alone — Finance quotes tenures, Sales quotes arrival
    # dates, and the next one has not been written yet.
    system = (
        loader.body_for(step.agent)
        + _DATE_ANCHOR.format(**_relative_dates(date.today()))
        + "\n\n"
        + FINDINGS_INSTRUCTION
        + _proposals_clause(step.agent)
    )
    schemas = _schemas_for(step.agent)
    # THE BINDING, ENFORCED AT THE EXECUTION SITE — the same lesson dispatch
    # learned for Aisha on 2026-08-29, relearned here on 2026-08-31. The card
    # prose is ported single-agent journey text and it SAYS "call
    # cancel_booking"; Haiku obliged with a tool_use it was never offered, and
    # with no `allow` this loop ran it through SUBAGENT_TOOL_FNS — which holds
    # every write. The booking survived only because a sub-agent session never
    # commits; the model saw the call go through and reported the cancellation
    # as done. A schema list constrains what is OFFERED, never what a model
    # emits — the allowlist has to travel with the dispatch.
    allowed = frozenset(s["name"] for s in schemas)
    # WHAT THIS AGENT MAY ASK FOR — the same card list `_proposals_clause`
    # advertises. A tool_use naming one of these is intercepted below rather
    # than refused: see the loop.
    proposable = frozenset(_proposes(step.agent))

    # WHAT THE SUB-AGENT IS TOLD, AND WHAT IT IS NOT. The brief, the entities an
    # earlier step produced, and the customer's stated CONSTRAINTS. Never a name,
    # a mobile or a booking ref — three more models see conversation context from
    # here, and this is the data-minimisation line (see agent/profile.py).
    parts = [f"Request: {step.brief}"]
    known = {k: v for k, v in (context or {}).items() if v is not None}
    # THE CAR THE CUSTOMER CHOSE, NOT THE CAR THE SEARCH LED WITH (found live,
    # 2026-09-03, three sessions in one morning). session_facts.vehicle_id is
    # harvested first-row-wins from a ranked search — the X70 FL Plus — and is
    # never touched when the customer taps the SECOND card and books the T2.
    # The brief then says vehicle_id 3, this line said "Already established:
    # vehicle_id 7", and the specialist, reading a contradiction, wrote prose
    # instead of the proposal ("these do not match") — twice per turn, until
    # Aisha apologised for a "technical issue". The brief is what the customer
    # confirmed; a held slot is what will actually be booked (booking.create
    # refuses any other vehicle for it). Both outrank a stale search fact.
    known = _reconcile_vehicle(known, step.brief, db)
    # THE BOOKING STATE GOES IN AS DATA, not instruction (found live,
    # 2026-08-31). A hold sets the slot to `locked`, and a sub-agent briefed to
    # finish the booking used to re-verify with get_available_slots, find the
    # customer's own held slot missing, and report their choice as taken — on
    # every slot they picked. Handing it the SAME derived state the write gate
    # enforces (booking_state.read, never raises) means the cascade and the
    # gate cannot disagree about where the booking is: a held slot arrives as
    # an established fact, and the remaining step is to propose create_booking.
    try:
        from app.orchestrator import booking_state as _booking_state

        _bstate = (
            _booking_state.read(db, session_id, customer_id=customer_id)
            if db is not None else None
        )
        if _bstate is not None and _bstate.slot_id is not None:
            known.setdefault("held_slot_id", _bstate.slot_id)
            known.setdefault("booking_stage", _bstate.stage)
            known.setdefault("licence_acknowledged", _bstate.licence_acknowledged)
            # The held slot's own row names the car (slots.facts_for_slot) —
            # the same source tool_node folds on lock_slot. It outranks both
            # the brief and any earlier fact: booking.create will refuse a
            # different vehicle for this slot, so telling the specialist any
            # other car can only produce a refusal or a contradiction.
            from app.services import slots as _slots

            held = _slots.facts_for_slot(db, _bstate.slot_id)
            if held.get("vehicle_id") is not None:
                if known.get("vehicle_id") not in (None, held["vehicle_id"]):
                    log.info(
                        "%s: established vehicle_id %s replaced by the held slot's %s",
                        step.agent, known.get("vehicle_id"), held["vehicle_id"],
                    )
                known["vehicle_id"] = held["vehicle_id"]
                if held.get("vehicle_name"):
                    known["vehicle_name"] = held["vehicle_name"]
        elif _bstate is not None and "hold_slot" in proposable:
            # NO HOLD — THE 2026-08-31 INCIDENT FROM THE OTHER SIDE (found
            # live, 2026-09-01). A customer accepted the one offered slot IN
            # WORDS ("Yes, book it!"), so no lock was ever taken, and the step
            # briefed to finish the booking re-verified with
            # get_available_slots — where an unheld slot has no held_by_you
            # protection: the 3-result cap (and a dropped daypart) left the
            # accepted, still-open slot out, and the step read absence as
            # "taken". Aisha then told the customer their open slot was gone.
            # The held case above passes state as DATA; here there is no fact
            # to pass — only the misreading to forbid — so it is the one place
            # the brief carries an instruction instead.
            parts.append(_NO_HOLD_BRIEF)
    except Exception:  # noqa: BLE001 — context enrichment must never kill a step
        log.warning("could not read booking state for %s", step.agent, exc_info=True)
    if known:
        parts.append(f"Already established: {json.dumps(known)}")
    if profile:
        parts.append(f"What the customer has said they want: {json.dumps(profile)}")
    messages: list[dict] = [{"role": "user", "content": "\n".join(parts)}]
    seen_results: list[dict] = []
    # WHAT THE TOOLS RETURNED, kept whole. seen_results above is the bare list
    # _harvest_entities reads ids out of; this one keeps the tool NAME with the
    # result, because Aisha cannot use `{"rows": [...]}` without knowing it came
    # from compare_vehicles. See _EVIDENCE in run.py for why she gets it at all.
    evidence: list[dict] = []
    # WHAT THE STEP ACTUALLY DID, for the line logged on the way out. Without
    # it a slow cascade is opaque from Aisha's side: her log shows only that
    # the orchestrator ran, never which specialist called what, how many times,
    # or where the seconds went. Every duplicate card on screen is one of these
    # calls, so the tool sequence IS the explanation for a repeated card.
    called: list[str] = []
    # Write tool_uses READ AS PROPOSALS (see the dispatch loop). Kept apart from
    # whatever the model writes into its JSON so the two can be merged, deduped
    # and validated together at the settle point.
    intercepted: list[dict] = []
    rounds = 0
    # Summed across the step's rounds. `cache_read` is the number that says
    # whether the caching above is working at all: it is 0 on the first round
    # of the day and should dominate every round after.
    usage = {"uncached": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    # WHERE THE OUTPUT TOKENS WENT. A step costs ~1s + 17.5ms per token it
    # writes, so this is the whole latency story — and the `entities=` line in
    # run.py cannot tell it, because that lists only the SURVIVING keys. A long
    # `note` (underscored, so filtered out of that list) or prose wrapped around
    # the JSON (discarded by _parse_findings) is billed in full and shows up
    # nowhere. One step wrote 483 tokens and yielded two keys; these three
    # numbers are what say which of those it was.
    args_chars = 0
    reply_chars = 0
    json_chars = 0
    # Set when the model ran out of budget mid-sentence, and when the write it
    # was asked for had to be read back off the state instead of its reply.
    truncated = False
    derived = False

    client = _client()
    # State reads, vehicle reconciliation and prompt assembly, before any
    # model call. Timed because the step's `settled` line only starts the
    # clock at `started` and nothing separated this from the first round.
    log.info(
        "%s prep %dms before its first call", step.agent,
        int((time.monotonic() - started) * 1000),
    )

    for _ in range(max_iterations):

        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{step.agent} exceeded {timeout:.0f}s")

        rounds += 1
        response = client.messages.create(
            model=model,
            # 256, DOWN FROM 400 (2026-08-31), which was down from 1024. Measured
            # on a live run: the largest legitimate findings reply was 821 chars
            # of JSON — about 205 tokens — with a 154-char note beside it. 256
            # clears that and binds only on the blowouts this ceiling exists for.
            # Truncation stays safe by construction: _harvest_entities reads the
            # ids out of the tool RESULTS and `_evidence` carries those results to
            # Aisha whole, so a cut-off JSON costs a note, not a fact.
            # A tool round needs ~50 tokens of
            # arguments; the findings round needs ids and one note — yet Haiku
            # was measured spending ~6s of an 11s cascade writing 1,156 tokens
            # of JSON it had been told not to write, copying tool results the
            # evidence channel already carries. The instruction asks for
            # brevity; this enforces it. Entities still thread even through a
            # truncated reply: _harvest_entities reads them from the raw tool
            # results, not from the model's JSON.
            #
            # 512, UP FROM 256 (2026-09-04), and this is the first time the
            # number has been set from the failures rather than from the
            # largest success. Measured over all 64 steps in the test VM's
            # journal: 25 of them — 39% — were discarded whole by
            # _parse_findings. Parsed replies run 94..691 chars; the discarded
            # ones start at 563 and reach 1,126. 256 tokens is ~650 chars, so
            # the ceiling sat INSIDE the legitimate range and the two
            # distributions overlap across it. It was chosen to just clear the
            # largest reply then observed, which left no headroom at all.
            #
            # 11 of those 25 were cut mid-JSON; the other 14 never reached the
            # JSON, having spent the budget on prose. _prose_note's docstring
            # reads that second group as a formatting habit ("30% returned a
            # reply with no JSON object in it at all") — it is the same
            # truncation, hit earlier.
            #
            # THE BLOWOUT THIS CEILING EXISTS FOR IS STILL BOUNDED: the 1,156
            # tokens of copied tool results that took it from 1024 to 400 to
            # 256 remain well outside 512. And truncation is no longer silent —
            # `stop_reason` is read below, so the next time this number is
            # wrong the log says so instead of a customer finding out.
            max_tokens=512,
            # CACHED, AND IT WAS NOT (found live, 2026-08-29). A card body is
            # 7-10k tokens and this loop re-sent every one of them, uncached,
            # on every round — while the single agent had cached its prefix
            # since day one. One compare turn spent ~84k input tokens re-reading
            # the same sales card nine times. The two breakpoints are the same
            # pair graph.py uses and are documented there: `tools -> system` is
            # the fixed render order, so marking the end of `system` caches the
            # schemas with it, and the 1h TTL is right for the same reason it is
            # right for Aisha — the next customer, not the next round, is who
            # re-reads it.
            #
            # WHAT KEEPS IT HITTING: _schemas_for(agent) must stay byte-stable,
            # which it is (card frontmatter order, no per-session assembly), and
            # `system` is the card body plus a DATE-granular anchor — so it rolls
            # once at midnight and not per request. A cache_read of 0 in the
            # `settled` line below is the only detector if that ever changes.
            system=_graph._system_blocks(system),
            tools=schemas,
            messages=_graph._cached_messages(messages),
        )

        if getattr(response, "stop_reason", None) == "max_tokens":
            truncated = True

        u = getattr(response, "usage", None)
        if u is not None:
            usage["uncached"] += getattr(u, "input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0

        blocks = list(response.content or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            text = "".join(
                getattr(b, "text", "") for b in blocks
                if getattr(b, "type", None) == "text"
            )
            reply_chars = len(text)
            _s, _e = text.find("{"), text.rfind("}")
            json_chars = (_e - _s + 1) if (_s != -1 and _e > _s) else 0
            findings = _parse_findings(text)
            # THE DECLARED ENTITIES COME FROM THE TOOL RESULTS, NOT THE MODEL.
            # Whatever it wrote is kept as context; the ids the next step needs
            # are read from what the tools actually returned. See
            # _harvest_entities for the live failure that made this necessary.
            harvested = _harvest_entities(
                step.agent, seen_results, [e["tool"] for e in evidence]
            )
            for key, value in harvested.items():
                findings.setdefault(key, value)
            # WHAT IT ASKED FOR, however it asked. A model that called the
            # tool AND wrote the proposal has asked once, not twice — deduped
            # on (action, args) because write.execute_all runs a list in order
            # and would otherwise reschedule the same booking to the same slot
            # twice. Intercepts come first: they carry the tool's own argument
            # names, already normalised.
            proposed = list(intercepted)
            for item in findings.get("_proposals") or []:
                if isinstance(item, dict):
                    proposed.append(
                        {"action": item.get("action"),
                         "args": _proposal_args(item.get("args") or {})}
                    )
            kept = _finalise_proposals(db, session_id, step.agent, proposed)
            if not kept and not proposed:
                # NOTHING ASKED FOR. Before reporting that, check whether the
                # brief asked for a write the state already determines — see
                # _derived_proposals for the 39% of replies that never survive
                # the token ceiling, and the booking that died in one.
                derived_asks = _derived_proposals(db, session_id, step, customer_id)
                if derived_asks:
                    derived = True
                    kept = _finalise_proposals(db, session_id, step.agent, derived_asks)
            if kept:
                findings["_proposals"] = kept
            else:
                findings.pop("_proposals", None)

            # THE REPLY WAS CUT OFF, AND THAT IS NOT THE SAME AS HAVING NOTHING
            # TO SAY. Never read before (2026-09-04): `stop_reason` was
            # available on every response and consulted nowhere, so a step the
            # ceiling truncated and a step that genuinely proposed nothing
            # reached Aisha as the same event — and she escalated a customer
            # who was one write away from booked. Carried under a reserved key
            # so turn.py can tell her to re-brief instead.
            if truncated:
                findings["_truncated"] = True
                log.warning(
                    "%s was cut off at max_tokens (%d chars of reply, %d parsed"
                    "%s) — session=%s",
                    step.agent, reply_chars, json_chars,
                    "; recovered the write from state" if derived else "",
                    session_id,
                )
            # Lifted off findings by run.execute, like _proposals. Underscored
            # so it can never be mistaken for something the model wrote.
            if evidence:
                findings["_evidence"] = evidence
            log.info(
                "%s settled in %d round(s), %dms — model=%s tools=[%s] "
                "(uncached=%d cache_write=%d cache_read=%d output=%d)",
                step.agent, rounds, int((time.monotonic() - started) * 1000),
                model,
                ", ".join(called) or "none",
                usage["uncached"], usage["cache_write"],
                usage["cache_read"], usage["output"],
            )
            log.info(
                "%s wrote: %d chars of tool args, %d chars of reply "
                "(%d json, %d discarded prose), note %d chars, %d finding key(s)",
                step.agent, args_chars, reply_chars, json_chars,
                reply_chars - json_chars,
                len(findings.get("_note") or ""),
                len([k for k in findings if not k.startswith("_")]),
            )
            return findings

        messages.append({"role": "assistant", "content": blocks})
        results = []
        settle_after_tools = False
        for use in tool_uses:
            called.append(use.name)
            args_chars += len(json.dumps(dict(use.input or {}), default=str))

            # A WRITE IT MAY ASK FOR IS READ AS THE ASKING (found live,
            # 2026-08-31, and this is the second attempt at it).
            #
            # The card body is ported single-agent journey prose and it SAYS
            # "call reschedule_booking"; ~10k tokens later the proposals clause
            # says to ask instead. The prose wins, every time. The first fix
            # enforced the binding at dispatch — correct, and it stopped the
            # write — but a refusal is not an outcome: measured live, a
            # reschedule went sub-agent tool_use -> refused, Aisha re-briefed,
            # sub-agent tool_use -> refused, Aisha tried the tool HERSELF ->
            # refused, and the customer was told to phone the helpline. The
            # booking never moved. Half of all cascades that day ended this way.
            #
            # ASKING HARDER IS THE WRONG FIX and this repo has learned it twice
            # already (sanitise.py on rule 17, _harvest_entities on the ids).
            # The request is not something the model needs to re-express in a
            # different shape: a tool_use for `cancel_booking` with a ref IS the
            # proposal, in the tool's own argument names, and reading it as one
            # is deterministic. So it is lifted here and the round is spent.
            #
            # NOTHING IS EXECUTED AND NO GATE MOVES. This produces a REQUEST on
            # exactly the path a well-behaved proposal takes — _validate_proposals
            # checks it against the card, booking_state says whether it is valid
            # now, and the service guards refuse independently underneath. The
            # allowlist below is untouched: a write this agent may NOT propose
            # still reaches _dispatch and is still refused.
            asked = _TOOL_SPELLINGS.get(use.name, use.name)
            if use.name not in allowed and asked in proposable:
                args = _proposal_args(dict(use.input or {}))
                intercepted.append({"action": asked, "args": args})
                log.info(
                    "%s asked for %s as a tool; reading it as a proposal",
                    step.agent, use.name,
                )

                # NO tool_event AND NO evidence. Nothing happened yet — a card
                # drawn from this would show the customer a change that has not
                # been decided, and `evidence` is what the TOOLS returned.
                results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": json.dumps({
                        "requested": use.name,
                        "args": args,
                        "detail": (
                            f"Recorded as a request to {use.name}. You do not "
                            "perform writes; somebody else decides whether this "
                            "happens and the customer is told by somebody else. "
                            "Do not say it has been done. Report your findings "
                            "now — there is nothing further to call."
                        ),
                    }),
                })
                continue

            result = tool_node._dispatch(
                db, session_id, use.name, dict(use.input or {}),
                customer_id, correlation_id,
                # The sub-agent surface: TOOL_FNS plus the five card reads.
                # Passed explicitly so the single agent's dispatch is untouched.
                fns=SUBAGENT_TOOL_FNS,
                allow=allowed,
            )
            if result.get("error") == "tool_not_available":
                # Dispatch's stock refusal points at emit_briefs — Aisha's
                # mechanism, which a sub-agent does not have. Its way to ask
                # for a commercial action is the proposals list.
                result["detail"] = (
                    f"{use.name} is not one of your tools. If the request "
                    "needs a commercial action — holding a slot, creating, "
                    "moving or cancelling a booking — add it to the "
                    "`proposals` list beside your findings instead; you never "
                    "perform writes yourself."
                )
            # FORWARDED, OR THE CARDS VANISH. runner._card_anchor builds the
            # vehicle and slot cards from these; a sub-agent that emits them
            # nowhere produces a turn with no cards and no error.
            if emit is not None:
                emit("tool_event", {"tool": use.name, "input": dict(use.input or {}),
                                    "output": result})
            seen_results.append(result)
            evidence.append({"tool": use.name, "result": result})
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": json.dumps(result, default=str)[:8000],
            })
        messages.append({"role": "user", "content": results})

        # THE ASK IS THE ANSWER (2026-09-04). Measured on the test VM over 28
        # live specialist steps: every testdrive step paid a second round,
        # because the gate below is closed to any card that may propose a
        # write — and 6 of those steps had ALREADY asked, as a tool_use the
        # intercept above read. The round that followed produced the same ask
        # again ("asked for reschedule_booking twice; keeping the first"), or
        # a JSON summary of tool results Aisha receives in full as `_evidence`.
        # At ~2.2s a round, that is the difference between a 3-round and a
        # 1-round booking step (7.9s vs 3.5s median).
        #
        # ONLY A TERMINAL ASK SETTLES. A booking, a move or a cancellation is
        # the last thing a step can want; the intercept's own tool_result has
        # already told the model there is nothing further to call. A lone
        # hold does not settle — see _TERMINAL_ASKS — because the prose walks
        # the model through "lock, then reschedule" and cutting it after the
        # lock would cut the move.
        #
        # THE SAME GUARDS AS THE SUMMARY PATH, by construction: the proposals
        # go through _finalise_proposals exactly as a written one would, and
        # write.execute_all's state gate and the service guards run underneath.
        from app.orchestrator.write import _ACTION_TO_TOOL as _to_tool

        if intercepted and any(
            _to_tool.get(p.get("action") or "") in _TERMINAL_ASKS for p in intercepted
        ):
            findings = _harvest_entities(
                step.agent, seen_results, [e["tool"] for e in evidence]
            )
            kept = _finalise_proposals(db, session_id, step.agent, list(intercepted))
            if kept:
                findings["_proposals"] = kept
            if evidence:
                findings["_evidence"] = evidence
            log.info(
                "%s settled on its ask in %d round(s), %dms — model=%s tools=[%s] "
                "asked=[%s] (uncached=%d cache_write=%d cache_read=%d output=%d)",
                step.agent, rounds, int((time.monotonic() - started) * 1000),
                model, ", ".join(called) or "none",
                ", ".join(p.get("action") or "?" for p in kept),
                usage["uncached"], usage["cache_write"],
                usage["cache_read"], usage["output"],
            )
            return findings

        # THE SUMMARY ROUND, WHEN NOTHING READS IT (measured 2026-08-31 over 40

        # live steps). A round costs ~2,187ms and 29 of those 40 took two: one
        # to call the tool, one to write findings about what came back. That
        # second round is a whole Haiku call for a paragraph the pipeline does
        # not consult — _harvest_entities reads the ids out of the tool RESULTS,
        # and `_evidence` hands Aisha those results in full. One step spent 821
        # characters of JSON to deliver two keys; another wrote 327 characters
        # and delivered none.
        #
        # TWO CONDITIONS, BOTH NECESSARY.
        #
        # `proposes` EMPTY. An agent that can ask for a write must see its tool
        # results and then decide — TestDrive reads the free times and proposes
        # a hold on one of them. Cutting its second round would cut the ask.
        # Sales and Finance can ask for nothing, so for them the round is
        # summary and nothing else.
        #
        # EVERY DECLARED ENTITY ALREADY HARVESTED. This is what keeps tool
        # CHAINING intact: Sales legitimately searches, then looks up details on
        # what it found, and a step that has not yet produced its vehicle_id has
        # not finished working. Settling only once the card's `produces` are all
        # in hand means an unfinished step still gets its next round.
        #
        # IT DID NOT FIRE FOR FINANCE UNTIL 2026-08-31, and the reason was a
        # typo with a two-round price on it: the card declared `produces:
        # [emi_estimate, on_road_price]` while its tools return `on_road_aed`,
        # so nothing was ever harvested and every finance step logged
        # `entities=[]`. Fixed where it belonged, in the card — see
        # app/agents/finance/v1.md.
        if not proposable:
            wanted = _order_produces(step.agent)
            got = _harvest_entities(
                step.agent, seen_results, [e["tool"] for e in evidence]
            )
            clean = not any(
                isinstance(r, dict) and r.get("error") for r in seen_results
            )
            if wanted and wanted <= set(got) and clean:
                settle_after_tools = True
            # TERMINAL READS SETTLE TOO (2026-09-02). The gate above never
            # fires for a knowledge question: search_knowledge produces no
            # card entity, so a warranty lookup paid the ~2,187ms summary
            # round the measurement above describes — a Haiku call writing
            # prose about passages Aisha receives verbatim in `_evidence`
            # anyway. When EVERY tool this step has called is a
            # self-contained document read — one that no specialist ever
            # chains a further tool call from — the tool round IS the answer.
            #
            # THE SET IS DELIBERATELY NARROW. compare_vehicles and
            # get_finance_options are excluded: a compare brief legitimately
            # follows up with search_knowledge ("...and which has the better
            # warranty"), and finance's useful chain (details -> variants ->
            # estimate) must keep its rounds. Settling on ALL tools called,
            # not just this round's, is what keeps a mixed step — search
            # first, then read a document — on the normal path.
            elif clean and called and all(t in TERMINAL_READS for t in called):
                settle_after_tools = True

        if settle_after_tools:
            findings = dict(got)
            if evidence:
                findings["_evidence"] = evidence
            log.info(
                "%s settled after its tool round in %d round(s), %dms — "
                "model=%s tools=[%s] (no summary round: proposes nothing and "
                "produced %s)",
                step.agent, rounds, int((time.monotonic() - started) * 1000),
                model, ", ".join(called) or "none", sorted(wanted),
            )
            return findings

    # ITERATIONS EXHAUSTED IS A FAILED STEP, not a silent empty one. A sub-agent
    # still looping after six rounds on one brief is lost, and the honest outcome
    # is a step the customer hears about.
    log.error("%s did not settle in %d rounds, %dms — tools=[%s]",
              step.agent, max_iterations, int((time.monotonic() - started) * 1000),
              ", ".join(called) or "none")
    raise RuntimeError(f"{step.agent} did not settle in {max_iterations} rounds")


def make_runner(
    db,
    session_id: str,
    *,
    customer_id=None,
    profile: dict | None = None,
    session_factory=None,
):
    """Bind the trusted context once and hand back the runner turn.execute wants.

    The context is closed over rather than passed through the step, so nothing a
    model produced can reach it.

    `session_factory` IS WHAT MAKES A PARALLEL WAVE LEGAL. A SQLAlchemy Session
    is not thread-safe, so two steps running at once on the shared `db` would
    interleave queries on one connection — which does not raise reliably, it
    corrupts results. Given a factory, each step opens and closes its own
    session and touches nothing the caller owns. Omit it and the runner behaves
    exactly as before, on the caller's session, single-threaded: run.execute
    will not widen a wave it cannot give sessions to.

    SAFE BECAUSE SUB-AGENTS ONLY READ. Their cards bind read-only tools and
    _dispatch never commits here — the write path is _execute_proposals, which
    runs afterwards on the caller's session, in one thread, as it always did.
    """
    def _runner(step, context, *, emit=None, timeout=20.0, max_iterations=6):
        if session_factory is None:
            return run_step(
                step, context, db=db, session_id=session_id,
                customer_id=customer_id, emit=emit, timeout=timeout,
                max_iterations=max_iterations, profile=profile,
            )

        own = session_factory()
        try:
            return run_step(
                step, context, db=own, session_id=session_id,
                customer_id=customer_id, emit=emit, timeout=timeout,
                max_iterations=max_iterations, profile=profile,
            )
        finally:
            # Never commits: whatever a read left pending is discarded with the
            # session, so a sub-agent cannot persist anything by accident.
            own.rollback()
            own.close()

    # run.execute reads this to decide whether a wave may be widened.
    _runner.thread_safe = session_factory is not None  # type: ignore[attr-defined]
    return _runner
