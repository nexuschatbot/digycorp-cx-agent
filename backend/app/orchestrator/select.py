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

THE SCORES ARE NOT THE SAME ON EVERY CPU (found 2026-09-01, the first CI run
that executed the backend suite). The model is int8-quantised and quantised
kernels differ by architecture: "I want to buy it, whats next" misroutes to
sales at margin 0.0163 on the macOS arm64 machine that calibrated the 0.02 —
safely declined — and at 0.0209 on the Linux x86_64 runner, CONFIDENTLY
ROUTED. Production is Linux. So the margin the threshold must clear is the
worst wrong margin on the platform that serves customers, plus headroom for
the same drift, which is what 0.03 buys: locally the fast lane keeps 7 of its
13 correct routes, every wrong case sits at least 0.009 under the line on
both platforms measured, and everything it now declines goes to the judge —
a decline is latency, a confident misroute is a wrong specialist answering a
customer.

NO LIVE CALL IS EVER MADE FROM THE FAST LANE. Embedding is local. The judge is
the only thing here that reaches the network, and it is skipped entirely when
the fast lane is confident or the model is unavailable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("app.orchestrator.select")

# Every card that CAN be routed to. Aisha is `routable: false` — nothing routes
# to her, she is the one writing the briefs.
#
# THE FULL ROSTER, NOT THE LIVE ONE. `routable()` below is what selection reads;
# this is the declaration it filters. They are separate because the two answer
# different questions — "which specialists exist" is a fact about the cards,
# "which are taking work today" is a fact about the configuration — and the
# eval harnesses, the calibration and the differential check all want the first.
ROUTABLE: tuple[str, ...] = ("sales", "testdrive", "finance")

# Cards that only exist because we take test drives. Removed from the live
# roster by config.TEST_DRIVE_ENABLED; the card file stays on disk, versioned
# and gate-checked, so turning the flag back on needs no prompt change.
TEST_DRIVE_AGENTS: frozenset[str] = frozenset({"testdrive"})


def routable() -> tuple[str, ...]:
    """The specialists taking work right now.

    READ ON EVERY SELECTION rather than frozen at import, so the flag can be
    flipped and the process restarted without a deploy — the same contract
    PROMPT_VERSIONS and ROUTER_ENABLED hold. It is three membership tests
    against a three-element tuple, which is nothing beside the embedding.

    NOTE FOR CALLERS THAT CACHE: `cards()` and `_vectors()` below are keyed on
    this answer implicitly, so a flag change inside a live process must be
    followed by `reset()`. Production never does that; tests do.
    """
    from app import config

    if config.TEST_DRIVE_ENABLED:
        return ROUTABLE
    return tuple(a for a in ROUTABLE if a not in TEST_DRIVE_AGENTS)


# THE CALIBRATED RULE. See the module docstring: the winner must beat the
# runner-up by this much. Measured on 32 card utterances, leave-one-out —
# 0.02 was where the last misroute disappeared on the calibrating machine;
# 0.03 is that number with cross-platform headroom, because the same case
# scored 0.0163 on macOS and 0.0209 on the Linux CI runner (see the module
# docstring — production is Linux).
MIN_MARGIN = 0.03

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
        _cards = {a: _card_text(a) for a in routable()}
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


_ENTITY_RE = re.compile(r"\b([a-z]+(?:_[a-z]+)*_(?:id|ref|aed))\b")


def entity_owner(brief: str) -> Selection | None:
    """The one card that `produces` an entity the brief asks for by name.

    WHY (2026-09-02, seen live). Aisha briefs for an id she needs — "What is
    the vehicle_id for the Jetour Dashing? I need it to look up slots" — and
    the Haiku judge answers "none": to it the request is a lookup that fits no
    specialist's job description. Six turns on the test VM and a booking in a
    local run ended in an apology that way, with the cascade ceiling reached
    on a question Sales answers in one round. Retrieval-first makes this more
    common, not less: when the early questions are answered from passages, no
    specialist has run yet and no vehicle_id has been harvested, so the first
    booking brief of the conversation is exactly this shape.

    The cards already say who owns which entity (`produces` in their
    frontmatter — the same declaration order.resolve plans by). A brief that
    names an entity exactly one routable card produces goes to that card, with
    no judge and no embedding margin involved. Names that nobody produces, or
    that two cards produce, change nothing and fall through to the judge.
    """
    from app.orchestrator.order import produces

    wanted = set(_ENTITY_RE.findall((brief or "").lower()))
    if not wanted:
        return None
    owners: set[str] = set()
    for agent in routable():
        try:
            if wanted & produces(agent):
                owners.add(agent)
        except Exception:  # noqa: BLE001 — a missing card is the judge's problem
            continue
    if len(owners) != 1:
        return None
    (agent,) = owners
    return Selection(agent=agent, layer="fast_lane", reason=f"produces {', '.join(sorted(wanted & produces(agent)))}")


