"""Reload-resume proof: the transcript read + the direct-path close (rule 19).

Persisting session_id in localStorage means a page reload resumes the SAME
conversation. These lock in the two halves that makes possible:

  - conversation.history() returns the text transcript in order, plus whether
    the session has closed (so a closed chat resumes VISIBLY ended);
  - the direct path (POST .../close) goes through the SAME service as the
    agent's tool, so "New chat" releases a held slot instead of stranding it —
    and is audited as the CUSTOMER, because a person pressed the button.

The zero-turns case is deliberately NOT an error: an unknown session returns an
empty transcript, which is the frontend's signal to rotate to a fresh id. That
guard is what stops a wiped app DB + surviving checkpoint file from showing an
empty thread to a customer while the agent still remembers everything.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import app
from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    ConversationTurn,
    Language,
    Slot,
    SlotStatus,
    Speaker,
)
from app.services import conversation, slots as slots_service

SESSION = "sess-resume-19"


def _client(db) -> TestClient:
    """A TestClient whose routes use the test's in-memory Session."""
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    yield_client = client
    return yield_client


def _add_turns(db, session_id: str = SESSION) -> None:
    db.add_all(
        [
            ConversationTurn(
                session_id=session_id, speaker=Speaker.customer,
                lang=Language.en, text="Tell me about the Jetour T2",
            ),
            ConversationTurn(
                session_id=session_id, speaker=Speaker.agent,
                lang=Language.en, text="It's a mid-size 5-seater SUV.",
            ),
            ConversationTurn(
                session_id=session_id, speaker=Speaker.customer,
                lang=Language.ar, text="أبغى أحجز تجربة قيادة",
            ),
        ]
    )
    db.commit()


# --------------------------------------------------------------------------- #
# history() — the transcript behind reload-resume
# --------------------------------------------------------------------------- #
def test_history_returns_turns_in_order_with_language(db):
    _add_turns(db)

    result = conversation.history(db, session_id=SESSION)

    assert result["session_id"] == SESSION
    assert result["status"] == "active"
    assert [t["speaker"] for t in result["turns"]] == ["customer", "agent", "customer"]
    assert result["turns"][0]["text"] == "Tell me about the Jetour T2"
    # Native script preserved, per-turn language kept — an Arabic turn must
    # rehydrate as Arabic, not as a translated or direction-less string (LOC-01).
    assert result["turns"][2]["lang"] == "ar"
    assert result["turns"][2]["text"] == "أبغى أحجز تجربة قيادة"
    assert result["turns"][0]["ts"] is not None


def test_history_reports_a_closed_session_as_closed(db):
    """A closed conversation must resume VISIBLY ended — history carries the flag
    so the UI can show the transcript AND the ended state."""
    _add_turns(db)
    assert conversation.history(db, session_id=SESSION)["status"] == "active"

    conversation.close(db, session_id=SESSION)
    db.commit()

    assert conversation.history(db, session_id=SESSION)["status"] == "closed"


def test_history_of_an_unknown_session_is_empty_not_an_error(db):
    """THE ZERO-TURNS GUARD's server half. A stale session_id (wiped DB, old
    phone) returns an empty transcript rather than 404 — the frontend reads that
    as 'unusable, start fresh' and rotates, so the visible thread and the model's
    memory can never disagree."""
    result = conversation.history(db, session_id="never-existed")

    assert result["turns"] == []
    assert result["status"] == "active"


def test_history_does_not_write_anything(db):
    """Read-only means read-only: no audit row, no turn, no side effect."""
    _add_turns(db)
    before = db.scalar(select(AuditEvent).where(AuditEvent.session == SESSION))
    conversation.history(db, session_id=SESSION)
    after = db.scalars(select(AuditEvent).where(AuditEvent.session == SESSION)).all()

    assert before is None
    assert after == []


# --------------------------------------------------------------------------- #
# The direct path — "New chat" closes without an LLM
# --------------------------------------------------------------------------- #
def test_close_records_who_closed_it(db):
    """Two callers, two honest answers. The agent's farewell is actor=agent; a
    customer tapping New chat is actor=customer. The trail should not credit a
    button press to Aisha."""
    conversation.close(db, session_id=SESSION, actor=Actor.customer)
    db.commit()

    row = db.scalars(
        select(AuditEvent).where(
            AuditEvent.session == SESSION, AuditEvent.type == AuditType.intent
        )
    ).one()
    assert row.actor == Actor.customer
    assert row.payload["action"] == conversation.CLOSE_ACTION


