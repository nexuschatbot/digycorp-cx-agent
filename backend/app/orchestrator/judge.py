"""The Haiku routing judge (T3.1) — the primary router, by measurement.

WHY THIS IS THE PRIMARY PATH AND NOT THE FALLBACK. The plan expected embeddings
to route most traffic with the judge as backstop. Calibration says otherwise:
the fast lane is only trustworthy on about a third of briefs (margin >= 0.02,
where the errors stop), so this is where most routing decisions are actually
made. See select.py for the evidence.

SETTINGS ARE THE CLASSIFIER'S, DELIBERATELY. Same pooled client, temperature=0,
max_retries=0, timeout=5.0. Router latency is customer-visible and a retry
doubles it; a second failed attempt at a routing call is worth less than
answering slightly worse, slightly sooner. Reusing the module rather than
re-declaring the numbers is what stops the two drifting.

THE NEGATIVES ARE THE FEW-SHOT, and that is the whole reason the cards carry
them. "how much is the X70" vs "what's the on-road price with insurance" are
nearly identical strings that belong to different agents, and no domain
paragraph separates them — an example does.

FAILS TO None, NEVER RAISES. A dead judge means the brief is unroutable, which
the orchestrator already handles: Aisha answers it herself or asks. A routing
call must never be the reason a customer sees an error.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("app.orchestrator.judge")

SYSTEM = """\
You route one request to exactly one specialist at a car dealership.

sales      — choosing a car: models, trims, specs, prices, colours, stock, \
brochures, comparisons, warranty, delivery times, accessories.
testdrive  — test drives: showrooms, days, times, availability, doorstep, and \
looking up, moving or cancelling a booking that already exists.
finance    — what a car costs to own: on-road price, instalments, banks, \
Islamic finance, down payment, tenure, offers, and buying or negotiating.

THREE DISTINCTIONS THAT DECIDE MOST CASES:
- A LIST price is sales ("how much is the X70"). A TOTAL cost of ownership is \
finance ("on-road price with insurance", "monthly instalments").
- "Change" is not always a booking. "Move my booking to Sunday" is testdrive; \
"can I change to the 7-seater instead" is sales — they are choosing a car.
- Negotiation and buying are finance, not a refusal: it answers what it can and \
hands over to a person.

Answer with the route tool: name the one specialist that owns the request, or \
"none" when no specialist does."""


def _route_tool(agents: list[str]) -> dict:
    """The forced-output tool, built from THIS router's roster.

    BUILT HERE, NOT BORROWED (2026-09-01). The judge used to reuse the legacy
    classifier's ROUTE_TOOL wholesale, and forced tool use made that schema the
    law: its enum is (testdrive, bookingmanagement, fallback), so the judge's
    Haiku call was STRUCTURALLY UNABLE to answer `sales` or `finance` — every
    finance brief came back "fallback", failed the roster check below, and was
    dropped as unroutable. 110 traced turns on the test VM and not one reached
    a specialist the legacy enum did not happen to contain.

    "none" is in the enum because forced tool use permits no other way to
    decline: there is always a tool_use block, so the deliberate null has to be
    a value the schema allows.
    """
    return {
        "name": "route",
        "description": "Record which specialist owns this request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": list(agents) + ["none"],
                    "description": (
                        'The specialist that owns the request, or "none" when '
                        "no specialist does."
                    ),
                }
            },
            "required": ["agent"],
        },
    }


def _examples() -> str:
    """Few-shot lines built from the cards' own utterances and negatives.

    READ FROM THE CARDS so the judge and the eval cannot disagree about what a
    card means. A negative on one card is a positive somewhere else, which is
    exactly the contrast a router needs to see.
    """
    from app.agents import loader

    lines: list[str] = []
    for agent in ("sales", "testdrive", "finance"):
        try:
            fm = loader.frontmatter(agent) or {}
        except Exception:  # noqa: BLE001
            continue
        for utterance in (fm.get("utterances") or [])[:6]:
            lines.append(f'"{utterance}" -> {agent}')
    return "\n".join(lines)


def judge(brief: str, agents: list[str], *, model: str | None = None) -> str | None:
    """Ask Haiku which agent owns this brief. None on anything unexpected.

    MAKES A LIVE API CALL. It is injected into select.select() rather than
    imported there, so nothing routes through the network unless a caller
    deliberately passes this function in — which is what keeps the whole
    orchestrator testable offline.
    """
    text = (brief or "").strip()
    if not text:
        return None

    try:
        from app import config
        from app.agent.router import classifier

        raw = classifier._live_generate(
            SYSTEM + "\n\nExamples:\n" + _examples(),
            f"Request: {text}",
            model=model or config.CLAUDE_MODEL,
            # THE JUDGE'S OWN ROSTER, never the legacy classifier's — see
            # _route_tool for what borrowing that schema cost.
            tool=_route_tool(agents),
        )
    except Exception:  # noqa: BLE001 — a routing call must never break a turn
        log.warning("routing judge call failed", exc_info=True)
        return None

    chosen = None
    if isinstance(raw, dict):
        chosen = raw.get("agent")
    elif isinstance(raw, str):
        try:
            chosen = json.loads(raw).get("agent")
        except Exception:  # noqa: BLE001
            chosen = None

    if chosen == "none":
        # The deliberate decline, spelled as a value because forced tool use
        # cannot emit an absence. Not an error and not worth a warning.
        return None
    if chosen not in agents:
        # Includes the deliberate null. Not an error — some briefs belong to no
        # specialist, and inventing one is worse than saying so.
        if chosen is not None:
            log.warning("routing judge returned an unknown agent: %r", chosen)
        return None
    return chosen
