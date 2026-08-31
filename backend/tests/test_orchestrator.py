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
    assert step.agent == "testdrive" and missing == ["booking_ref|vehicle_id"]


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
    assert order.requires("testdrive") == {"vehicle_id", "booking_ref"}
    # ONE group with two alternatives, not two requirements: either satisfies.
    assert order.requires_groups("testdrive") == [frozenset({"vehicle_id", "booking_ref"})]


def test_a_booking_ref_satisfies_testdrive_without_a_vehicle():
    """The manage journey (found live, 2026-08-31): "cancel TD-2026-0KXC" in a
    fresh chat has no vehicle_id and needs none — the booking knows its
    vehicle. The gate used to block it, and Aisha told the customer the
    booking could not be cancelled."""
    plan = order.resolve(
        [order.Step("cancel the booking", "testdrive", index=0)],
        known={"booking_ref"},
    )
    assert plan.ok and [s.agent for s in plan.steps] == ["testdrive"]


def test_what_a_brief_carries_is_per_step_not_global():
    """carries satisfies the step that carries it; it is never folded into the
    plan's known set, so a ref in a cancel brief cannot stand in for entities
    an unrelated step needs."""
    plan = order.resolve([
        order.Step("cancel TD-2026-0KXC", "testdrive", index=0,
                   carries=frozenset({"booking_ref"})),
        order.Step("the instalments", "finance", index=1),
    ])
    assert [s.agent for s in plan.steps] == ["testdrive"]
    step, missing = plan.blocked[0]
    assert step.agent == "finance" and missing == ["variant_id"]
    assert "booking_ref" not in plan.known


def test_plan_turn_detects_the_reference_the_customer_said():
    """The TD shape is fixed and separators are read, not required — the same
    forgiveness booking._normalise_ref applies to a spoken or typed ref."""
    from app.orchestrator import run as run_module

    for text in ("Cancel the test drive booking TD-2026-0KXC",
                 "cancel the customer's test drive, reference td 2026 htxe",
                 "cancel the booking with reference td2026htxe"):
        plan, unroutable = run_module.plan_turn([{"brief": text}])
        assert not unroutable and plan.ok, text
        assert plan.steps[0].carries == frozenset({"booking_ref"}), text
    plan, _ = run_module.plan_turn([{"brief": "book me a test drive"}])
    assert not plan.ok  # no ref, no vehicle: still honestly blocked


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
    def pending(m):
        return "a hold on Saturday 10:00" if m.lower() == "yes" else None

    path = cascade.decide("yes", current_agent="testdrive", pending_confirmation=pending)
    assert path.kind == "confirmation"


def test_a_new_question_is_not_swallowed_just_because_something_is_pending():
    """An earlier version asked only "is anything pending?" and claimed every
    turn while a hold was open — so "and Sunday?" was read as a confirmation
    when it is plainly a new question about a different day."""
    def pending(m):
        return "a hold" if m.lower() == "yes" else None

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


# --------------------------------------------------------------------------- #
# Parallel waves (2026-08-29)
#
# The module docstring promised "two briefs that need nothing from each other
# run together" and a plain nested for-loop delivered the opposite for the whole
# of Phase 3-5. Measured live, a three-question turn spent 34 seconds walking
# them one at a time. These pin the promise down.
# --------------------------------------------------------------------------- #
_PARALLEL_BRIEFS = [
    {"brief": "I need a 7 seater for the family"},
    {"brief": "what would the monthly payment be"},
]
_PARALLEL_JUDGE = _judge({
    "I need a 7 seater for the family": "sales",
    "what would the monthly payment be": "finance",
})
# Finance's card requires a variant_id, so these two are only independent once
# that entity is already known — which is exactly the live case: the customer
# named a trim last turn and now asks two things about it at once.
_PARALLEL_KNOWN = {"variant_id", "vehicle_id"}


def _blocking_runner(gate, *, thread_safe: bool):
    """A runner whose steps can only all finish if they all start first."""
    import threading

    started = threading.Barrier(2, timeout=5)

    def runner(step, ctx):
        if gate:
            started.wait()          # deadlocks unless something else is running
        return {"agent": step.agent}

    runner.thread_safe = thread_safe
    return runner


