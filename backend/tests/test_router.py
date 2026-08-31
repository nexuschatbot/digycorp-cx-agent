"""The routing cascade, tested offline.

Everything here runs with a canned classification, so the suite stays free and
hermetic. The one thing it cannot answer — does Haiku label real messages
correctly — is checked deliberately by evals/routing_live.py, outside pytest.

The rules-only cases ARE fully tested here: those exits must never reach a model,
which is a latency guarantee as much as a correctness one.
"""

from __future__ import annotations

import pytest

from app.agent.router import classifier, prefilter, sticky
from app.agent.router.policy import RouteDecision, Routing, apply
from app.agent.router.route import route
from evals.routing_cases import CASES


def _canned(agent="testdrive", confidence=0.95, second="fallback", margin=0.9):
    def generate(system, user, **kwargs):
        return {
            "agent": agent,
            "confidence": confidence,
            "second_best": second,
            "margin": margin,
        }

    return generate


def _explodes(system, user, **kwargs):
    raise RuntimeError("the router must never take a turn down with it")


# --------------------------------------------------------------------------- #
# Layer 1 — pre-filter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "Move TD-2026-HTXE to Sunday",
    "TD-2026-HTXE",
    "td.2026.htxe please",
    "my ref is TD 2026 HTXE",
    "TD_2026_HTXE",
])
def test_a_booking_reference_routes_without_a_model(message):
    assert prefilter.booking_reference(message) == "TD-2026-HTXE"
    assert prefilter.decide(message) == "bookingmanagement"


@pytest.mark.parametrize("message", [
    "can I change to the 7-seater instead",   # 'change', but about a car
    "what SUVs do you have?",
    "I want to book a test drive",
    "TD-20 is my flight",                     # not a reference shape
])
def test_prefilter_lets_testdrive_traffic_through(message):
    assert prefilter.decide(message) is None


def test_prefilter_reuses_bookings_own_normaliser():
    """A ref the service would reject must not be invented here."""
    assert prefilter.booking_reference("TD-2026-HTXEXTRA") is None
    assert prefilter.booking_reference("TD-26-HTXE") is None


# --------------------------------------------------------------------------- #
# Layer 2 — sticky gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", ["yes", "ok", "تمام", "3pm", "15:30", "the second one", "Al Quoz"])
def test_a_followup_stays_with_the_active_agent(message):
    assert sticky.decide(message, "testdrive") == "testdrive"
    assert sticky.decide(message, "bookingmanagement") == "bookingmanagement"


def test_sticky_does_nothing_without_an_active_agent():
    assert sticky.decide("yes", None) is None


def test_a_real_question_is_not_sticky():
    assert sticky.decide("what SUVs do you have?", "testdrive") is None


# --------------------------------------------------------------------------- #
# Layer 4 — threshold policy
# --------------------------------------------------------------------------- #
def test_a_switch_needs_confidence_and_margin():
    confident_and_clear = RouteDecision("bookingmanagement", 0.95, "testdrive", 0.6)
    assert apply(confident_and_clear, "testdrive").agent == "bookingmanagement"


def test_a_confident_switch_with_a_thin_margin_is_withheld():
    coin_toss = RouteDecision("bookingmanagement", 0.86, "testdrive", 0.04)
    result = apply(coin_toss, "testdrive")
    assert result.agent == "testdrive"
    assert result.clarify is True


def test_a_low_confidence_switch_is_withheld_without_asking():
    weak = RouteDecision("bookingmanagement", 0.5, "testdrive", 0.4)
    result = apply(weak, "testdrive")
    assert result.agent == "testdrive"
    assert result.clarify is False


def test_staying_needs_no_margin():
    """The asymmetry: staying is cheap, so a middling score still routes."""
    middling = RouteDecision("testdrive", 0.75, "bookingmanagement", 0.01)
    assert apply(middling, "testdrive").agent == "testdrive"


def test_unclear_with_no_active_agent_clarifies_once():
    lost = RouteDecision("testdrive", 0.4, "bookingmanagement", 0.05)
    result = apply(lost, None)
    assert result.agent == "fallback"
    assert result.clarify is True


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #
def test_rules_short_circuit_before_the_model():
    """If the model were reached, _explodes would take the turn down."""
    assert route("cancel my booking", generate=_explodes).agent == "bookingmanagement"
    assert route("yes", current_agent="testdrive", generate=_explodes).agent == "testdrive"


