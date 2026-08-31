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
    from app.agents import loader

    # RE-POINTED AT T2.1: the prose moved from prompts/compose.py to
    # app/agents/*/v1.md, so the refusal now comes from the loader. The claim is
    # unchanged — "fallback" is not an agent and must not resolve to one.
    with pytest.raises(loader.PromptError):
        loader.body_for("fallback")

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


# --------------------------------------------------------------------------- #
# The checkpoint read is best-effort, and must behave like it
# --------------------------------------------------------------------------- #
def test_a_failed_checkpoint_read_does_not_kill_the_turn(
    routing_on, monkeypatch, db
):
    """A checkpoint that cannot be read costs the sticky hint, not the answer.

    THE BUG THIS PINS. runner.py caught the failure and logged it through `_log`
    — a name that does not exist in that module (it is `log`). So the handler
    itself raised NameError, the `except` swallowed nothing, and a turn that
    should have degraded to "no previous agent" died instead. Worse, it died
    only on the routed path and only when the checkpoint read failed, which is
    why 388 tests never saw it.

    The point of the assertion is therefore NOT that a warning is logged. It is
    that the customer still gets their answer when the telemetry underneath
    fails — the same contract services/turn_trace.py holds.
    """
    import logging
    import threading

    from sqlalchemy.orm import Session

    from app.agent import runner
    from app.agent.router.policy import Routing

    class _UnreadableCheckpoint:
        """Reads fail; generation is fine. The real shape of a corrupt WAL."""

        def __init__(self) -> None:
            self.get_state_calls = 0

        def get_state(self, config):
            self.get_state_calls += 1
            raise RuntimeError("checkpoint db is locked")

        def stream(self, state, config, stream_mode=None):
            yield {"type": "token", "text": "Of course — let me help with that."}

    stub = _UnreadableCheckpoint()
    monkeypatch.setattr(runner, "build_graph", lambda checkpointer: stub)
    # Keep the router off the network: the classifier is not what is under test.
    monkeypatch.setattr(runner, "_route", lambda *a, **k: Routing("testdrive", "stub"))

    # CAPTURED OFF THE LOGGER ITSELF, not via caplog. app/main.py sets
    # `propagate = False` on the "app" logger, so once ANY test in the suite has
    # imported main, records from app.agent.runner never reach caplog's root
    # handler — and this assertion passed alone and failed in the full run,
    # depending on import order. A handler on the exact logger does not care.
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    runner_log = logging.getLogger("app.agent.runner")
    handler = _Capture(level=logging.WARNING)
    runner_log.addHandler(handler)

    events: list[tuple[str, dict]] = []
    try:
        result = runner.run_turn(
            "sess-unreadable-checkpoint",
            "I'd like to book a test drive",
            on_event=lambda e, d: events.append((e, d)),
            cancel=threading.Event(),
            session_factory=lambda: Session(db.get_bind()),
        )
    finally:
        runner_log.removeHandler(handler)

    # The read was attempted, and it failed. TWICE, not once: _attach_voice_customer
    # reads the checkpoint too (runner.py:83), and it has its own guard. Pinning
    # this to == 1 would make the test fail the day either caller is touched,
    # which is not what is being protected here.
    assert stub.get_state_calls >= 1
    # And the turn survived it, all the way to a delivered answer.
    assert result.status != "error"
    assert ("token", {"text": "Of course — let me help with that."}) in events
    # Degraded loudly, not silently.
    assert any("could not read prior agent from checkpoint" in m for m in captured)
