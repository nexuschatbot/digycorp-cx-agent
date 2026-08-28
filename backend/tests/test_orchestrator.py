"""The orchestration layer: select, order, dispatch, cascade (Phase 3).

EVERYTHING HERE RUNS OFFLINE. The Haiku judge is injected, never imported into
the path under test, so the whole cascade is exercised without a network call —
which is also why `select.select()` refuses to reach the API unless a caller
deliberately passes a judge in.

THE CALIBRATION IS PINNED, NOT ASSUMED. T3.6 measured the selection rule against
the cards' own utterances with leave-one-out; the numbers below are that
measurement, and a change that quietly moves them fails here rather than in
production.
"""

from __future__ import annotations

import pytest

from app.orchestrator import cascade, judge as judge_mod, order, run, select
from app.retrieval import embed

needs_model = pytest.mark.skipif(
    not embed.available(), reason="embedding model not vendored in this checkout"
)


@pytest.fixture(autouse=True)
def _clean():
    select.reset()
    yield
    select.reset()


def _judge(mapping: dict):
    """A stub standing in for Haiku. Returns None for anything unmapped."""
    return lambda brief, agents: mapping.get(brief)


# --------------------------------------------------------------------------- #
# select — the calibrated rule
# --------------------------------------------------------------------------- #
@needs_model
@pytest.mark.parametrize(
    "brief,expected",
    [
        ("what SUVs do you have", "sales"),
        ("book a test drive on Saturday", "testdrive"),
        ("do you offer islamic finance", "finance"),
        ("move my booking to Sunday", "testdrive"),
    ],
)
def test_the_fast_lane_routes_the_clear_cases(brief, expected):
    hit = select.fast_lane(brief)
    assert hit is not None, f"{brief!r} should be clear enough to route"
    assert hit.agent == expected
    assert hit.margin >= select.MIN_MARGIN


@needs_model
@pytest.mark.parametrize(
    "brief", ["which banks do you work with", "which showrooms do you work with"]
)
def test_the_fast_lane_declines_when_the_margin_is_thin(brief):
    """THE SHAPE THAT SET THE THRESHOLD. Anything phrased "which ___ do you work
    with" scores nearly the same on all three cards, because the embedding
    matches sentence FORM more strongly than topic — the three domains are ~88%
    similar to each other, leaving almost no room for the difference we care
    about.

    NOTE WHAT THIS TEST NO LONGER CLAIMS. It originally asserted that "which
    banks" ranked finance BELOW testdrive, which was true and was one of the
    eight measured misroutes. Sharpening the card domains at T3.6 fixed the
    ranking — finance now wins — but only by 0.0059, well under the 0.02 the
    calibration says is needed to trust it. So the invariant is not "it ranks
    wrongly", it is "it is not confident enough to route without the judge",
    and that is what is asserted.
    """
    ranked = select.score_brief(brief)
    margin = ranked[0][1] - ranked[1][1]
    assert margin < select.MIN_MARGIN, f"{brief!r} margin {margin:.4f}"
    assert select.fast_lane(brief) is None


@needs_model
def test_an_unconfident_brief_never_reaches_the_network_by_accident():
    """No judge supplied means no judge called. A routing decision must be an
    explicit choice to spend a live call, never a default."""
    result = select.select("which banks do you work with")
    assert result.agent is None
    assert result.layer == "unroutable"


@needs_model
def test_the_judge_decides_what_the_fast_lane_would_not():
    result = select.select(
        "which banks do you work with", judge=_judge({"which banks do you work with": "finance"})
    )
    assert result.agent == "finance"
    assert result.layer == "judge"


@needs_model
def test_a_judge_that_fails_leaves_the_brief_unroutable_not_broken():
    def boom(brief, agents):
        raise RuntimeError("haiku is down")

    result = select.select("which banks do you work with", judge=boom)
    assert result.agent is None and result.layer == "judge"


@needs_model
def test_a_judge_naming_an_unknown_agent_is_rejected():
    result = select.select("anything at all", judge=_judge({"anything at all": "concierge"}))
    assert result.agent is None


def test_an_empty_brief_is_unroutable_without_scoring_anything():
    assert select.select("").agent is None
    assert select.select("   ").agent is None


@needs_model
def test_the_card_vector_is_the_domain_not_the_utterances():
    """MEASURED: adding the eleven utterances to the card vector made the fast
    lane WORSE — short fragments dilute the one paragraph that describes the
    job. They earn their keep as calibration data and judge few-shot, not here.
    """
    for agent in select.ROUTABLE:
        text = select._card_text(agent)
        assert text and "Keywords:" in text
        assert "book a test drive on Saturday" not in text