@needs_model
def test_a_wave_actually_runs_its_steps_at_the_same_time():
    """The barrier is the assertion: it releases only when BOTH steps have
    reached it, so this cannot pass on a runner that walks them in sequence."""
    result = run.execute(
        _PARALLEL_BRIEFS,
        _blocking_runner(True, thread_safe=True),
        judge=_PARALLEL_JUDGE, known=set(_PARALLEL_KNOWN),
        max_workers=2,
    )
    assert [s.status for s in result.steps] == ["ok", "ok"]


@needs_model
def test_a_runner_that_is_not_thread_safe_is_never_widened():
    """The flag is the whole safety interlock: without it two steps would share
    one SQLAlchemy Session, which does not raise — it returns wrong rows."""
    import threading

    live = []
    peak = []
    lock = threading.Lock()

    def runner(step, ctx):
        with lock:
            live.append(step.agent)
            peak.append(len(live))
        try:
            return {"agent": step.agent}
        finally:
            with lock:
                live.remove(step.agent)

    runner.thread_safe = False
    result = run.execute(
        _PARALLEL_BRIEFS, runner, judge=_PARALLEL_JUDGE, known=set(_PARALLEL_KNOWN), max_workers=4,
    )
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert max(peak) == 1, "a runner without its own session must stay serial"


@needs_model
def test_results_come_back_in_wave_order_not_completion_order():
    """The transcript, the trace and every reader of result.steps must not
    depend on which specialist happened to answer first."""
    import time as _t

    def runner(step, ctx):
        # Sales dawdles, so completion order is the reverse of wave order.
        if step.agent == "sales":
            _t.sleep(0.15)
        return {"agent": step.agent}

    runner.thread_safe = True
    result = run.execute(
        _PARALLEL_BRIEFS, runner, judge=_PARALLEL_JUDGE, known=set(_PARALLEL_KNOWN), max_workers=2,
    )
    assert [s.agent for s in result.steps] == ["sales", "finance"]


@needs_model
def test_a_dependent_brief_still_waits_for_the_one_it_needs():
    """Widening a wave must not widen ACROSS waves — depends_on is the whole
    reason results thread forward."""
    seen = {}

    def runner(step, ctx):
        seen[step.agent] = dict(ctx)
        return {"vehicle_id": 8} if step.agent == "sales" else {"slot_id": 41}

    runner.thread_safe = True
    result = run.execute(BRIEFS, runner, judge=JUDGE, max_workers=4)
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert seen["testdrive"]["vehicle_id"] == 8


@needs_model
def test_every_sub_agent_event_reaches_the_parent_even_from_a_worker_thread():
    """A tool_event that never arrives is a card that never draws, and it does
    not error — the exact failure forward_events exists to prevent."""
    from app.orchestrator import turn as turn_mod

    seen = []

    def runner_fn(step, ctx, *, emit=None, timeout=20.0, max_iterations=6):
        for i in range(5):
            emit("tool_event", {"tool": f"{step.agent}_{i}", "input": {}, "output": {}})
        return {"agent": step.agent}

    runner_fn.thread_safe = True
    turn_mod.execute(
        _PARALLEL_BRIEFS, runner_fn,
        judge=_PARALLEL_JUDGE, known=set(_PARALLEL_KNOWN),
        parent_writer=lambda kind, payload: seen.append(payload["tool"]),
        max_workers=2,
    )
    assert len(seen) == 10
    assert {s.split("_")[0] for s in seen} == {"sales", "finance"}


def test_the_budget_divides_by_what_runs_in_sequence_not_by_step_count():
    """Batching three questions into one call must not give each a third of the
    clock — that would make asking together worse than asking separately, which
    is the opposite of why batching exists."""
    from app.orchestrator import turn as turn_mod

    serial = turn_mod.budget_for(3, 60.0, parallel=1)
    together = turn_mod.budget_for(3, 60.0, parallel=4)
    assert together > serial
    # All three in one wave: the whole usable window, not a third of it.
    assert together == pytest.approx(60.0 - turn_mod.COMPOSE_RESERVE_SECONDS)


# --------------------------------------------------------------------------- #
# Evidence: the tool results reach the composer (2026-08-29)
#
# Aisha used to receive only `findings` — the sub-agent's own summary. Live,
# "compare both" came back as four numbers and a sentence, which is not enough
# to answer with, so she asked twice more: 40 seconds and two identical tables.
# The tool output that would have answered it first time was collected, used for
# entity harvesting, and thrown away.
# --------------------------------------------------------------------------- #
def _step_with_evidence(agent="sales", n=1, size=10):
    from app.orchestrator.run import StepResult

    return StepResult(
        brief="b", agent=agent, status="ok", findings={"vehicle_id": 8},
        evidence=[
            {"tool": "compare_vehicles", "result": {"rows": ["x" * size]}}
            for _ in range(n)
        ],
    )


