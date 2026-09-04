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


# --------------------------------------------------------------------------- #
# THE THIRD TERMINAL (2026-09-04): a turn with nothing left to say
#
# Seen live on the test VM, twice inside one booking, and both times the model
# did the same thing. It asked the customer a question, the tool it called
# alongside came back with nothing worth repeating, and the graph handed it
# another generation — so it wrote the CUSTOMER'S reply and acted on it:
#
#   "Whereabouts are you? …" / "Dubai Marina" / "Jetour Al Quoz is your nearest,
#   about 14 km away."   -> nearest_showroom{location: "Dubai Marina"}, a place
#                           nobody had named
#   "May we send you WhatsApp confirmations?" / "Yes please" / "I'll need your
#   name and mobile."    -> record_consent{messaging: true}, a decision nobody
#                           had made
#
# THAT BOX WAS ALREADY RUNNING THE ALREADY-SAID BLOCK. Handing her own sentences
# back stops her REPEATING a question; it does not stop her answering one. And
# offer_options closes neither hole — its schema forbids it on open questions
# ("a name, a mobile number, an address, a landmark") and it is alone-only, so a
# question sharing a message with a write cannot use it. Hence a terminal keyed
# on the RESULT rather than on the tool the model chose.
# --------------------------------------------------------------------------- #
from app.agent import build_config, build_graph, new_sqlite_checkpointer  # noqa: E402
from app.models import Showroom  # noqa: E402
from app.tools.location_tools import nearest_showroom  # noqa: E402
from app.tools.slot_tools import acknowledge_licence_reminder  # noqa: E402


class _Scripted:
    """Replays canned assistant messages and COUNTS how many it was asked for.

    The count is the assertion in every graph test below: the bug is not what the
    model says, it is being asked to say anything at all.
    """

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.calls = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        assert self.calls < len(self.steps), "the graph asked for a generation it should not have"
        step = self.steps[self.calls]
        self.calls += 1
        return step


def _says_then_calls(text: str | None, *tools: tuple[str, dict]) -> dict:
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for i, (name, inp) in enumerate(tools):
        content.append({"type": "tool_use", "id": f"u{i}", "name": name, "input": inp})
    return {"role": "assistant", "content": content}


def _run(db, session_id: str, first: str, steps: list[dict]) -> _Scripted:
    model = _Scripted(steps)
    graph = build_graph(new_sqlite_checkpointer())
    graph.invoke(
        {"messages": [{"role": "user", "content": first}], "iterations": 0, "customer_id": None},
        build_config(db, session_id, generate=model),
    )
    return model


def _place_the_branches(db, monkeypatch) -> None:
    """The last two ask_for_landmark doors are reached only once the branches can
    be ranked, so the fixture's showrooms need coordinates and the Salesforce
    lookup needs to stay offline (it fails open to these anyway)."""
    from app.services import salesforce

    monkeypatch.setattr(salesforce, "showroom_coords", lambda: {})
    for i, s in enumerate(db.query(Showroom).all()):
        s.latitude, s.longitude = 25.10 + i * 0.05, 55.15 + i * 0.05
    db.flush()


# --- the producers: which results are silent -------------------------------- #
def test_a_location_we_cannot_place_is_waiting_on_the_customer(db):
    """The live failure's first tool call: nearest_showroom with no location.

    The result is not an answer, it is a request for one — the customer has to
    say where they are, and nothing in it can be said to them.
    """
    out = nearest_showroom(db, "sess-loc", vehicle_id=None)
    assert out["ask_for_landmark"] is True
    assert out["reason"] == "no_location"
    assert out["awaiting_customer"] is True


def test_an_ungeocodable_place_is_waiting_too(db, monkeypatch):
    from app.services import maps

    monkeypatch.setattr(maps, "geocode", lambda _q: None)
    out = nearest_showroom(db, "sess-loc", location="somewhere that is not a place")
    assert out["reason"] == "could_not_locate"
    assert out["awaiting_customer"] is True


