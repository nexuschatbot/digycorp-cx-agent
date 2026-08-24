"""Split prompt body (S5).

The single ~20k-token prompt is divided into a shared BASE and one block set per
journey agent. Nothing here is new prose — every constant is an exact slice of
the original `_PROMPT_BODY`, and compose.SINGLE_AGENT_ORDER reassembles them in
the order they shipped in.
"""

from app.agent.prompts import base, bookingmanagement, testdrive
from app.agent.prompts.compose import (
    AGENTS,
    JOURNEY_ORDER,
    SINGLE_AGENT_ORDER,
    body_for,
    single_agent_body,
)

__all__ = [
    "AGENTS",
    "JOURNEY_ORDER",
    "SINGLE_AGENT_ORDER",
    "base",
    "body_for",
    "bookingmanagement",
    "single_agent_body",
    "testdrive",
]
