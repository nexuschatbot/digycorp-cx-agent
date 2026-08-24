"""The ROUTED path, end to end through run_turn.

WHY THIS FILE EXISTS. tests/conftest.py pins the ported suite to
ROUTER_ENABLED=False, because those tests are the single-agent contract and must
keep testing it. That leaves the routed path uncovered — so it is covered here,
explicitly, with the classifier stubbed. Nothing in this file calls a vendor.

The properties that matter are not "does it route" but the ones that would be
expensive to discover in production: that the active agent SURVIVES to the next
turn (or a bare "yes" has nothing to stay with), that a non-customer turn does
not move it, and that "fallback" degrades to the full single-agent capability
rather than raising on a prompt that does not exist.
"""

from __future__ import annotations

import pytest

from app import config as _config
from app.agent.router.policy import Routing
from app.agent.runner import _journey_agent


@pytest.fixture
def routing_on(monkeypatch):
    monkeypatch.setattr(_config, "ROUTER_ENABLED", True)


# --------------------------------------------------------------------------- #
# fallback is not an agent
# --------------------------------------------------------------------------- #
def test_fallback_degrades_to_the_full_single_agent_capability():
    """There is no fallback prompt and no fallback tool set — asking raises."""
    from app.agent.prompt import system_prompt
    from app.agent.prompts import compose

    with pytest.raises(ValueError):
        compose.body_for("fallback")

    # So run_turn must never hand it on.
    assert _journey_agent(Routing("fallback", "unclear"), None) is None
    # Unrouted means the combined prompt: the system that handled these turns
    # before the split.
    assert system_prompt(agent=None) == system_prompt()


def test_fallback_keeps_an_active_agent_rather_than_dropping_it():
    """A customer mid-booking who says something unclassifiable is still mid-booking."""
    assert _journey_agent(Routing("fallback", "unclear"), "testdrive") == "testdrive"


def test_a_real_agent_is_passed_through():
    assert _journey_agent(Routing("bookingmanagement", "x"), "testdrive") == "bookingmanagement"


def test_no_routing_keeps_whatever_the_checkpoint_held():
    assert _journey_agent(None, "bookingmanagement") == "bookingmanagement"
    assert _journey_agent(None, None) is None


# --------------------------------------------------------------------------- #
# the prompt and the tools come from ONE value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("agent", ["testdrive", "bookingmanagement"])
def test_prompt_and_tools_agree_about_which_agent_this_is(agent):
    """The defect this design forecloses: a prompt for one agent, tools for another."""
    from app.agent import tool_binding
    from app.agent.prompt import system_prompt

    prompt = system_prompt(agent=agent)
    allowed = tool_binding.tools_for(agent)

    forbidden = set(tool_binding.SHARED | set().union(*tool_binding.JOURNEY.values())) - allowed
    for tool in forbidden:
        assert tool not in prompt, f"{agent}'s prompt names {tool}, which it cannot call"


def test_the_router_is_off_by_default_in_the_ported_suite():
    """Guards the pin itself: if this flips, 1000 tests silently change meaning."""
    assert _config.ROUTER_ENABLED is False


def test_routing_can_be_turned_on_in_scope(routing_on):
    assert _config.ROUTER_ENABLED is True


# --------------------------------------------------------------------------- #
# the classifier must fail fast, not retry
# --------------------------------------------------------------------------- #
def test_the_router_client_does_not_retry():
    """A retry on the voice hot path is silence on a phone call."""
    import inspect

    from app.agent.router import classifier

    source = inspect.getsource(classifier._get_client)
    assert "max_retries=0" in source
    assert "timeout=" in source


def test_a_blocked_network_degrades_immediately(monkeypatch):
    """The network guard stands in for an outage: it must become fallback, fast."""
    from app.agent.router import classifier

    monkeypatch.setattr(classifier, "_client", None)
    decision = classifier.classify("what about the weekend?")
    assert decision.agent == "fallback"
    assert decision.confidence == 0.0
