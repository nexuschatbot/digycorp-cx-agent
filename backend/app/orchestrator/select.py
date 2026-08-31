"""Which agent handles a brief (T3.1, calibrated at T3.6).

TWO LAYERS, AND THE FIRST ONE IS SMALL ON PURPOSE.

    fast lane   embed the brief against the agent cards. Route ONLY on a clear
                win. Free, instant, and covers about a third of traffic.
    judge       everything else goes to Haiku, which reads the brief and picks.
                ~1s and a few tokens, and it is the accurate path.

WHY THE FAST LANE IS NOT THE PRIMARY ROUTER, MEASURED RATHER THAN ASSUMED. The
plan proposed "score >= 0.80 route free, else judge". Calibrated against the
cards' own utterances with leave-one-out, that rule routes 56% of traffic and is
WRONG ON ONE IN SIX of what it keeps. The reason is visible in the numbers: the
three agent cards are 88% similar to EACH OTHER — they all describe car
dealership work in the same register — so almost no room is left to express
"choosing" vs "booking" vs "paying", and what remains is swamped by sentence
SHAPE. "which banks do you work with" routes to TestDrive, because anything
phrased "which ___ do you work with" looks like a showroom question; reworded to
"do you offer islamic finance" the same request routes to Finance correctly.

So an ABSOLUTE threshold cannot work here — the entire score range is ~0.09 wide
and a constant either admits everything or nothing. THE RULE IS A MARGIN: route
only when the winner beats the runner-up by MIN_MARGIN. That is a question about
this brief's own scores, not about a global scale, which is the only kind of
question a compressed embedding space can answer.

CALIBRATION, AND ITS LIMITS. At margin >= 0.02 the fast lane took 12 of 32
utterances and got all 12 right; at 0.015 it took 16 and got one wrong. So 0.02
is where the errors stop — ON TWELVE SAMPLES. That is not a guarantee, it is the
best available evidence, and it is why the number is a named constant with the
evidence beside it rather than a magic literal. Re-run evals/routing_calibration
.py when real briefs exist; raising it costs coverage and nothing else.

NO LIVE CALL IS EVER MADE FROM THE FAST LANE. Embedding is local. The judge is
the only thing here that reaches the network, and it is skipped entirely when
the fast lane is confident or the model is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("app.orchestrator.select")

# Cards that can be routed TO. Aisha is `routable: false` — nothing routes to
# her, she is the one writing the briefs.
ROUTABLE: tuple[str, ...] = ("sales", "testdrive", "finance")

# THE CALIBRATED RULE. See the module docstring: the winner must beat the
# runner-up by this much. Measured on 32 card utterances, leave-one-out —
# 0.02 is where the last misroute disappears, on twelve samples.
MIN_MARGIN = 0.02

# A floor as well, so a brief that resembles NOTHING cannot win by a margin
# between two equally poor scores. Deliberately low: it is a sanity bound, not
# the selection rule.
MIN_SCORE = 0.70

_cards: dict | None = None
_card_vectors = None


@dataclass(frozen=True)
class Selection:
    """One routing decision, with enough detail for turn_trace to explain it."""

    agent: str | None
    layer: str          # "fast_lane" | "judge" | "unroutable"
    score: float | None = None
    margin: float | None = None
    runner_up: str | None = None
    reason: str = ""


def _card_text(agent: str) -> str:
    """What a card looks like to the embedder: its domain paragraph.

    UTTERANCES ARE DELIBERATELY NOT INCLUDED. Adding them was measured and made
    the fast lane WORSE — eleven short fragments dilute the one paragraph that
    actually describes the job. They earn their keep as the calibration set and
    as few-shot examples for the judge, not as part of the vector.
    """
    from app.agents import loader

    fm = loader.frontmatter(agent)
    return str(fm.get("domain") or "").strip()


def cards() -> dict[str, str]:
    """{agent: domain text} for every routable agent. Cached per process."""
    global _cards
    if _cards is None:
        _cards = {a: _card_text(a) for a in ROUTABLE}
    return _cards


def _vectors():
    """Embedded cards, or None when the model is unavailable. Cached per process.

    CACHED FOR THE SAME REASON `cards()` IS, and it was the expensive half that
    was not. Measured 2026-08-31: embedding the three card paragraphs took
    206-432ms and was ~85% of score_brief's cost — re-run on EVERY brief, on the
    orchestrating thread, before any specialist starts, for three paragraphs
    that cannot change inside a process. The query embedding beside it is 18-47ms
    and genuinely per-brief.

    Keyed on nothing because `cards()` is already the cache key: reset() drops
    both together, so a prompt-version change in-process re-embeds exactly as it
    re-reads.
    """
    global _card_vectors
    if _card_vectors is not None:
        return _card_vectors
    from app.retrieval import embed

    texts = list(cards().values())
    if not all(texts):
        return None
    # Not memoised on failure: embed returns None when the model is unavailable,
    # and caching that would keep the fast lane dead for the life of the process
    # even after the asset is fixed.
    _card_vectors = embed.embed_passages(texts)
    return _card_vectors


def score_brief(brief: str) -> list[tuple[str, float]]:
    """Every agent scored against one brief, best first. [] if unavailable."""
    from app.retrieval import embed

    if not brief or not brief.strip():
        return []
    vectors = _vectors()
    qv = embed.embed_query(brief.strip())
    if vectors is None or qv is None:
        return []
    try:
        import numpy as np

        scores = np.asarray(vectors) @ np.asarray(qv)
    except Exception:  # noqa: BLE001
        log.exception("brief scoring failed")
        return []
    names = list(cards())
    ranked = sorted(zip(names, (float(s) for s in scores)), key=lambda p: -p[1])
    return ranked


def fast_lane(brief: str) -> Selection | None:
    """A confident embedding decision, or None to defer to the judge."""
    ranked = score_brief(brief)
    if len(ranked) < 2:
        return None
    (best, best_score), (second, second_score) = ranked[0], ranked[1]
    margin = best_score - second_score
    if best_score < MIN_SCORE:
        return None
    if margin < MIN_MARGIN:
        # THE COMMON CASE, and it is not a failure. Two thirds of briefs land
        # here, which is exactly what the calibration predicted — the judge is
        # the primary router and this lane is the cheap shortcut.
        return None
    return Selection(
        agent=best, layer="fast_lane", score=best_score, margin=margin,
        runner_up=second,
        reason=f"beat {second} by {margin:.3f}",
    )


def select(brief: str, *, judge=None) -> Selection:
    """Pick the agent for one brief. Never raises.

    `judge` is injected so tests can drive the whole cascade without a network
    call — and so the caller decides whether a live call is acceptable at all.
    Omitted, the judge is skipped and an unconfident brief comes back
    `unroutable` rather than reaching the API by accident.
    """
    if not brief or not brief.strip():
        return Selection(agent=None, layer="unroutable", reason="empty brief")

    hit = fast_lane(brief)
    if hit is not None:
        return hit

    if judge is None:
        ranked = score_brief(brief)
        return Selection(
            agent=None,
            layer="unroutable",
            score=ranked[0][1] if ranked else None,
            margin=(ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else None,
            reason="no clear winner and no judge supplied",
        )

    try:
        chosen = judge(brief, list(ROUTABLE))
    except Exception:  # noqa: BLE001 — a dead judge must not kill the turn
        log.exception("routing judge failed")
        chosen = None

    if chosen not in ROUTABLE:
        # An unroutable brief is a real outcome, not an error: Aisha may have
        # written something no specialist owns, and the orchestrator answers it
        # herself rather than guessing an agent.
        return Selection(agent=None, layer="judge", reason=f"judge returned {chosen!r}")
    return Selection(agent=chosen, layer="judge", reason="judged")


def reset() -> None:
    """Drop the cached cards AND their vectors. Tests, and a prompt-version
    change in-process — the two must go together or the fast lane would score
    new card text against old vectors."""
    global _cards, _card_vectors
    _cards = None
    _card_vectors = None
