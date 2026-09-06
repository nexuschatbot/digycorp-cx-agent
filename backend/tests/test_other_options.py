"""\"Any other option?\" — and the cards agreeing with the sentence beside them.

BOTH HALVES OF ONE LIVE FAILURE, session 9841adc7 on 2026-09-04. A customer
described what they needed, was shown three cars, and asked for something else:

    customer  any other option you have for me
    Aisha     [prose about the Jetour T1 i-DM]      cards: X70 FL Plus, X50, T2
    customer  I asked for other option you are suggesting me the same

Two defects, and they compound:

  THE SAME THREE. catalogue.search is deterministic and stateless, so the
  re-ask returned the identical spread. Six petrol models had matched and three
  were unreachable by construction — and with no feature_query there was no
  catalogue_digest either, so the payload held three cars and nothing else.

  THE PROSE AND THE PICTURES DISAGREED. Having nothing else to offer, she
  reached into the retrieval passages for the T1 i-DM — a PHEV her own
  fuel=petrol search had excluded — and described it beside pictures of three
  cars she never mentioned. The cascade path solves this with `cards_shown`;
  the path where she searches herself never got it.
"""

from __future__ import annotations

import json

from app.services import catalogue

# --------------------------------------------------------------------------- #
# The cars that fitted and are not on the cards
# --------------------------------------------------------------------------- #
def test_a_re_ask_can_reach_the_cars_that_were_held_back(db):
    """The exact query from the live session, and what it now also carries."""
    result = catalogue.search(db, fuel="petrol")

    carded = {m["vehicle_id"] for m in result["matches"]}
    others = {o["vehicle_id"] for o in result["other_matches"]}

    assert others, "there were more petrol cars; she must be able to name them"
    assert not (carded & others), "a car is either on the cards or held back"

    every_match = {v.id for v in catalogue._apply_filters(
        list(db.query(catalogue.VehicleModel).all()),
        body_type=None, min_seats=None, max_price_aed=None,
        fuel="petrol", brand=None,
    )}
    assert carded | others == every_match, "held back means matched, not invented"


def test_other_matches_is_absent_when_nothing_was_held_back(db):
    """Three or fewer fitted, so nothing was withheld and there is nothing to
    offer. The result keeps exactly the shape it always had."""
    result = catalogue.search(db, fuel="phev")

    assert len(result["matches"]) == 3
    assert "other_matches" not in result


def test_other_matches_carries_a_name_and_nothing_else(db):
    """NO PRICE, deliberately — the same withholding `_digest` relies on. She
    can name one of these; to say more she has to call get_vehicle_details,
    which re-grounds the claim and draws its card."""
    result = catalogue.search(db, fuel="petrol")

    for entry in result["other_matches"]:
        assert set(entry) == {"vehicle_id", "name"}


def test_the_cap_is_untouched_and_the_spread_is_still_deterministic(db):
    """The fix is what the payload CARRIES, not how many cards are drawn: the
    same question must still answer with the same three (FR-BK-09/10)."""
    first = catalogue.search(db, fuel="petrol")
    second = catalogue.search(db, fuel="petrol")

    assert len(first["matches"]) <= catalogue.MAX_RECOMMENDATIONS
    assert [m["vehicle_id"] for m in first["matches"]] == [
        m["vehicle_id"] for m in second["matches"]
    ]


def test_an_unresolvable_basis_offers_nothing_and_claims_nothing(db):
    """`offered` empty means the answer is the nearest alternatives, not a list
    of every car that filtered through — naming those would read as a shortlist
    that was never chosen."""
    result = catalogue.search(db, fuel="petrol", basis="efficiency")

    assert result["no_exact_match"] is True
    assert "other_matches" not in result


# --------------------------------------------------------------------------- #
# What the customer can see, told to the one who writes the sentence
# --------------------------------------------------------------------------- #
def _search(call_id: str, **args) -> dict:
    return {"type": "tool_use", "id": call_id, "name": "search_vehicles", "input": args}


def _run(db, monkeypatch, blocks: list, *, reads=True, multi=True):
    """One tool_node batch: what it told the model, and what it drew."""
    from app.agent import build_config
    from app.agent import tool_node as tool_node_mod

    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", reads)
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", multi)
    drawn: list = []
    monkeypatch.setattr(tool_node_mod, "_safe_writer", lambda: drawn.append)
    state = {
        "messages": [
            {"role": "user", "content": "something fuel efficient for the family"},
            {"role": "assistant", "content": blocks},
        ],
        "customer_id": None,
    }
    out = tool_node_mod.tool_node(state, build_config(db, "cards-shown-sess"))
    told = [
        json.loads(b["content"])
        for b in out["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    return told, drawn


def _rows(drawn: list) -> list:
    events = [
        e for e in drawn
        if e.get("type") == "tool_event" and e.get("tool") == "search_vehicles"
    ]
    return events


def test_her_own_search_names_the_cars_the_row_actually_drew(db, monkeypatch):
    told, drawn = _run(db, monkeypatch, [_search("t1", fuel="petrol")])

    shown = told[0]["cards_shown"]
    on_screen = _rows(drawn)[0]["output"]["matches"]
    assert [c["vehicle_id"] for c in shown] == [r["vehicle_id"] for r in on_screen]
    assert all(c["name"] for c in shown), "she has to be able to say them"


def test_the_browsers_copy_is_never_annotated(db, monkeypatch):
    """The streamed event draws the row and is what the reload anchor is built
    from; only the model's copy carries the note."""
    _told, drawn = _run(db, monkeypatch, [_search("t1", fuel="petrol")])

    assert "cards_shown" not in _rows(drawn)[0]["output"]


def test_two_searches_in_one_generation_are_told_the_one_merged_strip(db, monkeypatch):
    """One question draws one row, so both results must describe THAT row —
    each search's own matches would claim pictures nobody drew."""
    told, drawn = _run(db, monkeypatch, [
        _search("t1", min_seats=5, fuel="phev"),
        _search("t2", min_seats=5),
    ])
    events = _rows(drawn)
    assert len(events) == 1, "one question, one strip"
    strip = [r["vehicle_id"] for r in events[0]["output"]["matches"]]

    for entry in told:
        assert [c["vehicle_id"] for c in entry["cards_shown"]] == strip


def test_the_single_agent_path_is_left_byte_identical(db, monkeypatch):
    """That path is the kill switch and its card selection is deliberately
    frozen (OPEN-ISSUES #4) — the flag alone must not reach it."""
    told, _drawn = _run(db, monkeypatch, [_search("t1", fuel="petrol")], multi=False)

    assert "cards_shown" not in told[0]
    assert told[0]["matches"], "and it still gets a real search result"
