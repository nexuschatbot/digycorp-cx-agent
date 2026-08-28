"""The multi-agent turn: bodies, budgets, events, profile, kill switch (Phase 5).

EVERYTHING HERE IS OFFLINE. The sub-agent runner is injected, the judge is a
stub, and the kill switch is off by default — so none of this can reach the
network, which is also the point of shipping it behind a flag.
"""

from __future__ import annotations

import pytest

from app.agent import profile as profile_mod
from app.agents import loader
from app.orchestrator import run, turn


# --------------------------------------------------------------------------- #
# T5.2 — the sub-agent bodies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("agent", ["sales", "testdrive", "finance"])
def test_every_sub_agent_has_a_body_and_carries_base(agent):
    """Grounding and data minimisation are not optional for anyone."""
    assert loader.sections(agent)
    body = loader.body_for(agent)
    for base_text in loader.sections("base").values():
        assert base_text in body


def test_the_sales_and_testdrive_prose_was_ported_not_written():
    """Rule zero: port, don't re-author. Every section of these two is a
    byte-for-byte slice of prose that already shipped."""
    aisha = loader.sections("aisha")
    for agent in ("sales", "testdrive"):
        for name, text in loader.sections(agent).items():
            assert name in aisha, f"{agent}/{name} is not from the shipped prompt"
            assert text == aisha[name], f"{agent}/{name} was edited in the move"


def test_the_finance_body_is_new_and_says_so():
    """It is the ONE agent with nothing to port, and its guardrails belong to
    Phase 6 — so the file states that rather than pretending to be a port."""
    raw = (loader.AGENTS_DIR / "finance" / "v1.md").read_text(encoding="utf-8")
    assert "T6.1" in raw and "nothing to port" in raw.lower()
    body = "".join(loader.sections("finance").values()).lower()
    # Only what is already decided fact and enforced in code.
    assert "indicative" in body
    assert "sample" in body
    assert "never state a validity date" in body


def test_no_sub_agent_body_carries_the_persona_or_the_output_rules():
    """Those live in base/ and aisha/ ALONE, because a sub-agent returns DATA
    and never a sentence a customer reads. That is what makes Haiku safe."""
    aisha_only = {"IDENTITY", "CONSENT", "LICENCE", "BOOKING_SEQUENCE"}
    for agent in ("sales", "finance"):
        assert not (set(loader.sections(agent)) & aisha_only)


# --------------------------------------------------------------------------- #
# T5.8 — the kill switch
# --------------------------------------------------------------------------- #
def test_multi_agent_is_off_by_default():
    """The single-agent path stays the one customers meet until the baseline has
    been re-measured against the new one."""
    assert turn.enabled() is False


def test_voice_is_excluded_even_when_the_switch_is_on(monkeypatch):
    """PER CHANNEL, NOT GLOBAL. run_turn is shared and realtime has a 400ms
    first-audio budget; the measured Haiku round trip alone is 1088ms."""
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    assert turn.enabled(voice_call=False) is True
    assert turn.enabled(voice_call=True) is False


# --------------------------------------------------------------------------- #
# T5.6 — time budgets
# --------------------------------------------------------------------------- #
def test_composition_time_is_reserved_before_any_step_runs():
    """AGENT_DEADLINE_SECONDS is stamped once for the whole cascade today, so a
    slow sub-agent eats Aisha's composition budget and the turn dies with NO
    REPLY AT ALL — worse than a partial answer."""
    per_step = turn.budget_for(2, 60.0)
    assert per_step * 2 <= 60.0 - turn.COMPOSE_RESERVE_SECONDS + 0.01


def test_more_steps_means_less_each_but_never_nothing():
    assert turn.budget_for(2, 60.0) > turn.budget_for(5, 60.0)
    assert turn.budget_for(50, 10.0) >= 1.0


def test_a_step_starting_with_no_time_left_fails_rather_than_starts():
    """Beginning a call we cannot afford to finish spends money and returns
    nothing."""
    def slow(step, ctx, **kw):
        import time
        time.sleep(0.05)
        return {"vehicle_id": 1}

    result = turn.execute(
        [{"brief": "a"}, {"brief": "b"}],
        slow,
        judge=lambda b, a: "sales",
        deadline_seconds=turn.COMPOSE_RESERVE_SECONDS,  # nothing left for steps
    )
    assert all(s.status in {"failed", "blocked"} for s in result.steps)


def test_the_sub_graph_iteration_ceiling_is_below_the_single_agent_one():
    """A sub-agent answers ONE brief with read-only tools. Needing more rounds
    means it is lost, and the honest outcome is a failed step the customer hears
    about rather than a turn that never ends."""
    assert turn.SUBAGENT_MAX_ITERATIONS < 16


# --------------------------------------------------------------------------- #
# T5.3 — the event forwarding that silently blanks the UI if missed
# --------------------------------------------------------------------------- #
def test_sub_agent_events_reach_the_parent_stream():
    """runner._card_anchor builds the vehicle and slot cards from tool_events. A
    sub-agent that emits them into its own graph and nowhere else produces a
    turn where every card is missing and NOTHING ERRORS."""
    seen = []
    emit = turn.forward_events(lambda k, p: seen.append((k, p)), "sales")
    emit("tool_event", {"tool": "search_vehicles"})
    assert seen == [("tool_event", {"tool": "search_vehicles", "agent": "sales"})]


