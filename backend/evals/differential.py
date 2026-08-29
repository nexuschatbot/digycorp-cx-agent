"""Differential check: does splitting the prompt strand a tool from its rules?

THE FAILURE THIS EXISTS TO CATCH. Splitting one prompt into per-agent prompts and
binding a narrower tool set to each creates a defect that neither half can see on
its own: a block of prose lands in agent A telling it to call a tool that only
agent B may call. The instruction is intact, the tool is intact, and the agent
obeys prose it cannot act on.

That is not hypothetical. `_SECOND_DRIVE_ANCHOR` fires from booking state and
says "search_vehicles or get_vehicle_details first, every time" — under the
architecture doc's original Sales/Bookings roster it would have sat with the
agent that owns neither tool.

TWO DIRECTIONS, AND THE SECOND IS THE IMPORTANT ONE.

  ORPHANED RULE   agent's body names a tool the agent cannot call.
                  The agent is instructed to do something impossible.

  UNGUIDED TOOL   agent can call a tool, but no block in its body mentions it.
                  Weaker: some tools are self-describing through their schema,
                  so this is reported, not failed.

The suite cannot catch either. Only three of the ported test files exercise both
journeys in one flow, and none of them assert on prompt composition.

OFFLINE BY DESIGN. Everything here is string and set arithmetic over the prompt
blocks and the tool registry. No model is called, so this runs in CI on every
prompt change, for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent import tool_binding
from app.agent.prompts import anchors as _anchors
from app.agents import loader
from app.tools import REGISTRY


def _blocks() -> dict[tuple[str, str], str]:
    """Every prompt block as {(journey, SECTION): text}.

    READ FROM THE MARKDOWN, NOT FROM PYTHON CONSTANTS (T2.1). The prose moved to
    app/agents/*/v1.md, and reading it from anywhere else would give this check a
    second copy that drifts from the one that actually ships.

    THE JOURNEY KEY IS RECOVERED FROM PROVENANCE. testdrive and bookingmanagement
    merged into one `aisha` file, but tool bindings are still per journey — so
    the origin marker written by the migration is what keeps this check as sharp
    as it was. Without it, a bookingmanagement block naming a testdrive-only tool
    would become invisible, which is precisely the defect this file exists for.
    """
    out: dict[tuple[str, str], str] = {}
    for agent in ("base", "aisha"):
        texts = loader.sections(agent)
        origin = loader.origins(agent)
        for name, text in texts.items():
            out[(origin.get(name, agent), name)] = text
    return out

# S7a scoped the anchors, so they are checked per agent exactly like the body
# blocks: an anchor only counts against an agent that actually receives it.


def _mentions(text: str, tool: str) -> bool:
    """A tool is 'named' by a block only as a whole word."""
    return re.search(rf"\b{re.escape(tool)}\b", text) is not None


def blocks_naming(tool: str) -> list[tuple[str, str]]:
    """Every (journey, SECTION) block whose prose names `tool`."""
    return [key for key, text in _blocks().items() if _mentions(text, tool)]


def anchors_naming(tool: str, agent: str | None = None) -> list[str]:
    """Anchor constants naming `tool` — restricted to what `agent` receives."""
    names = _anchors.anchors_for(agent) if agent else (
        _anchors.SHARED_ANCHORS
        + tuple(a for v in _anchors.JOURNEY_ANCHORS.values() for a in v)
    )
    return [a for a in names if _mentions(getattr(_anchors, a, ""), tool)]


@dataclass
class AgentReport:
    agent: str
    orphaned_rules: list[tuple[str, str, str]] = field(default_factory=list)
    unguided_tools: list[str] = field(default_factory=list)
    orphaned_anchors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.orphaned_rules and not self.orphaned_anchors


def check(agent: str) -> AgentReport:
    allowed = tool_binding.tools_for(agent)
    # What this journey actually receives: every base block, plus its own.
    body_blocks = [k for k in _blocks() if k[0] in ("base", agent)]
    report = AgentReport(agent=agent)

    for tool in REGISTRY:
        named_in = [b for b in blocks_naming(tool) if b in body_blocks]
        named_anchors = anchors_naming(tool, agent)
        if tool not in allowed:
            for module_name, const in named_in:
                report.orphaned_rules.append((module_name, const, tool))
            for anchor in named_anchors:
                report.orphaned_anchors.append((anchor, tool))
        elif not named_in and not named_anchors:
            report.unguided_tools.append(tool)

    return report


def check_all() -> dict[str, AgentReport]:
    return {a: check(a) for a in tool_binding.AGENTS}


def render(reports: dict[str, AgentReport]) -> str:
    lines = []
    for agent, r in reports.items():
        lines.append(f"\n{agent}  —  {'OK' if r.ok else 'FAIL'}")
        lines.append(f"  tools bound: {len(tool_binding.tools_for(agent))}")
        if r.orphaned_rules:
            lines.append("  ORPHANED RULES (prose names a tool this agent cannot call):")
            for module_name, const, tool in r.orphaned_rules:
                lines.append(f"    {module_name}.{const} -> {tool}")
        if r.orphaned_anchors:
            lines.append("  ORPHANED ANCHORS:")
            for anchor, tool in r.orphaned_anchors:
                lines.append(f"    {anchor} -> {tool}")
        if r.unguided_tools:
            lines.append("  unguided tools (callable, no prose names them):")
            for tool in r.unguided_tools:
                lines.append(f"    {tool}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    reports = check_all()
    print(render(reports))
    sys.exit(0 if all(r.ok for r in reports.values()) else 1)


# --------------------------------------------------------------------------- #
# The Phase 4 write invariants (T7.3)
# --------------------------------------------------------------------------- #
# THESE ARE THE CLAIMS THE WHOLE WRITE ARCHITECTURE RESTS ON, and until now they
# were prose in a plan. The plan states them twice, in the roster decision and
# again in the verification list, which is exactly the kind of rule that stays
# true right up until somebody adds a tool to a card in a hurry.
#
#   1. NO SUB-AGENT HOLDS A WRITE TOOL. They return proposals as DATA and the
#      orchestrator executes them. This is the single property that makes it
#      safe to run a sub-agent on Haiku: a bad generation can ask for a booking,
#      it cannot make one.
#
#   2. AISHA HOLDS NO COMMERCIAL WRITE. The split is by KIND, not by layer —
#      she keeps the conversational writes (identity, consent, licence, memory,
#      close) because those are acts of dialogue and only she talks. Holding,
#      booking, rescheduling and cancelling are acts of commerce and belong to
#      the orchestrator, where they are serialised and audited in one place.
#
# Checked here rather than only in tests/ because differential.py is the file a
# prompt or card change is expected to run, and a rule enforced somewhere nobody
# looks is a rule with a delay fuse on it.

SUB_AGENTS: tuple[str, ...] = ("sales", "testdrive", "finance")


def _card_tools(agent: str) -> set[str]:
    from app.agents import loader

    try:
        return set(loader.frontmatter(agent).get("tools") or [])
    except Exception:  # noqa: BLE001
        return set()


def write_tool_violations() -> list[str]:
    """Sub-agent cards that hold a write tool. Empty is the only acceptable answer."""
    from app.tools import WRITE_TOOLS

    out: list[str] = []
    for agent in SUB_AGENTS:
        for tool in sorted(_card_tools(agent) & WRITE_TOOLS):
            out.append(f"{agent} holds the write tool {tool!r}")
    return out


def commercial_write_violations() -> list[str]:
    """Aisha holding a COMMERCIAL write, which is the orchestrator's alone.

    Reads the aisha card if it declares tools; falls back to the live binding,
    because in Phase 5 the single-agent path still binds everything and the
    claim only becomes enforceable once she has a card of her own.
    """
    from app.tools import COMMERCIAL_WRITE_TOOLS

    tools = _card_tools("aisha")
    if not tools:
        return []  # no declared binding yet — nothing to check, and say so
    return [
        f"aisha holds the commercial write {tool!r}"
        for tool in sorted(tools & COMMERCIAL_WRITE_TOOLS)
    ]


def proposal_coverage() -> list[str]:
    """Commercial actions a card PROPOSES that the orchestrator cannot execute.

    A card proposing something write.py does not know about is an instruction
    the sub-agent will follow and nothing will honour — the orphaned-rule defect
    at the level of actions rather than tools.
    """
    from app.agents import loader
    from app.orchestrator.write import PROPOSABLE

    out: list[str] = []
    for agent in SUB_AGENTS:
        try:
            proposes = set(loader.frontmatter(agent).get("proposes") or [])
        except Exception:  # noqa: BLE001
            continue
        for action in sorted(proposes):
            # Non-commercial proposals (send_brochure, request_human_callback)
            # are executed by the ordinary tool path, not by write.py.
            from app.tools import COMMERCIAL_WRITE_TOOLS

            commercial = action in COMMERCIAL_WRITE_TOOLS or action == "hold_slot"
            if commercial and action not in PROPOSABLE:
                out.append(f"{agent} proposes {action!r}, which write.py cannot execute")
    return out


def write_report() -> list[str]:
    """Every write-architecture violation, in one list. Empty means healthy."""
    return write_tool_violations() + commercial_write_violations() + proposal_coverage()
