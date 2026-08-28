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


def budget_for(steps: int, deadline_seconds: float) -> float:
    """Seconds each step may take, after reserving Aisha's composition time.

    Never returns less than a second: a budget so small that every step times
    out produces a turn that gathered nothing and says so, which is strictly
    worse than a slow answer.
    """
    if steps <= 0:
        return 0.0
    usable = max(deadline_seconds - COMPOSE_RESERVE_SECONDS, steps * 1.0)
    return max(usable / steps, 1.0)


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
) -> run.TurnResult:
    """Run the briefs, forwarding every sub-agent event to the parent stream."""
    per_step = budget_for(len(briefs), deadline_seconds)
    started = time.monotonic()

    def _runner(step, ctx):
        # A step that starts after the budget is already gone is failed rather
        # than started: beginning a call we cannot afford to finish spends money
        # and returns nothing.
        remaining = deadline_seconds - COMPOSE_RESERVE_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("no time left before composition")
        return runner_fn(
            step, ctx,
            emit=forward_events(parent_writer, step.agent),
            timeout=min(per_step, remaining),
            max_iterations=SUBAGENT_MAX_ITERATIONS,
        )

    return run.execute(briefs, _runner, known=known, judge=judge, step_timeout=per_step)


def compose_input(result: run.TurnResult) -> dict:
    """What Aisha's closing generation is given.

    FINDINGS **AND** STATUS, and the status half is the point. "Answer what
    worked, name what didn't" is impossible if the composer only sees what
    worked — it would report a half-answer as a whole one, which is worse than
    admitting the gap.
    """
    return {
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
        ],
        # Set when SOME of the answer is missing — the case she needs wording
        # for, and the one a compose prompt will otherwise paper over.
        "partial": result.partial,
        "nothing_worked": not result.any_ok and bool(result.steps),
    }
