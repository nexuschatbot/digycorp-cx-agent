"""Layer 4: what to do with a classification (architecture doc §4.1 step 4).

The thresholds encode one asymmetry: STAYING is cheap and SWITCHING is expensive.
A wrong stay costs a slightly worse answer from an agent that still has the
customer's context. A wrong switch drops the thread mid-question and hands the
customer to an agent holding different tools. So a switch has to clear a higher
bar than a stay, and clear it on TWO measures — absolute confidence AND margin
over the runner-up — because a confident-looking 0.86/0.84 split is not a
decision, it is a coin toss wearing a number.

Order matters and is not the doc's reading order: the SWITCH gate is evaluated
first, so a 0.95-confidence classification that implies a switch still fails if
its margin is thin.

THE NUMBERS ARE STARTING POINTS. They are tuned against evals/routing_cases.py
and, once there is traffic, against turn_trace. Nothing here is a truth.
"""

from __future__ import annotations

from dataclasses import dataclass

SWITCH_MIN_CONFIDENCE = 0.85
SWITCH_MIN_MARGIN = 0.15
ROUTE_CONFIDENCE = 0.90
CLARIFY_FLOOR = 0.70

FALLBACK = "fallback"


@dataclass(frozen=True)
class RouteDecision:
    """A classifier's answer. Mirrors the doc's pydantic shape."""

    agent: str
    confidence: float
    second_best: str = FALLBACK
    margin: float = 0.0
    intents: tuple[str, ...] = ()
    # TRUE means the classifier did not answer — timeout, API error, unparseable
    # output — as opposed to answering "I cannot tell". The two look identical in
    # the numbers and must not be treated alike: an unsure model is information,
    # a failed call is our outage, and the customer should not pay for ours with
    # a clarifying question they did not earn.
    failed: bool = False


@dataclass(frozen=True)
class Routing:
    """What the router actually did, and why — one row of turn_trace."""

    agent: str
    reason: str
    # Which layer decided: "prefilter" | "sticky" | "classifier". `reason` is
    # prose for a human reading a trace; this is the field you GROUP BY. The
    # share settled without a classifier call is both the cost metric and the
    # first thing to check when voice p95 moves.
    layer: str = "classifier"
    clarify: bool = False
    decision: RouteDecision | None = None


def apply(decision: RouteDecision, current_agent: str | None) -> Routing:
    """Turn a classification into a routing, under the threshold policy."""
    switching = current_agent is not None and decision.agent != current_agent

    if switching:
        confident = decision.confidence >= SWITCH_MIN_CONFIDENCE
        clear = decision.margin >= SWITCH_MIN_MARGIN
        if confident and clear:
            return Routing(decision.agent, "switch: confident and clear", decision=decision)
        if decision.confidence >= CLARIFY_FLOOR:
            # Enough signal to suspect a switch, not enough to make one. Ask.
            return Routing(current_agent, "switch withheld: ask one question",
                           clarify=True, decision=decision)
        return Routing(current_agent, "switch withheld: low confidence", decision=decision)

    if decision.confidence >= ROUTE_CONFIDENCE:
        return Routing(decision.agent, "confident", decision=decision)

    if decision.confidence >= CLARIFY_FLOOR:
        # Same agent (or none active): a middling score is still the best guess,
        # and staying put costs nothing.
        return Routing(decision.agent, "same agent, middling confidence", decision=decision)

    if current_agent:
        return Routing(current_agent, "unclear: stay put", decision=decision)
    return Routing(FALLBACK, "unclear with no active agent: clarify once",
                   clarify=True, decision=decision)
