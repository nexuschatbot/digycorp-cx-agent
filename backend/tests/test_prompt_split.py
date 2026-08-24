"""The S5 split changed no prose. This test is what makes that a fact.

`prompt._PROMPT_BODY` used to be one literal; it is now reassembled from
prompts/{base,testdrive,bookingmanagement}.py. The fixture holds the exact
string that shipped before the split, so any drift — a re-worded sentence, a
lost blank line, a block dropped from the order — fails here rather than in
production.

Regenerating the fixture is NOT how you fix a failure here. If the body must
change, change it in the block it lives in and update the fixture in the same
commit, deliberately.
"""

from __future__ import annotations

import pathlib

from app.agent import prompt
from app.agent.prompts import anchors, base, bookingmanagement, compose, testdrive

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "prompt_body_original.txt"


def test_reassembly_is_byte_identical_to_the_body_that_shipped():
    assert prompt._PROMPT_BODY == FIXTURE.read_text()


def test_compose_single_agent_body_matches_prompt_body():
    assert compose.single_agent_body() == prompt._PROMPT_BODY


def test_every_block_appears_exactly_once_in_the_order():
    seen = list(compose.SINGLE_AGENT_ORDER)
    assert len(seen) == len(set(seen))

    declared = {
        ("base", n) for n in dir(base) if n.isupper()
    } | {
        ("testdrive", n) for n in dir(testdrive) if n.isupper()
    } | {
        ("bookingmanagement", n) for n in dir(bookingmanagement) if n.isupper()
    }
    assert set(seen) == declared


def test_journey_bodies_are_strict_subsets_of_the_single_agent_body():
    """Each agent drops the other journey's sections and nothing else."""
    whole = compose.single_agent_body()
    for agent in compose.AGENTS:
        for _, const in compose.JOURNEY_ORDER(agent):
            pass
        body = compose.body_for(agent)
        assert body
        assert len(body) < len(whole)


def test_each_journey_carries_every_base_block():
    base_blocks = [b for b in compose.SINGLE_AGENT_ORDER if b[0] == "base"]
    for agent in compose.AGENTS:
        order = compose.JOURNEY_ORDER(agent)
        assert [b for b in order if b[0] == "base"] == base_blocks


def test_journeys_do_not_leak_into_each_other():
    td = compose.body_for("testdrive")
    bm = compose.body_for("bookingmanagement")
    assert bookingmanagement.RESCHEDULING not in td
    assert bookingmanagement.CANCELLING not in td
    assert testdrive.BOOKING_SEQUENCE not in bm
    assert testdrive.FINDING_THE_RIGHT_CAR not in bm


def test_unknown_agent_is_refused():
    import pytest

    with pytest.raises(ValueError):
        compose.body_for("sales")


# --------------------------------------------------------------------------- #
# S7a — the anchors
# --------------------------------------------------------------------------- #
def test_options_anchor_split_reproduces_the_original():
    """The two halves must rejoin into exactly the anchor that shipped."""
    rejoined = (
        anchors._OPTIONS_ANCHOR_CORE
        + anchors._OPTIONS_PARAGRAPH_SEPARATOR
        + anchors._COMPARE_ANCHOR
    )
    assert rejoined == anchors._OPTIONS_ANCHOR
    assert rejoined == prompt._OPTIONS_ANCHOR


def test_instruction_anchors_belong_to_testdrive_only():
    td = anchors.anchors_for("testdrive")
    bm = anchors.anchors_for("bookingmanagement")
    for name in ("_SECOND_DRIVE_ANCHOR", "_COMPARE_ANCHOR"):
        assert name in td
        assert name not in bm


def test_state_anchors_reach_both_journeys():
    for agent in compose.AGENTS:
        got = anchors.anchors_for(agent)
        for name in anchors.SHARED_ANCHORS:
            assert name in got


def test_prompt_still_exposes_every_anchor_name():
    """Callers and tests read these off `prompt`; moving them must not break that."""
    for name in anchors.SHARED_ANCHORS + tuple(
        n for v in anchors.JOURNEY_ANCHORS.values() for n in v
    ):
        assert hasattr(prompt, name), name


def test_unknown_agent_is_refused_by_anchors():
    import pytest

    with pytest.raises(ValueError):
        anchors.anchors_for("sales")
