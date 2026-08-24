"""Which tools each journey agent may call (S4 mapping, enforced).

TODAY THIS IS DATA, NOT YET A BINDING. The single-agent path still hands the
model the whole registry; S7 is where `bind()` starts feeding the agent node.
Landing the map now is deliberate — evals/differential.py checks every scenario
against it, so a tool that a journey needs but is not assigned fails in CI rather
than at runtime when the router first sends a customer down that path.

THE SPLIT IS BY TOOL OWNERSHIP, NOT TOPIC (architecture doc §6.2). A tool sits in
SHARED only when both journeys genuinely need it — identity, memory, escalation,
presentation — never to avoid a decision.

WHY THE SLOT TOOLS ARE SHARED. `get_available_slots` and `lock_slot` look like
TestDrive's alone until you read reschedule_booking's own description: "call
get_available_slots, let them choose, and lock_slot the one they pick." Moving a
booking is re-picking a slot, so BookingManagement needs both. Binding them to
TestDrive only would break rescheduling the first time a customer used it.
"""

from __future__ import annotations

from app.tools import REGISTRY

AGENTS = ("testdrive", "bookingmanagement")

# Identity, memory, escalation, presentation and the terminal action: every
# journey needs these, and they own no journey of their own.
SHARED: frozenset[str] = frozenset({
    "find_or_create_customer",
    "identify_by_reference_or_mobile",
    "confirm_returning_customer",
    "save_memory",
    "forget_me",
    "request_human_callback",
    "close_conversation",
    "offer_options",
    # Slot picking — see the module docstring.
    "get_available_slots",
    "lock_slot",
    # A READ, and SHARED because _BOOKINGS_ANCHOR — which is appended to every
    # agent's prompt — instructs it directly: "whenever they ask to see a
    # booking, ask anything about its details, or want to move or cancel one,
    # call get_booking for it first". An agent that gets that sentence must be
    # able to obey it. Caught by evals/differential.py, not by the suite.
    "get_booking",
})

JOURNEY: dict[str, frozenset[str]] = {
    "testdrive": frozenset({
        "search_vehicles",
        "get_vehicle_details",
        "compare_vehicles",
        "nearest_showroom",
        # NOT shared. Rule 10 allows moving branch, but no BookingManagement
        # block guides branch selection, and writing that prose would be
        # re-authoring. If moving branch turns out to matter, port the guidance
        # first, then widen this — in that order.
        "get_showrooms",
        "send_brochure",
        "acknowledge_licence_reminder",
        "create_booking",
        "record_consent",
    }),
    "bookingmanagement": frozenset({
        "reschedule_booking",
        "cancel_booking",
    }),
}


def tools_for(agent: str) -> frozenset[str]:
    """Every tool `agent` is allowed to call."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    return SHARED | JOURNEY[agent]


def schemas_for(agent: str) -> list[dict]:
    """Anthropic `tools=` payload for one agent, registry order preserved."""
    allowed = tools_for(agent)
    return [e["schema"] for n, e in REGISTRY.items() if n in allowed]


def unassigned() -> set[str]:
    """Registry tools no agent can reach. Must always be empty."""
    assigned = SHARED.union(*JOURNEY.values())
    return set(REGISTRY) - assigned


def unknown() -> set[str]:
    """Assigned names that are not in the registry. Must always be empty."""
    assigned = SHARED.union(*JOURNEY.values())
    return assigned - set(REGISTRY)
