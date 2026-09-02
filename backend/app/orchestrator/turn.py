"""The multi-agent turn: brief, dispatch, compose (T5.1-T5.6, T5.8).

SHAPE OF ONE TURN:

    1. AISHA, one generation. She streams her opening line AND emits briefs as a
       tool call in the same generation, so first token lands ~0.5s instead of
       after everything downstream finished. The `text_break` that seals her
       bubble is what makes that safe — the opening line is DELIVERED, not
       retracted.
    2. ORCHESTRATOR. Select an agent per brief, order them, run them in
       dependency waves, thread results forward.
    3. AISHA AGAIN, composing. From findings AND per-step status, so a step that
       failed is named honestly rather than dropped.

OFF BY DEFAULT AND PER CHANNEL (T5.8). `MULTI_AGENT_ENABLED` gates the whole
thing, and voice is excluded even when it is on: `run_turn` is shared, and the
realtime path has a 400ms first-audio budget that a fan-out cannot meet — the
measured Haiku round trip alone is 1088ms. Voice stays single-agent until Phase 9
gives it its own fast path.

THE ONE THING THAT SILENTLY BLANKS THE UI (T5.3). Sub-agents emit `tool_event`s,
and `runner._card_anchor` draws vehicle and slot cards from them. If those events
are not forwarded to the PARENT stream writer, every card disappears and nothing
errors — the reply just arrives with no cards, which reads as a styling bug and
is not one. `forward_events` exists for exactly that, and a test asserts it.

TIME IS BUDGETED PER STEP, NOT PER TURN (T5.6). `AGENT_DEADLINE_SECONDS` is
stamped once for the whole cascade today, so one slow sub-agent eats Aisha's
composition budget and the turn dies with NO REPLY AT ALL — the worst possible
failure, worse than a partial answer. Composition time is reserved up front and
the remainder is divided among the steps.
"""

from __future__ import annotations

import logging
import math
import queue as _queue
import time
from dataclasses import dataclass, field

from app.orchestrator import cascade, run

log = logging.getLogger("app.orchestrator.turn")

# Reserved for Aisha's closing generation, before any step is allowed to run.
# She is the only one the customer hears; a turn that spends its whole budget
# gathering facts and cannot afford to say them has failed completely.
COMPOSE_RESERVE_SECONDS = 8.0

# Per sub-graph, well under the single agent's MAX_ITERATIONS of 16. A sub-agent
# answers one brief with read-only tools; needing more than this many rounds
# means it is lost, and the honest outcome is a failed step the customer is told
# about rather than a turn that never ends.
SUBAGENT_MAX_ITERATIONS = 6

# HOW MANY SPECIALISTS MAY WORK AT ONCE. Briefs in one wave need nothing from
# each other, so the only reason to run them in sequence was that they shared a
# database session; run.execute widens a wave only for a runner that brought its
# own. Four is above the widest brief list Aisha realistically sends and below
# anything that would matter to the connection pool.
MAX_PARALLEL_STEPS = 4


@dataclass
class TurnPlan:
    """What Aisha decided this turn is."""

    kind: str                     # "direct" | "confirmation" | "sticky" | "planning"
    opening: str = ""
    briefs: list[dict] = field(default_factory=list)
    agent: str | None = None      # for sticky
    reason: str = ""


def enabled(*, voice_call: bool = False) -> bool:
    """Whether this turn takes the multi-agent path.

    PER CHANNEL, NOT GLOBAL. Voice shares run_turn and cannot afford a fan-out:
    its first-audio budget is 400ms and a Haiku round trip alone measured
    1088ms. WhatsApp runs no turn at all (it replies with a link into web chat).
    """
    from app.config import MULTI_AGENT_ENABLED

    if not MULTI_AGENT_ENABLED:
        return False
    if voice_call:
        return False
    return True