# --------------------------------------------------------------------------- #
# order — Aisha proposes, the orchestrator validates
# --------------------------------------------------------------------------- #
def test_a_correct_order_from_aisha_is_preserved_exactly():
    plan = order.resolve([
        order.Step("7 seater", "sales", index=0),
        order.Step("book it", "testdrive", (0,), 1),
    ])
    assert [s.agent for s in plan.steps] == ["sales", "testdrive"]
    assert plan.ok and not plan.disagreements


def test_a_backwards_order_is_corrected_because_testdrive_needs_a_car():
    """TestDrive `requires: [vehicle_id]` and Sales `produces` it. Whatever
    Aisha said, the booking step cannot run first."""
    plan = order.resolve([
        order.Step("book it", "testdrive", index=0),
        order.Step("7 seater", "sales", index=1),
    ])
    assert [s.agent for s in plan.steps] == ["sales", "testdrive"]


def test_a_requirement_already_met_by_the_session_needs_no_extra_step():
    """"book it for Saturday" as a follow-up: the car is already established, so
    TestDrive runs alone rather than demanding a Sales step nobody asked for."""
    plan = order.resolve(
        [order.Step("book it", "testdrive", index=0)], known={"vehicle_id"}
    )
    assert [s.agent for s in plan.steps] == ["testdrive"]
    assert plan.ok


def test_an_unsatisfiable_step_is_blocked_and_says_what_it_needed():
    """Aisha can then ask the ONE question that unblocks it, rather than
    apologising vaguely."""
    plan = order.resolve([order.Step("book a drive", "testdrive", index=0)])
    assert not plan.ok
    step, missing = plan.blocked[0]
    assert step.agent == "testdrive" and missing == ["vehicle_id"]


def test_independent_briefs_run_in_one_wave_not_in_series():
    """Two questions that need nothing from each other must not be serialised
    just because Aisha listed one first."""
    plan = order.resolve(
        [order.Step("price", "sales", index=0), order.Step("branch", "testdrive", index=1)],
        known={"vehicle_id"},
    )
    waves = order.independent_groups(plan)
    assert len(waves) == 1 and len(waves[0]) == 2


def test_a_dependency_forces_a_second_wave():
    plan = order.resolve([
        order.Step("7 seater", "sales", index=0),
        order.Step("book it", "testdrive", (0,), 1),
    ])
    assert [[s.agent for s in w] for w in order.independent_groups(plan)] == [
        ["sales"], ["testdrive"]
    ]


def test_showroom_is_not_a_requirement_of_testdrive():
    """THE TRAP THE PLAN NAMES. A precondition nothing reliably produces
    deadlocks the ordering forever — the customer's branch is a CONSTRAINT that
    narrows a tool call, never a gate on whether the agent runs."""
    assert "showroom_id" not in order.requires("testdrive")
    assert order.requires("testdrive") == {"vehicle_id"}


def test_the_cards_declare_the_producer_of_every_requirement():
    """Otherwise a step can never be satisfied and every turn needing it blocks."""
    producible = set()
    for agent in select.ROUTABLE:
        producible |= order.produces(agent)
    for agent in select.ROUTABLE:
        unmet = order.requires(agent) - producible - order.AMBIENT
        assert unmet == set(), f"{agent} requires {unmet}, which nothing produces"


# --------------------------------------------------------------------------- #
# run — dispatch and partial failure
# --------------------------------------------------------------------------- #
BRIEFS = [
    {"brief": "I need a 7 seater for the family"},
    {"brief": "book a test drive on Saturday", "depends_on": [0]},
]
JUDGE = _judge({
    "I need a 7 seater for the family": "sales",
    "book a test drive on Saturday": "testdrive",
})


@needs_model
def test_results_thread_forward_from_one_agent_to_the_next():
    """THE WHOLE REASON ORDER MATTERS. Without it, "a 7-seater and book me in on
    Saturday" becomes two unrelated answers and the customer is asked which car
    they meant, immediately after telling us."""
    seen = {}

    def runner(step, ctx):
        seen[step.agent] = dict(ctx)
        return {"vehicle_id": 8} if step.agent == "sales" else {"slot_id": 41}

    result = run.execute(BRIEFS, runner, judge=JUDGE)
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert seen["testdrive"]["vehicle_id"] == 8
    assert result.entities["vehicle_id"] == 8


@needs_model
def test_one_failed_step_does_not_discard_the_other_answers():
    """"Answer what worked, name what didn't." A Finance step that times out
    must not throw away the Sales answer already in hand."""
    def runner(step, ctx):
        if step.agent == "testdrive":
            raise TimeoutError("slow")
        return {"vehicle_id": 8}

    result = run.execute(BRIEFS, runner, judge=JUDGE)
    assert result.any_ok and result.partial
    failed = [s for s in result.steps if s.status == "failed"]
    assert failed and "timed out" in failed[0].detail