def test_a_forwarded_event_says_which_agent_produced_it():
    """So a card cannot be attributed to Aisha, and the routing view can show
    which specialist produced which card."""
    seen = []
    turn.forward_events(lambda k, p: seen.append(p), "testdrive")("tool_event", {})
    assert seen[0]["agent"] == "testdrive"


def test_forwarding_never_breaks_a_turn():
    def boom(kind, payload):
        raise RuntimeError("stream is gone")

    turn.forward_events(boom, "sales")("tool_event", {})  # must not raise
    turn.forward_events(None, "sales")("tool_event", {})   # nor with no writer


def test_the_runner_is_handed_the_forwarder_and_the_budget():
    got = {}

    def runner(step, ctx, **kw):
        got.update(kw)
        return {}

    turn.execute([{"brief": "x"}], runner, judge=lambda b, a: "sales")
    assert callable(got["emit"])
    assert got["timeout"] > 0
    assert got["max_iterations"] == turn.SUBAGENT_MAX_ITERATIONS


# --------------------------------------------------------------------------- #
# T5.4 — composing from findings AND status
# --------------------------------------------------------------------------- #
def test_the_composer_is_told_what_failed_not_just_what_worked():
    """"Answer what worked, name what didn't" is impossible if the composer only
    sees what worked — it would report a half-answer as a whole one."""
    result = run.TurnResult(steps=[
        run.StepResult(brief="a", agent="sales", status="ok", findings={"vehicle_id": 8}),
        run.StepResult(brief="b", agent="testdrive", status="failed",
                       detail="timed out after 20s"),
    ])
    payload = turn.compose_input(result)
    assert payload["partial"] is True
    assert payload["findings"][0]["agent"] == "sales"
    assert payload["unavailable"][0]["agent"] == "testdrive"
    assert "timed out" in payload["unavailable"][0]["detail"]


def test_a_total_failure_is_distinguished_from_a_partial_one():
    """They need different wording: "I couldn't check that" versus "here is what
    I found, but I couldn't check stock"."""
    all_failed = run.TurnResult(steps=[
        run.StepResult(brief="a", agent="sales", status="failed"),
    ])
    payload = turn.compose_input(all_failed)
    assert payload["nothing_worked"] is True and payload["partial"] is False


# --------------------------------------------------------------------------- #
# T5.5 — the conversation profile
# --------------------------------------------------------------------------- #
def test_the_profile_is_not_session_facts_and_says_so_when_rendered():
    """session_facts is folded from TOOL RESULTS only (rule 2). Family size never
    comes from a tool; it comes from somebody saying it. Mixing an assertion into
    the grounded anchor would make "he has six children" as authoritative as
    "booking TD-2026-HTXE exists"."""
    p = profile_mod.Profile().merge(seats_needed=6, area="Deira")
    text = p.render()
    assert "TOLD YOU" in text
    assert "not verified facts" in text
    assert "never as facts" in text.lower() or "never as facts you have" in text.lower()


def test_an_empty_profile_renders_nothing():
    assert profile_mod.Profile().render() == ""


def test_unknown_fields_are_dropped_loudly_not_stored():
    """A closed set is only closed if writing outside it fails visibly —
    silently accepting `mobile` would put a phone number one render away from a
    brief."""
    p = profile_mod.Profile().merge(mobile="+971500000000", seats_needed=7)
    assert not hasattr(p, "mobile")
    assert p.seats_needed == 7


def test_a_brief_carries_constraints_and_never_the_person():
    """Three more models see conversation context from Phase 5. This is the
    data-minimisation line."""
    p = profile_mod.Profile().merge(seats_needed=7, area="Deira", must_haves=["good audio"])
    constraints = p.constraints_for_brief()
    assert constraints["seats_needed"] == 7 and constraints["area"] == "Deira"
    for banned in profile_mod.NEVER_IN_BRIEF:
        assert banned not in constraints


def test_must_haves_accumulate_without_duplicating():
    p = profile_mod.Profile()
    p.merge(must_haves="seven seats").merge(must_haves=["good audio", "seven seats"])
    assert p.must_haves == ["seven seats", "good audio"]


def test_nothing_in_this_module_writes_to_the_database():
    """The profile is WORKING MEMORY until the customer consents. Persistence
    goes through the existing consent-gated save_memory and the forget_me path
    that already clears it — there is deliberately no write here."""
    import inspect

    src = inspect.getsource(profile_mod)
    for forbidden in ("db.add", "db.commit", "session.add", "sa_delete", "update("):
        assert forbidden not in src


def test_the_promotion_note_is_what_save_memory_would_store():
    p = profile_mod.Profile().merge(seats_needed=6, area="Deira", must_haves=["good audio"])
    note = profile_mod.promotion_note(p)
    assert "6 seats" in note and "Deira" in note and "good audio" in note
    assert profile_mod.promotion_note(profile_mod.Profile()) == ""