def budget_for(steps: int, deadline_seconds: float, *, parallel: int = 1) -> float:
    """Seconds each step may take, after reserving Aisha's composition time.

    Never returns less than a second: a budget so small that every step times
    out produces a turn that gathered nothing and says so, which is strictly
    worse than a slow answer.

    DIVIDE BY WHAT ACTUALLY RUNS IN SEQUENCE. When steps run one after another
    that is the step count, and it is why this used to divide by it. Running
    `parallel` of them at once means only ceil(steps / parallel) of them are
    sequential, so dividing by the step count would hand each one a third of the
    clock it is actually free to use — and would make asking three questions in
    one call WORSE than asking them in three, which is the opposite of the point.
    """
    if steps <= 0:
        return 0.0
    sequential = max(1, math.ceil(steps / max(1, parallel)))
    usable = max(deadline_seconds - COMPOSE_RESERVE_SECONDS, sequential * 1.0)
    return max(usable / sequential, 1.0)


def forward_events(parent_writer, agent: str):
    """Wrap a sub-agent's event sink so its events reach the PARENT stream.

    THE ONE THING THAT SILENTLY BLANKS THE UI. runner._card_anchor builds the
    vehicle and slot cards from `tool_event`s. A sub-agent that emits them into
    its own graph and nowhere else produces a turn where every card is missing
    and NOTHING ERRORS — it reads as a styling bug and is not one.

    The agent name is attached so the routing view can show which specialist
    produced which card, and so a card cannot be attributed to Aisha.
    """
    def _emit(kind: str, payload: dict) -> None:
        if parent_writer is None:
            return
        try:
            enriched = dict(payload or {})
            enriched.setdefault("agent", agent)
            parent_writer(kind, enriched)
        except Exception:  # noqa: BLE001 — a UI event must never break a turn
            log.warning("could not forward %s from %s", kind, agent, exc_info=True)

    return _emit


def decide(
    message: str,
    *,
    db=None,
    session_id: str = "",
    current_agent: str | None = None,
) -> TurnPlan:
    """Which path this turn takes, before any model is called.

    Thin on purpose: the ordering rule lives in cascade.py and the booking state
    in booking_state.py. This is the seam where they meet the turn.
    """
    pending = None
    if db is not None and session_id:
        from app.orchestrator import booking_state

        pending = booking_state.pending_confirmation(db, session_id)

    path = cascade.decide(
        message, current_agent=current_agent, pending_confirmation=pending
    )
    return TurnPlan(kind=path.kind, agent=path.agent, reason=path.reason)


def execute(
    briefs: list[dict],
    runner_fn,
    *,
    known: set[str] | None = None,
    judge=None,
    deadline_seconds: float = 60.0,
    parent_writer=None,
    max_workers: int = MAX_PARALLEL_STEPS,
    preselected: dict | None = None,
) -> run.TurnResult:
    """Run the briefs, forwarding every sub-agent event to the parent stream."""
    per_step = budget_for(len(briefs), deadline_seconds, parallel=max_workers)
    started = time.monotonic()

    # EVENTS CROSS A THREAD BOUNDARY HERE, so they cross it on a queue. A wave
    # may run several specialists at once, and every one of them emits the
    # tool_events the customer's cards are built from — but `parent_writer` is
    # LangGraph's custom-stream writer and nothing documents it as thread-safe.
    # Workers put; run.execute's on_tick drains, on the one thread that owns the
    # stream. Unbounded on purpose: dropping an event here silently deletes a
    # card, which is the failure forward_events exists to prevent.
    events: _queue.Queue = _queue.Queue()

    def _drain() -> None:
        while True:
            try:
                kind, payload, agent = events.get_nowait()
            except _queue.Empty:
                return
            forward_events(parent_writer, agent)(kind, payload)

    # CARDS FOR THESE ARE CHOSEN AFTER THE CASCADE, not per call — see
    # graph._select_cards. A specialist answering a three-part question runs a
    # search per clause, and forwarding each one drew a row per query: nine
    # cards for one question, under a reply describing three. The results still
    # reach Aisha in full through `evidence`; only the DRAWING is deferred.
    CURATED = {"search_vehicles"}

    def _queue_emit(agent: str):
        def _emit(kind: str, payload: dict) -> None:
            if kind == "tool_event" and (payload or {}).get("tool") in CURATED:
                return
            events.put((kind, dict(payload or {}), agent))
        return _emit

    def _runner(step, ctx):
        # A step that starts after the budget is already gone is failed rather
        # than started: beginning a call we cannot afford to finish spends money
        # and returns nothing.
        remaining = deadline_seconds - COMPOSE_RESERVE_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("no time left before composition")
        return runner_fn(
            step, ctx,
            emit=_queue_emit(step.agent),
            timeout=min(per_step, remaining),
            max_iterations=SUBAGENT_MAX_ITERATIONS,
        )

    # `thread_safe` belongs to the runner run.execute actually calls, so the
    # wrapper has to carry the flag the wrapped runner set. Without this the
    # wave is never widened and the queue above is pure overhead.
    _runner.thread_safe = getattr(runner_fn, "thread_safe", False)  # type: ignore[attr-defined]

    try:
        return run.execute(
            briefs, _runner, known=known, judge=judge, step_timeout=per_step,
            max_workers=max_workers, on_tick=_drain, preselected=preselected,
        )
    finally:
        # Whatever a failed or timed-out step already emitted still drew cards
        # on screen; losing it here would leave the transcript disagreeing with
        # what the customer saw.
        _drain()