def test_a_broken_classifier_degrades_instead_of_raising():
    result = route("what about the weekend?", current_agent="testdrive",
                   generate=_explodes)
    assert isinstance(result, Routing)
    assert result.agent == "testdrive"       # stayed put rather than crashed


@pytest.mark.parametrize("bad", [
    {"agent": "sales", "confidence": 0.9, "second_best": "fallback", "margin": 0.5},
    {"agent": "testdrive", "confidence": "high", "second_best": "x", "margin": None},
    {},
])
def test_an_unusable_classification_becomes_fallback(bad):
    decision = classifier.classify("hello", generate=lambda s, u, **k: bad)
    assert decision.agent in ("fallback", "testdrive")
    assert 0.0 <= decision.confidence <= 1.0


def test_session_note_carries_what_a_followup_needs():
    note = classifier.session_note(
        current_agent="testdrive",
        session_facts={"vehicle_name": "Jetour X70 Plus", "showroom_name": "Al Quoz"},
        live_bookings=[{"ref": "TD-2026-HTXE"}],
    )
    assert "testdrive" in note
    assert "Jetour X70 Plus" in note
    assert "existing bookings: 1" in note


# --------------------------------------------------------------------------- #
# The eval set's free cases, run for real
# --------------------------------------------------------------------------- #
RULES_ONLY = [c for c in CASES if c.expect_rules_only]


@pytest.mark.parametrize("case", RULES_ONLY, ids=[c.message for c in RULES_ONLY])
def test_rules_only_cases_never_reach_the_model(case):
    result = route(
        case.message,
        current_agent=case.current_agent,
        session_facts=case.session_facts,
        live_bookings=list(case.live_bookings),
        generate=_explodes,
    )
    assert result.agent == case.expect


def test_the_eval_set_covers_every_agent_and_both_languages():
    expected = {c.expect for c in CASES}
    assert expected == {"testdrive", "bookingmanagement", "fallback"}
    arabic = [c for c in CASES if any("؀" <= ch <= "ۿ" for ch in c.message)]
    assert len(arabic) >= 4


# --------------------------------------------------------------------------- #
# S7 — the binding is what enforces the allowlist, not the prompt
# --------------------------------------------------------------------------- #
def test_bookingmanagement_physically_cannot_create_a_booking():
    """Not 'is told not to' — does not have it."""
    from app.agent import tool_binding

    names = {s["name"] for s in tool_binding.schemas_for("bookingmanagement")}
    assert "create_booking" not in names
    assert "search_vehicles" not in names
    assert "reschedule_booking" in names


def test_testdrive_cannot_cancel_or_reschedule():
    from app.agent import tool_binding

    names = {s["name"] for s in tool_binding.schemas_for("testdrive")}
    assert "cancel_booking" not in names
    assert "reschedule_booking" not in names
    assert "create_booking" in names


def test_both_agents_keep_the_shared_identity_and_escalation_tools():
    from app.agent import tool_binding

    for agent in tool_binding.AGENTS:
        names = {s["name"] for s in tool_binding.schemas_for(agent)}
        for shared in ("find_or_create_customer", "request_human_callback",
                       "save_memory", "close_conversation", "get_booking"):
            assert shared in names, f"{agent} lost {shared}"


def test_an_unrouted_turn_still_gets_the_whole_registry():
    """Every channel that has not adopted the router must be untouched."""
    from app.agent.prompt import system_prompt
    from app.tools import TOOL_SCHEMAS

    assert len(TOOL_SCHEMAS) == 22
    assert system_prompt(agent=None) == system_prompt()


def test_the_journey_prompt_drops_the_other_journeys_rules():
    from app.agent.prompt import system_prompt

    bm = system_prompt(agent="bookingmanagement")
    td = system_prompt(agent="testdrive")
    assert "FINDING THE RIGHT CAR" in td
    assert "FINDING THE RIGHT CAR" not in bm
    assert "RESCHEDULING" in bm
    assert "RESCHEDULING" not in td


def test_the_compare_paragraph_travels_with_testdrive_only():
    from app.agent.prompt import system_prompt

    assert "compare_vehicles" in system_prompt(agent="testdrive")
    assert "compare_vehicles" not in system_prompt(agent="bookingmanagement")
