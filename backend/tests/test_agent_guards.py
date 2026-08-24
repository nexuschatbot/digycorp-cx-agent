"""The two things the tool node VETOES, and the option-chip contract.

Both vetoes exist for the same reason: a rule the prompt states but cannot
enforce, where getting it wrong is visible to the customer. Adding a third
should feel expensive, and these tests are part of that cost.

THE RECOGNITION GUARD IS THE ONE THAT MATTERS. On the first live test of C10 the
model called confirm_returning_customer on the word "hello", bound the session,
and read the customer's booking back to whoever was typing. The tool description
forbade exactly that, in capitals, twice, and lost. So the rule became
arithmetic: you cannot answer a question that has not been asked. These tests
pin that arithmetic, including the retry case an iteration-based guard would
have let straight through.
"""

from __future__ import annotations

from app.agent.tool_node import _recognition_refusal
from app.tools.ui_tools import _clean, offer_options


# --------------------------------------------------------------------------- #
# confirm_returning_customer: you cannot answer an unasked question
# --------------------------------------------------------------------------- #
def test_never_asked_is_refused():
    """No question rendered means this browser is linked to nobody."""
    out = _recognition_refusal({"customer_turns": 3})
    assert out is not None
    assert out["error"] == "nobody_to_recognise"
    # The recovery must tell the model to say NOTHING about devices, or it
    # invents a question it has no name for.
    assert "do not mention recognition" in out["detail"].lower()


def test_asked_this_very_turn_is_refused():
    """The failure that actually happened, on the word "hello".

    The question went out on turn 4 and the model tried to confirm it inside the
    same turn — so it answered itself.
    """
    out = _recognition_refusal({"customer_turns": 4, "recognition_offered_turn": 4})
    assert out is not None
    assert out["error"] == "not_asked_yet"


def test_a_retry_inside_the_same_turn_is_refused_again():
    """WHY THE COUNTER IS CUSTOMER TURNS AND NOT ITERATIONS.

    A refused model calls again in the same turn. `customer_turns` does not move
    on a tool loop — only when a human speaks — so the second attempt gets the
    same answer. An iteration counter would have incremented and let it through,
    which is the whole reason this shape was chosen.
    """
    state = {"customer_turns": 4, "recognition_offered_turn": 4}
    assert _recognition_refusal(state) is not None
    assert _recognition_refusal(state) is not None  # unchanged state, same refusal


def test_the_customer_answering_allows_it():
    out = _recognition_refusal({"customer_turns": 5, "recognition_offered_turn": 4})
    assert out is None


def test_answering_several_turns_later_still_allows_it():
    """Someone who ignores the question and answers it three turns on is fine —
    the stamp is written once and never moved forward."""
    assert _recognition_refusal({"customer_turns": 9, "recognition_offered_turn": 4}) is None


def test_an_old_checkpoint_fails_closed():
    """A session checkpointed before these fields existed carries neither. It
    must refuse rather than bind — it simply asks again next turn."""
    assert _recognition_refusal({}) is not None
    assert _recognition_refusal({"customer_turns": 0}) is not None


# --------------------------------------------------------------------------- #
# offer_options: what may become a chip
# --------------------------------------------------------------------------- #
def test_two_options_is_the_minimum(db):
    assert offer_options(db, "s", options=["Yes"])["error"] == "not_enough_options"
    assert offer_options(db, "s", options=[])["error"] == "not_enough_options"
    assert offer_options(db, "s", options=None)["error"] == "not_enough_options"


def test_a_normal_pair_is_offered(db):
    out = offer_options(db, "s", options=["Yes", "No"])
    assert out["offered"] is True
    assert out["options"] == ["Yes", "No"]


def test_order_is_preserved(db):
    """The agent wrote them in the order it asked them; the chips must match."""
    out = offer_options(db, "s", options=["Space", "Efficiency", "Value"])
    assert out["options"] == ["Space", "Efficiency", "Value"]


def test_duplicates_are_dropped_because_a_repeated_chip_is_a_dead_chip(db):
    out = offer_options(db, "s", options=["Yes", "yes", "No"])
    assert out["options"] == ["Yes", "No"]


def test_over_long_labels_are_dropped_not_truncated(db):
    """Truncating would put words in the customer's mouth: the label IS the
    message sent on tap, so half a sentence would send half a sentence."""
    long = "x" * 200
    out = offer_options(db, "s", options=["Yes", long, "No"])
    assert out["options"] == ["Yes", "No"]


def test_more_than_four_are_capped(db):
    out = offer_options(db, "s", options=["a", "b", "c", "d", "e", "f"])
    assert out["options"] == ["a", "b", "c", "d"]


def test_non_strings_are_ignored_rather_than_crashing(db):
    out = offer_options(db, "s", options=["Yes", 42, None, {"a": 1}, "No"])
    assert out["options"] == ["Yes", "No"]


def test_whitespace_is_normalised(db):
    assert _clean(["  Yes  ", "N o\n"]) == ["Yes", "N o"]


def test_arabic_labels_survive_intact(db):
    """The agent writes chips in the language it is speaking; nothing here may
    mangle them."""
    out = offer_options(db, "s", options=["نعم", "لا"])
    assert out["options"] == ["نعم", "لا"]
