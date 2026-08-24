"""Assembling the split blocks back into a prompt body.

TWO ORDERS, DELIBERATELY.

`SINGLE_AGENT_ORDER` is the sequence the blocks appeared in inside the original
`_PROMPT_BODY` — base and journey sections interleaved, exactly as written. It is
what `prompt.system_prompt()` uses today, which is why the split changes nothing:
tests/test_prompt_split.py asserts the reassembly is byte-identical to the body
that shipped. Until the router exists (S7) this is the ONLY order used in
production.

`JOURNEY_ORDER` is the multi-agent shape: every BASE block in its original
relative order, then that agent's own blocks in theirs. Interleaving within each
group is preserved, so a section never moves relative to its neighbours of the
same kind — the only thing that changes is that the other journey's sections are
absent. That is the whole point of the split: a shorter sheet per agent.
"""

from __future__ import annotations

from app.agent.prompts import base, bookingmanagement, testdrive

AGENTS = ("testdrive", "bookingmanagement")

_JOURNEY_MODULES = {
    "testdrive": testdrive,
    "bookingmanagement": bookingmanagement,
}

# (module, CONSTANT) in the order they appear in the original literal.
SINGLE_AGENT_ORDER: tuple[tuple[str, str], ...] = (
    ("base", "PREAMBLE"),
    ("base", "WHO_YOU_ARE"),
    ("base", "GROUNDING"),
    ("testdrive", "SHOWROOMS_FIRST"),
    ("testdrive", "NEAREST_BRANCH"),
    ("testdrive", "ONE_QUESTION_SHOWROOM_CARD"),
    ("testdrive", "SLOT_ALTERNATIVES"),
    ("testdrive", "BRANCH_STICKINESS"),
    ("base", "BUTTONS"),
    ("testdrive", "BROCHURE"),
    ("testdrive", "COMPARE"),
    ("testdrive", "MODEL_LOOKUP"),
    ("testdrive", "DOORSTEP"),
    ("testdrive", "SECOND_DRIVE_ORDERING"),
    ("base", "LANGUAGE"),
    ("base", "HOW_YOU_TALK"),
    ("base", "TWICE_IS_THE_LIMIT"),
    ("base", "YOUR_VOICE"),
    ("base", "OUTPUT_FORMAT"),
    ("testdrive", "FINDING_THE_RIGHT_CAR"),
    ("testdrive", "MAKING_THE_VISIT_THEIRS"),
    ("base", "WHAT_WE_REMEMBER"),
    ("testdrive", "BOOKING_SEQUENCE"),
    ("bookingmanagement", "MANAGING_EXISTING"),
    ("bookingmanagement", "RESCHEDULING"),
    ("bookingmanagement", "CANCELLING"),
    ("base", "BRINGING_IN_A_PERSON"),
    ("base", "HOLDING_THE_LINE"),
    ("base", "CLOSING"),
)


def _text(module_name: str, const: str) -> str:
    module = base if module_name == "base" else _JOURNEY_MODULES[module_name]
    return getattr(module, const)


def single_agent_body() -> str:
    """The original body, reassembled. Byte-identical to what shipped."""
    return "".join(_text(m, c) for m, c in SINGLE_AGENT_ORDER)


def JOURNEY_ORDER(agent: str) -> tuple[tuple[str, str], ...]:
    """BASE blocks in original order, then `agent`'s blocks in original order."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    keep = {"base", agent}
    ordered = [(m, c) for m, c in SINGLE_AGENT_ORDER if m in keep]
    return tuple(
        [(m, c) for m, c in ordered if m == "base"]
        + [(m, c) for m, c in ordered if m == agent]
    )


def body_for(agent: str) -> str:
    """The prompt body for one journey agent."""
    return "".join(_text(m, c) for m, c in JOURNEY_ORDER(agent))