def test_new_chat_releases_a_held_slot_via_the_route(slots, open_slot):
    """The requirement in one test: pressing New chat mid-booking must not cost
    inventory any more than an idle close does. The route calls the SAME service
    as the agent's tool, which is why this works rather than needing its own
    release logic."""
    slots_service.lock(slots, session_id=SESSION, slot_id=open_slot.id)
    slots.commit()
    assert slots.get(Slot, open_slot.id).status == SlotStatus.locked

    app.dependency_overrides[get_db] = lambda: slots
    try:
        res = TestClient(app).post(f"/api/conversation/{SESSION}/close")
    finally:
        app.dependency_overrides.clear()

    assert res.status_code == 200
    assert res.json()["closed"] is True
    slots.expire_all()
    freed = slots.get(Slot, open_slot.id)
    assert freed.status == SlotStatus.open
    assert freed.locked_by_session is None


def test_routes_round_trip(db):
    """GET the transcript, POST the close, GET again — the exact sequence a
    reload followed by New chat performs."""
    _add_turns(db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)

        got = client.get(f"/api/conversation/{SESSION}").json()
        assert got["status"] == "active"
        assert len(got["turns"]) == 3

        closed = client.post(f"/api/conversation/{SESSION}/close").json()
        assert closed == {"closed": True, "already_closed": False, "released_locks": 0}

        # Idempotent: New chat pressed twice, or a retried request.
        again = client.post(f"/api/conversation/{SESSION}/close").json()
        assert again["already_closed"] is True

        # ...and the transcript now resumes as CLOSED, history intact.
        after = client.get(f"/api/conversation/{SESSION}").json()
        assert after["status"] == "closed"
        assert len(after["turns"]) == 3
    finally:
        app.dependency_overrides.clear()


