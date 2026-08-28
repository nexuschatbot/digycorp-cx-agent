"""What runs before what (T3.2).

AISHA PROPOSES, THE ORCHESTRATOR VALIDATES. She writes briefs with `depends_on`
because she is the one who read the sentence and knows that "and book a test
drive for it" means *it* — the car from the first half. But she does not know the
roster and cannot know that TestDrive needs a `vehicle_id` before it can do
anything, so her ordering is a HINT, checked here against what the cards declare.

THE CHECK IS `requires` / `produces`, DECLARED IN EACH CARD:

    sales      requires []                produces vehicle_id, variant_id
    testdrive  requires [vehicle_id]      produces slot_id, booking_ref
    finance    requires [variant_id]      produces emi_estimate, on_road_price

So "book a test drive for the X70" cannot run TestDrive first, whatever Aisha
said — and if she said otherwise, that disagreement is LOGGED rather than
silently corrected, because a pattern of them means her prompt needs work and
nobody would ever find out.

THREE OUTCOMES, NOT TWO:

    satisfied   every requirement is met by an earlier step. Run in this order.
    reordered   Aisha's order was wrong but a valid one exists. Run the fixed
                order, log the disagreement.
    blocked     no order can satisfy the requirements, because nothing in this
                turn produces what a step needs. The customer has to be asked —
                "which car?" — and that is Aisha's job, not a guess here.

SHOWROOM IS NOT A REQUIREMENT, deliberately, and this is the trap the plan calls
out by name. TestDrive needs a `vehicle_id`; it does NOT need a showroom, because
nothing reliably produces one and a precondition nothing produces deadlocks the
ordering forever. The customer's preferred branch is a CONSTRAINT that narrows a
tool call, never a gate on whether the agent runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("app.orchestrator.order")

# Entities that exist before any agent runs, because Aisha established them in
# conversation. A brief may depend on these without a producer.
AMBIENT: frozenset[str] = frozenset({"customer_id", "session_id"})


@dataclass
class Step:
    """One brief, and the agent chosen for it."""

    brief: str
    agent: str
    depends_on: tuple[int, ...] = ()   # indices into the ORIGINAL brief list
    index: int = 0


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    blocked: list[tuple[Step, list[str]]] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    # What already existed before this turn. CARRIED ON THE PLAN because
    # independent_groups needs it too: without it, a follow-up whose vehicle_id
    # came from the session looks unrunnable in the first wave and gets
    # needlessly serialised behind a step it does not depend on.
    known: frozenset[str] = frozenset()

    @property
    def ok(self) -> bool:
        return not self.blocked


def _card(agent: str) -> dict:
    from app.agents import loader

    try:
        return loader.frontmatter(agent) or {}
    except Exception:  # noqa: BLE001
        return {}


def requires(agent: str) -> set[str]:
    return set(_card(agent).get("requires") or [])


def produces(agent: str) -> set[str]:
    return set(_card(agent).get("produces") or [])


def resolve(steps: list[Step], *, known: set[str] | None = None) -> Plan:
    """Order the steps so every requirement is met before it is needed.

    A STABLE TOPOLOGICAL SORT, not a clever one. Steps are taken in Aisha's
    order whenever they are runnable, so an ordering she got right is preserved
    exactly — the reordering only ever moves a step that could not have run.

    `known` is what already exists before this turn: entities the session
    established earlier. A follow-up "book it for Saturday" carries a
    vehicle_id from the session, so TestDrive runs alone rather than being told
    it needs a Sales step that the customer never asked for.
    """
    plan = Plan(known=frozenset(known or ()))
    available: set[str] = set(AMBIENT) | set(known or ())
    remaining = list(steps)

    # Honour Aisha's declared dependencies first: a step whose named predecessor
    # has not run yet is not runnable, even if its `requires` happen to be met.
    done: set[int] = set()

    while remaining:
        runnable = [
            s for s in remaining
            if requires(s.agent) <= available and set(s.depends_on) <= done
        ]
        if not runnable:
            # Try ignoring Aisha's depends_on — if that frees something, her
            # ordering was the problem and it is worth recording as hers.
            loosened = [s for s in remaining if requires(s.agent) <= available]
            if loosened:
                s = loosened[0]
                plan.disagreements.append(
                    f"{s.agent} was told to wait for step(s) {list(s.depends_on)}, "
                    f"but its requirements were already met — ran it earlier"
                )
                runnable = [s]
            else:
                # Genuinely unsatisfiable: nothing left can run.
                for s in remaining:
                    plan.blocked.append((s, sorted(requires(s.agent) - available)))
                break

        chosen = runnable[0]
        remaining.remove(chosen)
        done.add(chosen.index)
        available |= produces(chosen.agent)
        plan.steps.append(chosen)

    if plan.disagreements:
        # LOGGED, NOT SWALLOWED. One disagreement is noise; a pattern of them
        # means Aisha's briefing prompt needs work, and this is the only place
        # that would ever notice.
        for line in plan.disagreements:
            log.info("ordering disagreement: %s", line)
    return plan


def independent_groups(plan: Plan) -> list[list[Step]]:
    """Steps that can run AT THE SAME TIME, in dependency waves.

    This is what run.py fans out with. Two briefs that need nothing from each
    other — "how much is the X70" and "where is your Deira branch" — should not
    be serialised just because Aisha listed one first.
    """
    waves: list[list[Step]] = []
    available: set[str] = set(AMBIENT) | set(plan.known)
    pending = list(plan.steps)
    done: set[int] = set()

    while pending:
        wave = [
            s for s in pending
            if requires(s.agent) <= available and set(s.depends_on) <= done
        ]
        if not wave:
            wave = [pending[0]]  # cannot happen after resolve(); belt and braces
        waves.append(wave)
        for s in wave:
            pending.remove(s)
            done.add(s.index)
        for s in wave:
            available |= produces(s.agent)
    return waves
