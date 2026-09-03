"""Layer 3: the model call, and the only paid step in the cascade (doc §4.2).

Reached only when the rules cannot decide, which on measured traffic should be a
minority of turns. It is a CLASSIFICATION, not a conversation: the router holds
no tools, is never shown the catalogue, and its output is a label plus a number.
It never writes a word the customer sees.

STRUCTURED OUTPUT BY FORCED TOOL USE, not by asking for JSON in prose. A model
told to "reply with JSON" sometimes replies with an apology first; a model given
one tool and tool_choice={"type":"tool"} has no other move available. The schema
is the contract, and a malformed answer fails loudly here rather than becoming a
silently mis-parsed route.

INJECTABLE, like the agent node's own model call (graph._default_generate). The
`generate` parameter is what lets every test in tests/test_router.py run offline
with canned classifications, and what lets evals/routing_live.py swap in the real
call deliberately, outside pytest.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from app import config
from app.agent.router.policy import FALLBACK, RouteDecision

_log = logging.getLogger(__name__)

AGENTS = ("testdrive", "bookingmanagement", FALLBACK)

ROUTE_TOOL = {
    "name": "route",
    "description": "Record which assistant should answer this message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "enum": list(AGENTS),
                "description": "The assistant that should answer.",
            },
            "confidence": {
                "type": "number",
                "description": "0-1. How sure you are about `agent`.",
            },
            "second_best": {
                "type": "string",
                "enum": list(AGENTS),
                "description": "The next most likely assistant.",
            },
            "margin": {
                "type": "number",
                "description": "0-1. How far ahead `agent` is of `second_best`.",
            },
        },
        "required": ["agent", "confidence", "second_best", "margin"],
    },
}

# The scope lines, the examples, and — the part that earns its length — the
# NEGATIVE examples. Routing errors here are not random: they cluster on messages
# that mention a car while managing a booking, and on money.
SYSTEM = """\
You route one customer message to the assistant that should answer it. You never \
reply to the customer and you never see the answer. Output only the `route` tool.

THE ASSISTANTS

testdrive — booking a NEW test drive, start to finish. Browsing and comparing \
cars, what a model costs or includes, which showroom, which day and time, \
confirming the booking. Also every follow-up question about a car.
  "what SUVs do you have"
  "tell me about the X70 Plus"
  "how much is the T2"
  "I'd like to book a test drive on Saturday"
  "ايه الفرق بين X70 و X70 Plus"

bookingmanagement — a booking the customer ALREADY HAS. Looking it up, moving it, \
cancelling it.
  "move TD-2026-HTXE to Sunday"
  "I need to cancel my test drive"
  "what time is my booking on Thursday"
  "أريد تأجيل موعدي"

fallback — things neither assistant may settle: price negotiation, discounts, \
finance and instalments, insurance, trade-ins, complaints, defects, recalls, \
accidents, or an explicit request for a human.
  "can you do better on the price"
  "what are the monthly instalments"
  "I want to speak to someone"

THE DISTINCTIONS THAT ACTUALLY DECIDE THINGS

A PRICE QUESTION ANSWERABLE FROM THE CATALOGUE IS testdrive. What a car costs, \
what a trim includes, what separates two models — those are facts we hold. \
NEGOTIATING that price is fallback. "How much is the X70" is testdrive; "what's \
your best price on the X70" is fallback.

A CAR MENTIONED DURING A BOOKING CHANGE IS STILL bookingmanagement. "Can I move \
my X70 drive to Sunday" is about the booking, not the car.

A CAR CHANGE THAT IS NOT ABOUT AN EXISTING BOOKING IS testdrive. "Can I try the \
7-seater instead" is choosing a car, even though it says "change".

WANTING A TEST DRIVE IS testdrive EVEN WITH NO CAR NAMED. "I want to book a test \
drive" is a new booking; it is only bookingmanagement if they refer to one they \
already have.

USE THE SESSION NOTE. "What about the weekend?" is unroutable alone: it means \
availability if a new booking is in progress, and moving it if the customer is \
managing an existing one. When the note names an active assistant and the message \
adds no new intent, prefer that assistant.

