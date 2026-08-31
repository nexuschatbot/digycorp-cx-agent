"""The cascade itself — the one function the graph will call (doc §4.1).

Four layers, cheapest first, each with an early exit:

    1. pre-filter   ~0ms   a booking reference, or "cancel" in words
    2. sticky gate  ~0ms   a bare follow-up belongs to whoever asked
    3. classifier  ~350ms  the only paid step
    4. policy       ~0ms   thresholds; switching costs more than staying

The ordering is a latency decision before it is a cost one. The voice budget is
p50 400ms to first audio, and layers 1, 2 and 4 are all string and float work —
so a turn that exits early spends nothing measurable, and only genuinely
ambiguous turns pay the 350ms.
"""

from __future__ import annotations

from typing import Callable

from app.agent.router import classifier, prefilter, sticky
from app.agent.router.policy import Routing, apply


def route(
    message: str,
    *,
    current_agent: str | None = None,
    session_facts: dict | None = None,
    live_bookings: list | None = None,
    history: list[str] | None = None,
    generate: Callable[..., dict] | None = None,
    model: str | None = None,
) -> Routing:
    """Decide which agent answers `message`."""
    hit = prefilter.decide(message, current_agent)
    if hit:
        return Routing(hit, "pre-filter: explicit booking-management signal",
                       layer="prefilter")

    hit = sticky.decide(message, current_agent)
    if hit:
        return Routing(hit, "sticky: follow-up stays with the active agent",
                       layer="sticky")

    decision = classifier.classify(
        message,
        current_agent=current_agent,
        session_facts=session_facts,
        live_bookings=live_bookings,
        history=history,
        generate=generate,
        model=model,
    )
    if decision.failed:
        # THE CLASSIFIER DID NOT ANSWER. Clarifying here would make the customer
        # pay for our outage with a question they did not earn — and measured
        # live, that is exactly what happened to "do you have anything hybrid".
        # So degrade to the active agent, or to the journey most traffic wants.
        # Both prompts carry the shared base, so escalation still works either
        # way; the worst case is a slightly wrong specialist, not a dead end.
        return Routing(
            current_agent or prefilter.DEFAULT_AGENT,
            "classifier unavailable: degraded to default",
            layer="degraded",
            decision=decision,
        )
    return apply(decision, current_agent)