def test_unknown_session_route_returns_empty_not_404(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        res = TestClient(app).get("/api/conversation/never-existed")
    finally:
        app.dependency_overrides.clear()

    assert res.status_code == 200
    assert res.json()["turns"] == []


# --------------------------------------------------------------------------- #
# Vehicle-card anchors — the cars survive a reload (rule 18)
# --------------------------------------------------------------------------- #
def test_history_replays_vehicle_card_anchors(db):
    """A turn that showed cards says so, as IDS. The frontend resolves them
    through GET /api/vehicles/{id}, so a reloaded card carries live catalogue
    data rather than a frozen — and eventually wrong — copy."""
    db.add_all(
        [
            ConversationTurn(
                session_id=SESSION, speaker=Speaker.customer,
                lang=Language.en, text="Tell me about the T2",
            ),
            ConversationTurn(
                session_id=SESSION, speaker=Speaker.agent,
                lang=Language.en, text="Here it is.",
                cards=[{"vehicle_ids": [2], "variant": "full"}],
            ),
        ]
    )
    db.commit()

    turns = conversation.history(db, session_id=SESSION)["turns"]

    # Always present, so the client never distinguishes "no cards" from a row
    # written before the column existed.
    assert turns[0]["cards"] == []
    assert turns[1]["cards"] == [{"vehicle_ids": [2], "variant": "full"}]


def test_slot_chips_are_still_never_replayed(db):
    """The asymmetry, locked in. Cards come back; chips must not — a rehydrated
    chip pins a slot_id that may have been booked by someone else since, which is
    the staleness the 'only the newest offer is tappable' rule already prevents.
    A vehicle_id has no such failure mode."""
    _add_turns(db)

    turns = conversation.history(db, session_id=SESSION)["turns"]

    # `audio_id` joined the shape with voice notes (2026-07-21) and belongs to
    # the same family as `cards`: a POINTER the client resolves live, not a
    # snapshot. It is listed here rather than allowed through loosely, because
    # the point of this assertion is that the replay shape is CLOSED — the next
    # field to appear should have to argue for itself here too.
    assert all(
        set(t) == {"turn_id", "speaker", "lang", "text", "ts", "cards", "audio_id"}
        for t in turns
    )
    assert all("slots" not in t and "slot_id" not in t for t in turns)


# --------------------------------------------------------------------------- #
# _without_drawn — trims a row to the cars it actually adds (2026-08-31)
# --------------------------------------------------------------------------- #
def test_a_row_is_trimmed_to_the_cars_it_actually_adds():
    """FOUND LIVE. One Sales brief fired three searches and each drew its own
    row — [7,8,1], [8,6,7], [1,3,2]. Nine cards for one question, three cars
    shown twice, under a reply that described three."""
    from app.agent.runner import _card_anchor, _without_drawn

    out = {"matches": [{"vehicle_id": 8}, {"vehicle_id": 6}, {"vehicle_id": 7}]}
    trimmed = _without_drawn("search_vehicles", out, {7, 8, 1})
    assert _card_anchor("search_vehicles", trimmed) == {
        "vehicle_ids": [6], "variant": "compact"
    }
    # The ORIGINAL is untouched — the caller still owns what the tool returned.
    assert len(out["matches"]) == 3


def test_a_row_of_nothing_new_trims_to_nothing():
    from app.agent.runner import _card_anchor, _without_drawn

    out = {"matches": [{"vehicle_id": 7}, {"vehicle_id": 8}]}
    assert _card_anchor("search_vehicles", _without_drawn(
        "search_vehicles", out, {7, 8})) is None


def test_trimming_never_invents_or_reorders():
    """Entries are dropped, never added or moved, so what renders is still a
    subset of what the tool returned."""
    from app.agent.runner import _without_drawn

    out = {"matches": [{"vehicle_id": 3}, {"vehicle_id": 9}, {"vehicle_id": 4}]}
    kept = _without_drawn("search_vehicles", out, {9})["matches"]
    assert [v["vehicle_id"] for v in kept] == [3, 4]


def test_nothing_drawn_yet_leaves_the_payload_alone():
    from app.agent.runner import _without_drawn

    out = {"matches": [{"vehicle_id": 3}]}
    assert _without_drawn("search_vehicles", out, set()) is out


def test_a_tool_that_draws_no_vehicle_cards_is_untouched():
    from app.agent.runner import _without_drawn

    out = {"bookings": [{"booking_ref": "TD-2026-0142"}]}
    assert _without_drawn("get_booking", out, {7, 8}) is out


# --------------------------------------------------------------------------- #
# _card_anchor — must mirror extractCards/extractSearchCards in the FE cards.ts
# --------------------------------------------------------------------------- #
def test_card_anchor_mirrors_the_frontend_extractors():
    """These two implementations are one contract in two languages: if they
    disagree, a reload draws a different set of cards than the turn originally
    showed — the single thing this feature exists to prevent."""
    from app.api.chat import _card_anchor

    # get_vehicle_details: the single hit and the ambiguous multi-variant shape.
    assert _card_anchor("get_vehicle_details", {"vehicle": {"vehicle_id": 2}}) == {
        "vehicle_ids": [2], "variant": "full",
    }
    assert _card_anchor(
        "get_vehicle_details", {"ambiguous": True, "candidates": [{"vehicle_id": 2}, {"vehicle_id": 3}]}
    ) == {"vehicle_ids": [2, 3], "variant": "full"}

    # search_vehicles: capped at 3 like MAX_RECOMMENDATION_CARDS, and falling
    # back to nearest_alternatives exactly when there are no matches.
    assert _card_anchor(
        "search_vehicles", {"matches": [{"vehicle_id": i} for i in range(5)]}
    ) == {"vehicle_ids": [0, 1, 2], "variant": "compact"}
    assert _card_anchor(
        "search_vehicles", {"matches": [], "nearest_alternatives": [{"vehicle_id": 9}]}
    ) == {"vehicle_ids": [9], "variant": "compact"}


def test_card_anchor_records_nothing_when_no_card_was_drawn():
    """Shapes that drew no card live must draw none on reload.

    The price_unknown case left here on 2026-07-27 with the nullable-price path.
    An UNRECOGNISED key is still the point being made, and is now covered by the
    made-up shape below: _card_anchor keys off the shapes it knows and records
    nothing for anything else, rather than guessing ids out of a payload it does
    not understand (FR-CX-07)."""
    from app.api.chat import _card_anchor

    assert _card_anchor("get_vehicle_details", {"error": "vehicle_not_found"}) is None
    assert _card_anchor("search_vehicles", {"some_future_bucket": [{"vehicle_id": 4}]}) is None
    assert _card_anchor("lock_slot", {"locked": True}) is None
    assert _card_anchor("get_available_slots", {"slots": [{"slot_id": 7}]}) is None
    # An unrecognised shape records nothing rather than inventing ids (FR-CX-07).
    assert _card_anchor("search_vehicles", "not a dict") is None
    assert _card_anchor("get_vehicle_details", {"vehicle": {"name": "T2"}}) is None