@needs_model
def test_a_failed_step_contributes_no_entities():
    """A failed Sales step must not leave a vehicle_id behind for TestDrive to
    trust — that is how a wrong car reaches a booking."""
    def runner(step, ctx):
        if step.agent == "sales":
            raise RuntimeError("no")
        return {}

    result = run.execute(BRIEFS, runner, judge=JUDGE)
    assert result.entities.get("vehicle_id") is None


@needs_model
def test_a_blocked_step_reports_what_it_needed():
    result = run.execute(
        [{"brief": "book a test drive on Saturday"}], lambda s, c: {}, judge=JUDGE
    )
    assert result.steps[0].status == "blocked"
    assert "vehicle_id" in result.steps[0].detail


@needs_model
def test_an_unroutable_brief_does_not_abort_the_other_steps():
    briefs = [{"brief": "tell me a joke"}, {"brief": "what SUVs do you have"}]
    result = run.execute(briefs, lambda s, c: {"vehicle_id": 1}, judge=_judge({}))
    kinds = {s.status for s in result.steps}
    assert "unroutable" in kinds and "ok" in kinds


# --------------------------------------------------------------------------- #
# trace — what gets written, and what must not be
# --------------------------------------------------------------------------- #
@needs_model
def test_the_trace_records_the_decision_and_never_the_brief():
    """THE BRIEF CARRIES THE CONVERSATION PROFILE — family size, area, budget —
    and this column is written on every turn. What lands is the decision: which
    agent, chosen how, how confident, how long, whether it worked."""
    result = run.execute(BRIEFS, lambda s, c: {"vehicle_id": 8}, judge=JUDGE)
    payload = run.trace_steps(result)
    assert payload and all("agent" in row and "status" in row for row in payload)
    blob = str(payload)
    assert "7 seater" not in blob and "family" not in blob
    assert "brief" not in payload[0]


@needs_model
def test_the_trace_keeps_the_scores_so_the_threshold_can_be_recalibrated():
    """The rule was calibrated on 32 synthetic utterances. These rows are how it
    gets re-measured against real briefs — the only way that number stops being
    a guess."""
    result = run.execute(BRIEFS, lambda s, c: {"vehicle_id": 8}, judge=JUDGE)
    rows = run.trace_steps(result)
    assert any(r["layer"] == "fast_lane" and r["margin"] is not None for r in rows)


# --------------------------------------------------------------------------- #
# cascade — state machine, then sticky, then planning
# --------------------------------------------------------------------------- #
def test_a_bare_yes_is_a_confirmation_before_it_is_a_follow_up():
    """THE CASE THAT FORCES THE ORDER. Sticky would hand "yes" to whichever
    read-only agent spoke last, where it can do nothing — the thing being
    confirmed is a WRITE, and writes are the orchestrator's."""
    pending = lambda m: "a hold on Saturday 10:00" if m.lower() == "yes" else None
    path = cascade.decide("yes", current_agent="testdrive", pending_confirmation=pending)
    assert path.kind == "confirmation"


def test_a_new_question_is_not_swallowed_just_because_something_is_pending():
    """An earlier version asked only "is anything pending?" and claimed every
    turn while a hold was open — so "and Sunday?" was read as a confirmation
    when it is plainly a new question about a different day."""
    pending = lambda m: "a hold" if m.lower() == "yes" else None
    path = cascade.decide("and Sunday?", current_agent="testdrive", pending_confirmation=pending)
    assert path.kind != "confirmation"


def test_with_nothing_pending_the_cascade_behaves_exactly_as_it_does_today():
    assert cascade.decide("yes", current_agent="testdrive").kind == "sticky"
    assert cascade.decide("what SUVs do you have").kind == "planning"


def test_a_broken_state_machine_falls_through_rather_than_failing_the_turn():
    def boom(_message):
        raise RuntimeError("state machine exploded")

    path = cascade.decide("yes", current_agent="testdrive", pending_confirmation=boom)
    assert path.kind in {"sticky", "planning"}


# --------------------------------------------------------------------------- #
# the judge is wired but never called from here
# --------------------------------------------------------------------------- #
def test_the_judge_builds_its_few_shot_from_the_cards():
    """Read from the cards so the judge and the eval cannot disagree about what
    a card means."""
    examples = judge_mod._examples()
    assert "-> sales" in examples and "-> testdrive" in examples and "-> finance" in examples


def test_the_judge_prompt_encodes_the_three_traps():
    system = judge_mod.SYSTEM.lower()
    assert "list price" in system and "total cost" in system
    assert "change" in system
    assert "negotiation" in system or "negotiating" in system


def test_the_judge_returns_none_on_an_empty_brief_without_calling_out():
    assert judge_mod.judge("", ["sales"]) is None
    assert judge_mod.judge("   ", ["sales"]) is None
