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
    rules_only: bool = False,
) -> Routing:
    """Decide which agent answers `message`.

    `rules_only` SKIPS THE CLASSIFIER — the multi-agent chat path's mode
    (2026-09-02). On that path the routed agent selects a prompt-body FLAVOUR
    and nothing else: the tool binding ignores it (graph hands Aisha
    aisha_schemas() before the per-journey branch is ever reached, and the
    dispatch allowlist short-circuits the same way), and the un-routed body is
    a strict superset of both journey bodies, served daily. The classifier was
    measured at p50 1,829ms on 59% of production turns — a Haiku call per turn
    buying a narrower prompt of unmeasured value. The free layers stay: a
    booking reference or an explicit manage-signal still routes, and a bare
    follow-up still sticks with the active agent, so the sticky seed keeps
    carrying. When neither rule fires, FALLBACK degrades to the active agent
    or the combined prompt — exactly the classifier-outage path below, minus
    the outage. Voice and the single-agent path never pass this flag: their
    journey tool binding is real.
    """
    hit = prefilter.decide(message, current_agent)
    if hit:
        return Routing(hit, "pre-filter: explicit booking-management signal",
                       layer="prefilter")

    hit = sticky.decide(message, current_agent)
    if hit:
        return Routing(hit, "sticky: follow-up stays with the active agent",
                       layer="sticky")

    if rules_only:
        from app.agent.router.policy import FALLBACK

        return Routing(
            current_agent or FALLBACK,
            "classifier skipped: multi-agent path serves the combined prompt",
            layer="skipped",
        )

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