# HOW MUCH RAW TOOL OUTPUT REACHES AISHA, in characters across the whole turn.
#
# WHY SHE GETS IT AT ALL (measured live, 2026-08-29). She used to receive only
# `findings` — a small JSON blob the sub-agent WROTE about what the tools said.
# For "compare both" that came back as price_left/price_right/seats_left/
# seats_right and one sentence, which is not enough to answer with, so she went
# back and asked twice more: 40 seconds and two identical comparison tables for
# a question one cascade had already answered.
#
# The arithmetic is the argument. compare_vehicles returns ~257 tokens. We were
# spending 4.6s of Haiku OUTPUT compressing that into something thinner, then
# 18s of extra cascades recovering what the compression threw away. Input
# tokens are ~5x cheaper than output, they cache, and they cost no generation
# time at all. The single agent never had this problem because it called the
# tool itself and saw the whole result — the cascade LOST information, and this
# gives it back.
#
# THE BUDGET IS NOT OPTIONAL. _orchestrator_node truncates the composed payload
# at 16000 chars with a plain slice, which would cut a JSON string in half. This
# ceiling keeps the evidence inside that.
#
# 12000, UP FROM 6000 (2026-08-29). The old ceiling was sized to "the largest
# realistic single result" — but a specialist that calls two tools is routine,
# and the measured payloads (search_vehicles ~3.1k, search_knowledge ~3.0k,
# get_vehicle_details ~2.0k) meant any two-tool brief dropped evidence on the
# floor ("1 result(s) over the budget were dropped", live 2026-08-29). Dropped
# evidence is what pushed Haiku to copy specs into its findings prose — the
# composer then quoted a summary instead of the data. Room for four to five
# results keeps the raw numbers in front of the composer, still well under the
# 16k slice.
EVIDENCE_BUDGET_CHARS = 12000


def _card_proposes(agent: str | None) -> bool:
    """Whether this agent's card may ask for writes at all. Never raises —
    a loader hiccup must cost the gap annotation, not the compose."""
    if not agent:
        return False
    try:
        from app.orchestrator.subagent import _proposes

        return bool(_proposes(agent))
    except Exception:  # noqa: BLE001
        return False


def _evidence_for(steps) -> list[dict]:
    """Raw tool results for the composer, oldest first, inside the budget.

    ORDER MATTERS WHEN IT DOES NOT ALL FIT. The earliest results are the ones
    later steps were built on, so they are the ones a dependent answer needs;
    dropping from the end degrades the tail of a long cascade rather than the
    foundation of it. What is dropped is REPORTED rather than silently missing,
    because an answer composed from partial evidence must be able to say so.
    """
    import json as _json

    out: list[dict] = []
    spent = 0
    dropped = 0
    for step in steps:
        for item in step.evidence or []:
            body = _json.dumps(item.get("result"), default=str)
            if spent + len(body) > EVIDENCE_BUDGET_CHARS:
                dropped += 1
                continue
            spent += len(body)
            out.append({
                "agent": step.agent,
                "tool": item.get("tool"),
                "result": item.get("result"),
            })
    if dropped:
        log.info("evidence: %d result(s) over the %d-char budget were dropped",
                 dropped, EVIDENCE_BUDGET_CHARS)
    return out