# The gap a verb and its object may sit apart: "create a booking",
# "create a test drive booking". Three words covers every shape Aisha has
# written live; wider starts joining unrelated clauses.
_ACTION_GAP = 3


def _action_pattern(action: str) -> re.Pattern | None:
    """A regex for one proposal name, built FROM the name — never hand-written.

    `create_booking` becomes create[_ a test drive ]booking, tolerating the
    underscore spelling, an article or two, and the `_id`/`_ref` suffix the
    object usually wears ("lock slot_id 1392").
    """
    verb, _, noun = action.partition("_")
    if not verb or not noun:
        return None
    return re.compile(
        rf"\b{verb}(?:_|\W+(?:\w+\W+){{0,{_ACTION_GAP}}}){noun}(?:_(?:id|ref))?\b",
        re.IGNORECASE,
    )


def action_owners(brief: str) -> dict[str, set[str]]:
    """{action: cards that may perform it} for every write this brief names.

    FACTORED OUT OF action_owner (2026-09-04) rather than re-implemented, so
    routing and recovery can never disagree about what a brief asked for.
    action_owner answers "who"; subagent._derived_proposals needs "what", and a
    second matcher drifting from this one would put the two on different
    answers for the same sentence.
    """
    text = (brief or "").strip()
    if not text:
        return {}
    from app.agents import loader
    from app.orchestrator.write import _ACTION_TO_TOOL, PROPOSABLE

    owners: dict[str, set[str]] = {}
    for agent in routable():
        try:
            declared = list(loader.frontmatter(agent).get("proposes") or [])
        except Exception:  # noqa: BLE001 — a missing card is the judge's problem
            continue
        for action in declared:
            if action not in PROPOSABLE:
                continue
            # Both spellings of the same act: the card says `hold_slot`, the
            # customer's brief may well say "lock slot 1392".
            for spelling in {action, _ACTION_TO_TOOL.get(action, action)}:
                pattern = _action_pattern(spelling)
                if pattern is not None and pattern.search(text):
                    owners.setdefault(action, set()).add(agent)
    return owners


def action_owner(brief: str) -> Selection | None:
    """The one card that may PERFORM the write this brief asks for.

    FOUND LIVE 2026-09-03, and it is why a confirmed booking spent eight
    seconds in the wrong specialist. Aisha wrote "Create a booking for Ali
    Test ... (vehicle_id 3) ... slot ID 1147" — `slot ID` spelled with a
    space, so `entity_owner` below matched only `vehicle_id`, which Sales
    produces, and sent a booking request to Sales. Sales has no write to
    propose, answered in prose, and Aisha had to brief again.

    ASKING FOR A WRITE IS THE STRONGEST SIGNAL A BRIEF CARRIES, so this runs
    FIRST — ahead of the embedding lane and ahead of entity_owner. The cards
    declare what they may ask for (`proposes`), write.py declares what it can
    actually perform (PROPOSABLE), and exactly one routable card may do any of
    it; a car named in passing is the object of the sentence, not its verb.
    Two owners, or none, change nothing and fall through as before.
    """
    owners = action_owners(brief)
    if not owners:
        return None
    asked = {a for a, who in owners.items() if len(who) == 1}
    if not asked:
        return None
    agents = {next(iter(owners[a])) for a in asked}
    if len(agents) != 1:
        return None
    (agent,) = agents
    return Selection(
        agent=agent, layer="fast_lane", reason=f"proposes {', '.join(sorted(asked))}"
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

    # A WRITE REQUEST OUTRANKS BOTH LAYERS BELOW — see action_owner.
    doer = action_owner(brief)
    if doer is not None:
        return doer

    hit = fast_lane(brief)
    if hit is not None:
        return hit

    owner = entity_owner(brief)
    if owner is not None:
        return owner

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
        chosen = judge(brief, list(routable()))
    except Exception:  # noqa: BLE001 — a dead judge must not kill the turn
        log.exception("routing judge failed")
        chosen = None

    if chosen not in routable():
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