def test_a_bare_city_is_waiting_too(db, monkeypatch):
    """The third door into the same result — a city geocodes, but not precisely."""
    from app.services import maps

    _place_the_branches(db, monkeypatch)
    monkeypatch.setattr(
        maps, "geocode",
        lambda _q: {"lat": 25.2, "lng": 55.27, "formatted": "Dubai", "precise": False, "emirate": "dubai"},
    )
    out = nearest_showroom(db, "sess-loc", location="Dubai")
    assert out["reason"] == "need_landmark"
    assert out["awaiting_customer"] is True


def test_a_placed_location_is_not_waiting(db, monkeypatch):
    """The other side of the guard: a real answer must still get its generation."""
    from app.services import maps

    _place_the_branches(db, monkeypatch)
    monkeypatch.setattr(
        maps, "geocode",
        lambda _q: {"lat": 25.08, "lng": 55.14, "formatted": "Dubai Marina", "precise": True, "emirate": "dubai"},
    )
    monkeypatch.setattr(maps, "drive_times", lambda _o, dests: [None for _ in dests])
    out = nearest_showroom(db, "sess-loc", location="Dubai Marina")
    assert out["resolved"] is True
    assert "awaiting_customer" not in out


def test_the_licence_acknowledgement_has_nothing_to_say(db, monkeypatch):
    """A server-side flag on the held slot. Not one word of it is for the
    customer, and the message that calls it carries the consent question."""
    from app.tools import slot_tools

    monkeypatch.setattr(
        slot_tools.slots, "acknowledge_licence_reminder",
        lambda _db, session_id: {"acknowledged": True, "slot_id": 7},
    )
    assert acknowledge_licence_reminder(db, "sess-ack")["awaiting_customer"] is True


def test_a_missing_slot_still_needs_a_generation(db, monkeypatch):
    """no_locked_slot is an INSTRUCTION to the model — lock, remind, call again —
    so it must not end the turn."""
    from app.tools import slot_tools

    monkeypatch.setattr(
        slot_tools.slots, "acknowledge_licence_reminder",
        lambda _db, session_id: {"acknowledged": False, "reason": "no_locked_slot"},
    )
    assert "awaiting_customer" not in acknowledge_licence_reminder(db, "sess-ack")


# --- the terminal, through the real compiled graph -------------------------- #
def test_the_turn_ends_when_the_only_tool_had_nothing_to_say(db):
    """THE LIVE BUG. Step two is the model writing the customer's line; if the
    graph ever asks for it, this fails on the call count."""
    model = _run(
        db, "sess-await-1", "Visit a showroom",
        [
            _says_then_calls("Whereabouts are you? Share a nearby landmark.", ("nearest_showroom", {})),
            _says_then_calls("Dubai Marina", ("nearest_showroom", {"location": "Dubai Marina"})),
        ],
    )
    assert model.calls == 1, "the model got a generation it could only fill with the customer's answer"


def test_a_silent_result_beside_an_informative_one_still_gets_its_generation(db):
    """ALL, not ANY. One tool with nothing to say among tools that do is normal —
    the model has a real answer to give and must be allowed to give it."""
    model = _run(
        db, "sess-await-2", "Which showroom is closest?",
        [
            _says_then_calls(
                "Let me look.",
                ("nearest_showroom", {}),
                ("get_vehicle_details", {"vehicle_id": 999999}),
            ),
            _says_then_calls("Here is what I found."),
        ],
    )
    assert model.calls == 2


def test_a_turn_that_has_said_nothing_is_never_ended(db):
    """Ending here would leave the customer an empty bubble the client drops —
    silence, where the whole point is that the question is already on screen."""
    model = _run(
        db, "sess-await-3", "Visit a showroom",
        [
            _says_then_calls(None, ("nearest_showroom", {})),
            _says_then_calls("Whereabouts are you?"),
        ],
    )
    assert model.calls == 2
