"""Routing cases for the FOUR-agent roster, built from the cards (T7.3).

WHY THIS IS A SEPARATE FILE FROM routing_cases.py. That one holds the labelled
cases for the SHIPPED two-journey router (testdrive / bookingmanagement /
fallback) and is still the gate for it — the single-agent path is live and stays
live until MULTI_AGENT_ENABLED is flipped. Rewriting it in place would have
retired a working eval to make room for one guarding a switched-off path.

THE CASES ARE READ FROM THE CARDS, NOT COPIED FROM THEM. Every `utterance` on a
card is a positive for that agent; every `negative` is an assertion that the
utterance must NOT score highest there. That is the plan's rule — "every
`negative` becomes an assertion" — and reading them rather than duplicating them
means a card edit updates the eval automatically, instead of leaving two lists
to drift.

WHAT A FAILURE HERE MEANS, AND WHAT IT DOES NOT. This measures the EMBEDDING
fast lane alone, offline. It is not the router: the Haiku judge decides most
briefs and cannot be exercised without a live call. So a case failing here says
"the cheap path cannot settle this one", which is information, not an alarm —
the calibrated fast lane only claims about a third of traffic in the first
place, and the rest is the judge's by design.

    python evals/routing_agents.py
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCase:
    utterance: str
    agent: str
    kind: str          # "positive" | "negative"
    note: str = ""


def cases() -> list[AgentCase]:
    """Every positive and negative the three cards declare."""
    from app.agents import loader

    out: list[AgentCase] = []
    for agent in ("sales", "testdrive", "finance"):
        fm = loader.frontmatter(agent) or {}
        for utterance in fm.get("utterances") or []:
            out.append(AgentCase(utterance, agent, "positive"))
        for utterance in fm.get("negative") or []:
            out.append(
                AgentCase(
                    utterance, agent, "negative",
                    note="must not score highest on this card",
                )
            )
    return out


def evaluate() -> dict:
    """Score every case against the fast lane. Offline; no judge, no network."""
    from app.orchestrator import select

    positives_right = positives_total = 0
    negatives_right = negatives_total = 0
    failures: list[tuple[AgentCase, str, float]] = []

    for case in cases():
        ranked = select.score_brief(case.utterance)
        if not ranked:
            continue
        top, top_score = ranked[0]
        margin = top_score - ranked[1][1] if len(ranked) > 1 else 0.0

        if case.kind == "positive":
            positives_total += 1
            if top == case.agent:
                positives_right += 1
            else:
                failures.append((case, top, margin))
        else:
            negatives_total += 1
            # THE ASSERTION THE PLAN ASKED FOR: a negative must not win its own
            # card. It is allowed to win somebody else's — that is the point of
            # it being a negative rather than nonsense.
            if top != case.agent:
                negatives_right += 1
            else:
                failures.append((case, top, margin))

    routed = [
        c for c in cases()
        if (h := select.fast_lane(c.utterance)) is not None and c.kind == "positive"
        and h.agent == c.agent
    ]
    return {
        "positives": (positives_right, positives_total),
        "negatives": (negatives_right, negatives_total),
        "fast_lane_correct": len(routed),
        "failures": failures,
    }


def main() -> None:
    result = evaluate()
    pr, pt = result["positives"]
    nr, nt = result["negatives"]
    print(f"positives  {pr}/{pt}  ({pr / pt:.1%})" if pt else "no positives")
    print(f"negatives  {nr}/{nt}  ({nr / nt:.1%})" if nt else "no negatives")
    print(f"fast lane routes {result['fast_lane_correct']} correctly and confidently")
    if result["failures"]:
        print("\nthe fast lane could not settle these (the judge's job):")
        for case, got, margin in result["failures"]:
            print(f"  [{case.kind:8}] {case.utterance[:46]!r:50} want {case.agent:10} got {got:10} margin {margin:.4f}")


if __name__ == "__main__":
    main()