def compose_input(result: run.TurnResult) -> dict:
    """What Aisha's closing generation is given.

    FINDINGS **AND** STATUS, and the status half is the point. "Answer what
    worked, name what didn't" is impossible if the composer only sees what
    worked — it would report a half-answer as a whole one, which is worse than
    admitting the gap.

    EVIDENCE is the third part: what the tools actually returned, verbatim. See
    EVIDENCE_BUDGET_CHARS for why it is there and what it cost not to have it.
    """
    return {
        "evidence": _evidence_for([s for s in result.steps if s.ok]),
        "findings": [
            {"agent": s.agent, "data": s.findings}
            for s in result.steps if s.ok and s.findings
        ],
        "unavailable": [
            {
                "agent": s.agent,
                "status": s.status,
                # WHY, in words she can use. "I couldn't check stock just now"
                # needs a reason; "something went wrong" does not help anyone.
                "detail": s.detail or None,
            }
            for s in result.steps if not s.ok
        ] + [
            # A STEP THAT SUCCEEDED AND RETURNED NOTHING IS A GAP, AND IT HAS TO
            # BE NAMED. It is `ok`, so it is not in `unavailable` above; it has
            # no findings, so the list above it skips it too — and Aisha is left
            # looking at a lookup that simply is not there, with no reason. What
            # she does then is ask again: one live turn on 2026-08-31 briefed
            # the same specialist three times and spent 22 seconds doing it,
            # stopped only by MAX_CASCADES_PER_TURN. Saying "this came back
            # empty" is what makes asking again pointless instead of tempting.
            #
            # NO EVIDENCE EITHER is part of the test. A step with evidence and
            # no summary is still answerable — the tool results are in front of
            # her — and calling that a gap would make her apologise for an
            # answer she is holding.
            {
                "agent": s.agent,
                "status": "empty",
                "detail": (
                    "This came back with nothing — no result and no reason. "
                    "Asking again will not change it: answer from what you "
                    "already have, or tell the customer you could not get it."
                ),
            }
            for s in result.steps
            if s.ok and not s.findings and not s.evidence
        ] + [
            # A WRITE-CAPABLE STEP THAT NARRATED INSTEAD OF PROPOSING (found
            # live, 2026-08-31; OPEN-ISSUES #1). The specialist had a settled
            # slot, wrote 1,098 characters ABOUT booking it, and proposed
            # nothing — so write.py was never handed an action, Aisha re-briefed
            # into the cascade ceiling, and a decided customer got a human
            # handoff instead of a booking. The step was `ok` with a note, so
            # neither list above named anything wrong.
            #
            # THE SIGNATURE, NOT A GUESS: the card may propose writes, the step
            # proposed none, and its findings hold no fact — only prose (a
            # `_note`) or bare evidence. A step that answered with data is a
            # read answer and is left alone; a step with nothing at all is the
            # `empty` gap above. Asking the model harder is recorded twice in
            # this repo as not the fix — this names the incomplete step to the
            # one party who can recover: the composer.
            {
                "agent": s.agent,
                "status": "no_action",
                "detail": (
                    "This specialist may only REQUEST holds, bookings, moves "
                    "and cancellations, and it requested none — so nothing was "
                    "held, booked, moved or cancelled by this step, whatever "
                    "its note says. If the customer asked for one of those, it "
                    "has NOT happened: never confirm it. Re-brief once, naming "
                    "the exact action and every detail it needs, or tell the "
                    "customer plainly and offer a person."
                ),
            }
            for s in result.steps
            if s.ok
            and not s.proposals
            and not any(not k.startswith("_") for k in (s.findings or {}))
            and ((s.findings or {}).get("_note") or s.evidence)
            and _card_proposes(s.agent)
        ],
        # Set when SOME of the answer is missing — the case she needs wording
        # for, and the one a compose prompt will otherwise paper over.
        "partial": result.partial,
        "nothing_worked": not result.any_ok and bool(result.steps),
    }
