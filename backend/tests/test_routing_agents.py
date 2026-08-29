"""The four-agent routing eval as a CI gate (T7.3).

WHAT THIS ASSERTS, AND WHY IT IS NOT ACCURACY. The embedding fast lane gets
71.9% of the card utterances right, which sounds like a failing grade and is not
the number that matters. The fast lane is not the router — it is a cheap
shortcut that only claims a brief when it is confident, and the Haiku judge
decides everything else.

So the invariant is: EVERY CASE IT ROUTES CONFIDENTLY IS CORRECT. Measured on
the cards' own positives and negatives, every single failure sits below
MIN_MARGIN — the worst is 0.0163 against a threshold of 0.02 — so the fast lane
declines all of them. That is the calibration working, not failing.

An accuracy target here would be the wrong gate twice over: it would fail on
cases the system already handles correctly via the judge, and it would tempt
somebody to lower MIN_MARGIN to make the number look better, which is precisely
the change that would start routing them wrongly.

Offline: local embeddings, no judge, no network.
"""

from __future__ import annotations

import pytest

from app.orchestrator import select
from app.retrieval import embed
from evals import routing_agents

needs_model = pytest.mark.skipif(
    not embed.available(), reason="embedding model not vendored in this checkout"
)


@pytest.fixture(scope="module")
def result():
    select.reset()
    return routing_agents.evaluate()


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
@needs_model
def test_the_fast_lane_is_never_confidently_wrong(result):
    """THE ONE THAT MATTERS. A case the fast lane routes is a case the judge
    never sees, so a confident mistake reaches a customer. Every failure must be
    one it declined."""
    confident_and_wrong = [
        (case.utterance, case.agent, got, margin)
        for case, got, margin in result["failures"]
        if margin >= select.MIN_MARGIN
    ]
    assert confident_and_wrong == [], confident_and_wrong


@needs_model
def test_the_fast_lane_still_earns_its_place(result):
    """If it settled nothing, it would be latency and code for no benefit — and
    the honest move would be to delete it and always ask the judge."""
    assert result["fast_lane_correct"] >= 8


@needs_model
def test_every_card_negative_is_asserted(result):
    """THE PLAN'S RULE: "every `negative` becomes an assertion". They are the
    cases that separate nearly identical wordings — "how much is the X70" versus
    "what's the on-road price with insurance" — which no domain paragraph does.
    """
    _, negatives_total = result["negatives"]
    assert negatives_total == 12, "all three cards' negatives must be evaluated"


@needs_model
def test_the_cases_come_from_the_cards_not_a_copy():
    """A duplicated list is two lists that drift, and the one that drifts is the
    one nobody is reading."""
    from app.agents import loader

    declared = set()
    for agent in ("sales", "testdrive", "finance"):
        fm = loader.frontmatter(agent)
        declared |= set(fm.get("utterances") or []) | set(fm.get("negative") or [])
    assert {c.utterance for c in routing_agents.cases()} == declared


# --------------------------------------------------------------------------- #
# The write invariants the plan states twice and nothing enforced
# --------------------------------------------------------------------------- #
def test_no_sub_agent_holds_a_write_tool():
    """The single property that makes Haiku safe for them: a bad generation can
    ASK for a booking, it cannot make one."""
    from evals import differential

    assert differential.write_tool_violations() == []


def test_aisha_holds_no_commercial_write():
    """The split is by KIND, not by layer. She keeps identity, consent, licence,
    memory and close because those are acts of dialogue and only she talks.
    Holding and booking are acts of commerce."""
    from evals import differential

    assert differential.commercial_write_violations() == []


def test_every_commercial_action_a_card_proposes_can_actually_be_executed():
    """A card proposing something write.py does not know about is an instruction
    the sub-agent will follow and nothing will honour — the orphaned-rule defect
    at the level of actions rather than tools."""
    from evals import differential

    assert differential.proposal_coverage() == []


def test_the_write_report_is_clean():
    from evals import differential

    assert differential.write_report() == []