def test_the_composer_receives_what_the_tools_returned():
    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod

    result = run_mod.TurnResult(steps=[_step_with_evidence()])
    payload = turn_mod.compose_input(result)

    assert payload["evidence"], "the composer must see the raw tool output"
    assert payload["evidence"][0]["tool"] == "compare_vehicles"
    assert payload["evidence"][0]["agent"] == "sales"
    assert payload["evidence"][0]["result"] == {"rows": ["x" * 10]}


def test_evidence_is_lifted_off_findings_not_left_in_them():
    """`_evidence` travels on findings only as far as run.execute. Leaving it
    there would put tool output in a field Aisha reads as the specialist's own
    words — and would double it in the payload."""
    from app.orchestrator import run as run_mod

    def runner(step, ctx):
        return {"vehicle_id": 8, "_evidence": [{"tool": "t", "result": {"a": 1}}]}

    runner.thread_safe = False
    out = run_mod.execute(
        [{"brief": "I need a 7 seater for the family"}],
        runner,
        judge=_judge({"I need a 7 seater for the family": "sales"}),
    )
    step = out.steps[0]
    assert step.evidence == [{"tool": "t", "result": {"a": 1}}]
    assert "_evidence" not in step.findings
    assert step.findings == {"vehicle_id": 8}


def test_evidence_stays_inside_its_budget():
    """_orchestrator_node truncates the composed payload with a plain slice, so
    unbounded evidence would cut a JSON string in half and hand the model
    something unparseable."""
    import json

    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod

    huge = _step_with_evidence(n=40, size=1000)   # ~40k chars offered
    payload = turn_mod.compose_input(run_mod.TurnResult(steps=[huge]))

    body = json.dumps(payload["evidence"], default=str)
    assert len(body) <= turn_mod.EVIDENCE_BUDGET_CHARS * 1.5
    assert payload["evidence"], "the budget must trim, not empty, the evidence"


def test_a_failed_step_contributes_no_evidence():
    """Same rule as entities: a step that failed established nothing, and its
    partial tool output must not be quoted to a customer as fact."""
    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod

    bad = _step_with_evidence()
    bad.status = "failed"
    payload = turn_mod.compose_input(run_mod.TurnResult(steps=[bad]))
    assert payload["evidence"] == []


# --------------------------------------------------------------------------- #
# A step that succeeded and returned nothing (2026-08-31)
# --------------------------------------------------------------------------- #
def test_an_ok_step_that_returned_nothing_is_named_as_a_gap():
    """It is `ok`, so `unavailable` skipped it; it has no findings, so the
    findings list skipped it too — and Aisha saw a lookup that simply was not
    there. She asked again, three times, for 22 seconds."""
    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod
    from app.orchestrator.run import StepResult

    empty = StepResult(brief="b", agent="testdrive", status="ok")
    payload = turn_mod.compose_input(run_mod.TurnResult(steps=[empty]))

    assert payload["findings"] == []
    assert len(payload["unavailable"]) == 1
    gap = payload["unavailable"][0]
    assert gap["agent"] == "testdrive"
    assert gap["status"] == "empty"
    assert "Asking again" in gap["detail"]


def test_a_step_with_evidence_and_no_summary_is_not_a_gap():
    """The tool results are in front of her — calling that missing would make
    her apologise for an answer she is holding."""
    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod

    step = _step_with_evidence()
    step.findings = {}
    payload = turn_mod.compose_input(run_mod.TurnResult(steps=[step]))

    assert payload["unavailable"] == []
    assert payload["evidence"], "the evidence is the answer here"


def test_a_failed_step_is_reported_once_not_twice():
    from app.orchestrator import run as run_mod
    from app.orchestrator import turn as turn_mod
    from app.orchestrator.run import StepResult

    bad = StepResult(brief="b", agent="finance", status="failed", detail="timed out")
    payload = turn_mod.compose_input(run_mod.TurnResult(steps=[bad]))

    assert [u["status"] for u in payload["unavailable"]] == ["failed"]
