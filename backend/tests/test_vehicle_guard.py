"""The vehicle_id guard (2026-09-03).

REPRODUCES THE LIVE FAILURE FIRST. A customer opened with "i want to book a
test drive of g700" and every tool call carried vehicle_id 6 — the Dashing.
test_live_failure_shape_is_refused is that transcript, verbatim, and it fails
against the code as it stood.

The rest of the file is about what the guard must NOT do. It is a refusal in
the middle of a read the customer is waiting on, so a false positive costs a
working booking: mid-conversation car switches, cars Aisha named herself, an
empty transcript, an unparseable id and a broken catalogue all have to pass
straight through.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ConversationTurn, Language, Speaker, VehicleModel
from app.services import vehicle_guard
from app.tools.location_tools import nearest_showroom
from app.tools.slot_tools import get_available_slots

SESSION = "guard-session"


def _say(db, speaker: Speaker, text: str) -> None:
    db.add(ConversationTurn(
        session_id=SESSION, speaker=speaker, lang=Language.en, text=text,
    ))
    db.commit()


def _id_of(db, name: str) -> int:
    return db.scalars(select(VehicleModel).where(VehicleModel.name == name)).one().id


# --------------------------------------------------------------------------- #
# The failure this exists for
# --------------------------------------------------------------------------- #
def test_live_failure_shape_is_refused(slots):
    """"book a test drive of g700" then a Dashing id — the 2026-09-03 bug."""
    _say(slots, Speaker.customer, "i want to book a test drive of g700")
    dashing = _id_of(slots, "Jetour Dashing")

    out = get_available_slots(slots, SESSION, vehicle_id=dashing)

    assert out["error"] == "vehicle_id_not_discussed"
    assert out["slots"] == []
    assert out["vehicle_name"] == "Jetour Dashing"
    assert out["discussed"] == ["Jetour G700"]
    # The recovery, not just the reason — see orchestrator/write's next_step.
    assert "Jetour G700" in out["detail"]
    assert "get_vehicle_details" in out["detail"]


def test_the_right_id_still_answers(slots):
    """The same transcript with the id the customer actually meant."""
    _say(slots, Speaker.customer, "i want to book a test drive of g700")
    out = get_available_slots(slots, SESSION, vehicle_id=_id_of(slots, "Jetour G700"))
    assert "error" not in out
    assert out["slots"]


def test_nearest_showroom_refuses_too(db):
    """Where the wrong id entered the live conversation, one call earlier."""
    _say(db, Speaker.customer, "i want to book a test drive of g700")
    out = nearest_showroom(
        db, SESSION, location="Al Quoz", vehicle_id=_id_of(db, "Jetour Dashing"),
    )
    assert out["resolved"] is False
    assert out["reason"] == "vehicle_id_not_discussed"
    assert "Jetour G700" in out["detail"]


def test_refusal_precedes_the_geocode(db, monkeypatch):
    """A wrong car is refused without spending a maps call on it."""
    from app.services import maps

    def _boom(*a, **k):  # pragma: no cover — asserted by not being reached
        raise AssertionError("geocode ran on a refused vehicle_id")

    monkeypatch.setattr(maps, "geocode", _boom)
    _say(db, Speaker.customer, "tell me about the g700")
    out = nearest_showroom(
        db, SESSION, location="Al Quoz", vehicle_id=_id_of(db, "Jetour Dashing"),
    )
    assert out["reason"] == "vehicle_id_not_discussed"


# --------------------------------------------------------------------------- #
# What it must not do
# --------------------------------------------------------------------------- #
def test_a_mid_conversation_switch_is_allowed(slots):
    """Customers change their mind; the set is every car named, not the last."""
    _say(slots, Speaker.customer, "tell me about the g700")
    _say(slots, Speaker.customer, "actually, show me the Dashing instead")
    out = get_available_slots(slots, SESSION, vehicle_id=_id_of(slots, "Jetour Dashing"))
    assert "error" not in out


def test_a_car_only_aisha_named_is_allowed(slots):
    """Either side speaking counts — she recommends cars the customer never
    typed, and refusing those would break every recommendation flow."""
    _say(slots, Speaker.customer, "something big for the family")
    _say(slots, Speaker.agent, "The Jetour G700 would suit you beautifully.")
    out = get_available_slots(slots, SESSION, vehicle_id=_id_of(slots, "Jetour G700"))
    assert "error" not in out


def test_silent_when_no_car_was_ever_named(slots):
    """A transcript with no model in it proves nothing, so it refuses nothing.

    The id may have come from a search result the transcript never quotes.
    """
    _say(slots, Speaker.customer, "i want to book a test drive")
    out = get_available_slots(slots, SESSION, vehicle_id=_id_of(slots, "Jetour Dashing"))
    assert "error" not in out


def test_silent_on_an_empty_session(slots):
    """No turns at all — the API and the tests both reach the tools this way."""
    out = get_available_slots(slots, "no-such-session", vehicle_id=1)
    assert "error" not in out


@pytest.mark.parametrize("bad", ["not-an-int", None, 9999])
def test_existing_validators_still_own_their_errors(slots, bad):
    """An unparseable or unknown id is not this guard's to answer."""
    _say(slots, Speaker.customer, "tell me about the g700")
    out = get_available_slots(slots, SESSION, vehicle_id=bad)
    assert out.get("error") != "vehicle_id_not_discussed"


def test_guards_without_a_process_catalogue(slots, monkeypatch):
    """The names must come from the caller's Session, not from SessionLocal.

    THIS IS THE CI FAILURE (2026-09-03). vehicle_mentions.mentions() defaults
    to opening its own SessionLocal; CI has no elite_demo.db, so it matched
    nothing, and all three guard assertions failed on a build whose full suite
    passed on the developer's machine. A guard that only works where a stray
    database file happens to exist is not a guard.
    """
    from app.retrieval import vehicle_mentions

    def _no_process_catalogue():  # what an unopenable database looks like
        return (None, {})

    monkeypatch.setattr(vehicle_mentions, "_current", _no_process_catalogue)
    _say(slots, Speaker.customer, "i want to book a test drive of g700")

    out = get_available_slots(slots, SESSION, vehicle_id=_id_of(slots, "Jetour Dashing"))
    assert out["error"] == "vehicle_id_not_discussed"
    assert out["discussed"] == ["Jetour G700"]


def test_a_broken_catalogue_never_costs_the_read(db, monkeypatch):
    """The guard fails open — the customer's turn matters, the check does not."""
    from app.retrieval import vehicle_mentions

    def _boom(*a, **k):
        raise RuntimeError("catalogue unavailable")

    monkeypatch.setattr(vehicle_mentions, "mentions", _boom)
    _say(db, Speaker.customer, "tell me about the g700")
    assert vehicle_guard.contradiction(db, SESSION, _id_of(db, "Jetour Dashing")) is None


# --------------------------------------------------------------------------- #
# The sentence handed to the agent
# --------------------------------------------------------------------------- #
def test_detail_names_every_car_discussed():
    clash = {"passed": 6, "passed_name": "Jetour Dashing",
             "discussed": ["Jetour G700", "Jetour T2 i-DM"]}
    said = vehicle_guard.detail(clash)
    assert "Jetour G700" in said and "Jetour T2 i-DM" in said
    assert "unavailable" in said          # the thing it must not say
