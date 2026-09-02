"""The pre-fetch's vehicle hint and answer-shape gate (2026-09-02).

Two measured problems with the first RETRIEVAL_FIRST cut. A pronoun question
("how much does it cost") searched the index with no car in it and found the
wrong car 82% of the time; and answers — names, numbers, chip taps, "yes" —
paid for a search that could only attach noise. Both fixes are deterministic
and offline; every message below is a real one from the test VM's transcripts.
"""

from __future__ import annotations

import pytest

from app.agent import graph
from app.retrieval import index, vehicle_mentions

NAMES = [
    "Jetour G700", "Jetour Dashing", "Jetour T1", "Jetour T1 i-DM",
    "Jetour T2 Luxury Plus", "Jetour T2 i-DM", "Jetour X50", "Jetour X70 FL", "Jetour X70 FL Plus",
]


@pytest.fixture(autouse=True)
def _names(monkeypatch):
    monkeypatch.setattr(vehicle_mentions, "model_names", lambda db=None: list(NAMES))
    vehicle_mentions.reset()
    yield
    vehicle_mentions.reset()


# --------------------------------------------------------------------------- #
# Name matching
# --------------------------------------------------------------------------- #
def test_aliases_cover_the_spellings_customers_use():
    assert {"jetour t2 i-dm", "t2 i-dm", "t2 idm", "t2 i dm"} <= vehicle_mentions.aliases_for("Jetour T2 i-DM")
    assert {"x70 fl plus", "x70 plus"} <= vehicle_mentions.aliases_for("Jetour X70 FL Plus")


def test_the_longest_alias_wins():
    assert vehicle_mentions.mentions("what about the t2 idm") == ["Jetour T2 i-DM"]
    assert vehicle_mentions.mentions("the X70 FL and the x70 plus") == ["Jetour X70 FL", "Jetour X70 FL Plus"]
    assert vehicle_mentions.mentions("what is its battery type") == []


def test_last_mentioned_reads_both_sides_of_the_conversation():
    history = [
        {"role": "user", "content": "What is fuel average of T1 i-DM"},
        {"role": "assistant", "content": [{"type": "text", "text": "The T1 i-DM does about 18 km/l."}]},
        {"role": "user", "content": "let me know more about the engine"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "emit_briefs", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "Jetour X50 evidence"}]},
    ]
    # Tool results are not conversation; the last NAMED car is the T1 i-DM.
    assert vehicle_mentions.last_mentioned(history) == "Jetour T1 i-DM"
    assert vehicle_mentions.last_mentioned([]) is None


def test_no_catalogue_means_no_hint_and_no_error(monkeypatch):
    monkeypatch.setattr(vehicle_mentions, "model_names", lambda db=None: [])
    vehicle_mentions.reset()
    assert vehicle_mentions.mentions("dashing colours") == []


# --------------------------------------------------------------------------- #
# The answer-shape gate — real VM messages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,why", [
    ("Abraham , 0445534231", "phone"),
    ("bilal ahmed \n0987678844", "phone"),
    ("my name is Muhammad and number is 0556046430", "phone"),
    ("I’d like to book a test drive.", "chip"),
    ("I'll take Wednesday 2 September at 9:00 am at Jetour Deira.", "chip"),
    ("Yes, that's correct", "affirmation"),
    ("No, I'm someone else", "affirmation"),
    ("Yes, send me updates", "affirmation"),
    ("no, thank you", "affirmation"),
    ("Jetour t2 luxury plus", "vehicle name only"),
    ("The petrol T2 Luxury Plus", "vehicle name only"),
])
def test_answers_are_recognised(text, why):
    assert graph._looks_like_answer(text) == why


@pytest.mark.parametrize("text", [
    "yes, what accessories are available for this model?",
    "what are the colors available in it",
    "Hi, what colors do you have for the dashing",
    "is there any promotion or offers",
    "I want to know about warranty of both options",
    "can you compare all the suggested 3 options for me",
    "what is fuel average of T1 i-DM",
])
def test_questions_are_not(text):
    assert graph._looks_like_answer(text) is None


def _state(text: str, history: list | None = None) -> dict:
    return {"messages": [*(history or []), {"role": "user", "content": text}]}


def test_answers_never_reach_the_index(monkeypatch):
    def boom(q, **k):
        raise AssertionError(f"an answer reached the index: {q!r}")

    monkeypatch.setattr(index, "search", boom)
    assert graph._prefetch_passages(_state("Yes, that's correct")) is None
    assert graph._prefetch_passages(_state("bilal ahmed 0987678844")) is None
    assert graph._prefetch_passages(_state("what colours does it come in"), slot_pinned=True) is None


# --------------------------------------------------------------------------- #
# The hint in the query
# --------------------------------------------------------------------------- #
def test_a_pronoun_question_carries_the_last_named_car(monkeypatch):
    seen = []
    monkeypatch.setattr(index, "search", lambda q, **k: seen.append(q) or [])
    history = [
        {"role": "user", "content": "Hi, what colours do you have for the Dashing?"},
        {"role": "assistant", "content": [{"type": "text", "text": "The Dashing comes in six colours."}]},
    ]
    graph._prefetch_passages(_state("how much does it cost", history))
    assert seen == ["how much does it cost Jetour Dashing"]


def test_a_question_that_names_a_car_is_sent_as_typed(monkeypatch):
    seen = []
    monkeypatch.setattr(index, "search", lambda q, **k: seen.append(q) or [])
    history = [{"role": "user", "content": "tell me about the Dashing"}]
    graph._prefetch_passages(_state("and what colours does the X50 come in", history))
    assert seen == ["and what colours does the X50 come in"]


def test_no_history_means_no_hint(monkeypatch):
    seen = []
    monkeypatch.setattr(index, "search", lambda q, **k: seen.append(q) or [])
    graph._prefetch_passages(_state("which one is the seven seater"))
    assert seen == ["which one is the seven seater"]
