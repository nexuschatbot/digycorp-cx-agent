"""What kind of turn is this? (T3.4)

THE ORDER IS STATE MACHINE, THEN STICKY, THEN PLANNING — and it is not the
obvious one.

    1. STATE MACHINE   is the customer confirming something we proposed?
    2. STICKY          is this a follow-up to the agent already working?
    3. PLANNING        otherwise, Aisha writes briefs and the orchestrator routes.

A BARE "YES" IS THE CASE THAT FORCES THIS ORDER. Sticky routing, which shipped
first and is reused unchanged, would read "yes" as a follow-up and hand it to
whichever read-only agent spoke last — where it can do nothing at all, because
the thing being confirmed is a WRITE and writes are the orchestrator's. Asking
"which agent said something recently?" before "is anyone waiting on an answer?"
gets it wrong every time a booking is being confirmed, which is the highest-value
turn in the system.

So the question "is anything pending confirmation?" is asked FIRST, of state, and
only if nothing is does the conversational heuristic get a look.

THE STATE MACHINE ITSELF LANDS IN PHASE 4 (T4.3). It is injected here rather
than imported, so this ordering exists, is tested, and is already correct when
the machine arrives — rather than being retrofitted around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("app.orchestrator.cascade")


@dataclass(frozen=True)
class Path:
    kind: str            # "confirmation" | "sticky" | "planning"
    agent: str | None = None
    reason: str = ""


def decide(
    message: str,
    *,
    current_agent: str | None = None,
    pending_confirmation=None,
) -> Path:
    """Which path this turn takes. Never raises.

    `pending_confirmation(message)` returns what this MESSAGE confirms, or None.

    IT TAKES THE MESSAGE ON PURPOSE. An earlier version asked only "is anything
    pending?" and claimed the turn whenever something was — so with a hold
    awaiting a yes, "and Sunday?" was swallowed as a confirmation when it is
    plainly a new question about a different day. Whether a message answers the
    outstanding question is a judgement about BOOKING STATE, and it belongs to
    the state machine that holds that state, not to this three-line router.
    """
    text = (message or "").strip()

    # 1. STATE FIRST. A confirmation belongs to whoever asked the question, and
    #    what is being confirmed is a write — so it never reaches a read-only
    #    agent, whatever the words look like.
    if pending_confirmation is not None:
        try:
            pending = pending_confirmation(text)
        except Exception:  # noqa: BLE001
            log.exception("pending-confirmation check failed — falling through")
            pending = None
        if pending:
            return Path(
                kind="confirmation",
                reason=f"awaiting confirmation of {pending}",
            )

    # 2. STICKY. Reused unchanged from the shipped router — a genuine follow-up
    #    ("and Sunday?") stays with the agent already working, and does not pay
    #    a routing call to learn that.
    try:
        from app.agent.router import sticky

        stuck = sticky.decide(text, current_agent)
    except Exception:  # noqa: BLE001
        log.exception("sticky check failed — falling through to planning")
        stuck = None
    if stuck:
        return Path(kind="sticky", agent=stuck, reason="follow-up to the current agent")

    # 3. PLANNING. Aisha writes briefs; select/order/run take it from here.
    return Path(kind="planning", reason="new request")
