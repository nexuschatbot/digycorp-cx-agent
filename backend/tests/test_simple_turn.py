"""SIMPLE_TURN_MODEL (OPEN-ISSUES #2) — the cheap tier for turns a rule can
prove simple.

The dial ships OFF and the detector is deliberately narrow: the router
prefilter's bare-greeting rule and nothing else. These tests pin both halves —
the shipped default, and the detector's refusal to call anything else simple.
"""

from app.agent import graph as graph_mod


def test_simple_turn_model_is_off_by_default():
    """Which model greets a customer is a product trade; the shipped default
    keeps every turn on the agent model until the measured run decides."""
    from app import config

    assert config.SIMPLE_TURN_MODEL == ""


def _state(messages):
    return {"messages": messages}


def test_a_bare_greeting_is_a_simple_turn():
    assert graph_mod._is_simple_turn(_state([{"role": "user", "content": "hi"}]))


def test_a_greeting_in_content_blocks_is_still_simple():
    state = _state([
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ])
    assert graph_mod._is_simple_turn(state)


def test_a_question_is_not_a_simple_turn():
    state = _state([{"role": "user", "content": "hi, how much is the X70?"}])
    assert not graph_mod._is_simple_turn(state)


def test_an_affirmation_is_not_a_simple_turn():
    """A bare "yes" belongs to the booking sequence — the one place
    conversational judgement is the product. It must never take the discount."""
    state = _state([{"role": "user", "content": "yes"}])
    assert not graph_mod._is_simple_turn(state)


def test_a_tool_round_reentry_is_not_a_simple_turn():
    """On re-entry the last user message is a tool_result: no text, no
    discount — the conservative direction."""
    state = _state([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "record_consent", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result",
                                      "tool_use_id": "t1", "content": "{}"}]},
    ])
    assert not graph_mod._is_simple_turn(state)


def test_empty_or_absent_messages_are_not_simple():
    assert not graph_mod._is_simple_turn(_state([]))
    assert not graph_mod._is_simple_turn({"messages": None})