CONFIDENCE IS A REAL NUMBER. Say 0.95 when the message names the journey \
outright. Say 0.5 or less when you are guessing — a low number is useful and a \
confident wrong answer is not. `margin` is how far ahead your choice is; make it \
small when two readings are both plausible.\
"""


def _user_block(message: str, *, session_note: str, history: list[str]) -> str:
    parts = [f"SESSION NOTE: {session_note}"]
    if history:
        parts.append("RECENT TURNS (oldest first):\n" + "\n".join(history[-6:]))
    parts.append(f"MESSAGE TO ROUTE:\n{message}")
    return "\n\n".join(parts)


def session_note(
    *,
    current_agent: str | None,
    session_facts: dict | None = None,
    live_bookings: list | None = None,
) -> str:
    """One line of state, because half of all follow-ups are unroutable without it."""
    facts = session_facts or {}
    bits = [f"active assistant: {current_agent or 'none yet'}"]
    if facts.get("vehicle_name"):
        bits.append(f"car in play: {facts['vehicle_name']}")
    if facts.get("showroom_name"):
        bits.append(f"showroom: {facts['showroom_name']}")
    bits.append(f"existing bookings: {len(live_bookings or [])}")
    if facts.get("held_slot") or facts.get("held"):
        bits.append("a slot is being held right now")
    return "; ".join(bits)


_client = None


def _get_client():
    """One pooled client for the process.

    Built per call, this cost more than the classification: a fresh
    anthropic.Anthropic() means a new TLS handshake to the API on every turn,
    which measured at ~1.5s p50 locally against ~250ms once the connection is
    reused. services/salesforce.py already learned this lesson and says so in its
    own header ("Keep-alive — one pooled HTTPS connection per host"); the router
    is on the same voice hot path and needs the same treatment.
    """
    global _client
    if _client is None:
        import anthropic

        from app.services.anthropic_http import pooled_http_client

        # Idle keep-alive longer than the SDK's 5s, so the judge's first call
        # of a turn does not re-handshake — see services/anthropic_http.
        http_client = pooled_http_client()
        extra = {"http_client": http_client} if http_client is not None else {}
        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            **extra,
            # FAIL FAST, DO NOT RETRY. The SDK defaults to retrying with
            # backoff, which is right for the agent's own call — that reply is
            # the product — and wrong here. A routing decision has a fallback
            # that costs nothing: stay with the active agent. Spending seconds
            # of a 400ms voice budget to retry a CLASSIFICATION trades the thing
            # the customer notices for a marginally better guess.
            #
            # Measured: with retries on, a blocked network turned a 165s test
            # suite into 673s. In production it would be silence on a phone call.
            max_retries=0,
            timeout=5.0,
        )
    return _client


def _live_generate(system: str, user: str, *, model: str, tool: dict | None = None) -> dict:
    """The real Haiku call. Client is pooled — see _get_client.

    `tool` DEFAULTS TO THIS ROUTER'S OWN ROUTE_TOOL and exists for the
    orchestrator's judge, whose roster is different (found live, 2026-09-01):
    the judge reused this function as-is, so its Haiku call was FORCED into the
    legacy enum (testdrive / bookingmanagement / fallback) and could never
    answer `sales` or `finance` however clearly the brief named them. A caller
    with its own roster must pass its own tool.
    """
    tool = tool or ROUTE_TOOL
    response = _get_client().messages.create(
        model=model,
        max_tokens=256,
        # A CLASSIFIER MUST NOT BE CREATIVE. Left at the default 1.0, the same
        # message scored 0.85 on one run and 0.65 on the next — enough to move it
        # across CLARIFY_FLOOR and change the route. Sampling variance in a
        # routing decision means a customer can be sent to a different agent for
        # saying the same words twice, which no threshold can fix downstream.
        temperature=0,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise ValueError(f"router returned no tool_use block: {response.content!r}")


def classify(
    message: str,
    *,
    current_agent: str | None = None,
    session_facts: dict | None = None,
    live_bookings: list | None = None,
    history: list[str] | None = None,
    generate: Callable[..., dict] | None = None,
    model: str | None = None,
) -> RouteDecision:
    """Classify one message. Never raises: an unusable answer becomes fallback/0.0.

    A router that throws would take the whole turn down over a classification —
    so a failure here degrades to `fallback` at zero confidence, which the policy
    layer already knows how to handle (clarify once, or stay put).
    """
    note = session_note(
        current_agent=current_agent,
        session_facts=session_facts,
        live_bookings=live_bookings,
    )
    user = _user_block(message, session_note=note, history=history or [])
    call = generate or _live_generate

    try:
        raw = call(SYSTEM, user, model=model or config.CLAUDE_MODEL)
    except Exception:
        _log.warning("router classification failed", exc_info=True)
        return RouteDecision(FALLBACK, 0.0, FALLBACK, 0.0, failed=True)

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            _log.warning("router returned unparseable output: %r", raw)
            return RouteDecision(FALLBACK, 0.0, FALLBACK, 0.0, failed=True)

    agent = raw.get("agent")
    if agent not in AGENTS:
        _log.warning("router returned unknown agent: %r", agent)
        return RouteDecision(FALLBACK, 0.0, FALLBACK, 0.0, failed=True)

    def _num(key: str) -> float:
        try:
            return max(0.0, min(1.0, float(raw.get(key, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    second = raw.get("second_best")
    return RouteDecision(
        agent=agent,
        confidence=_num("confidence"),
        second_best=second if second in AGENTS else FALLBACK,
        margin=_num("margin"),
    )
