"""The bookings anchor (2026-07-27): the customer's LIVE bookings and any unconfirmed
hold, read from the DB and injected into the system prompt on every generation.

WHAT IT IS FOR. The model is sent only a trailing window of the conversation
(graph.TRIM_KEEP), and one exchange is three or four messages once tool_use and
tool_result blocks are counted. So a booking confirmed six exchanges earlier was
simply not in front of it, and live that produced the worst failure the demo has
had: minutes after confirming a second test drive, the agent told the customer
"I only have one booking in progress here". Separately, a reschedule the customer
had already agreed to was dropped because the conversation moved on before it was
executed, leaving the slot held and the booking untouched.

A booking is a FACT in a table, so the anchor reads the table rather than trusting
a sentence to survive the window — the same discipline as the date, identity and
language anchors.

OFFLINE ONLY (CLAUDE.md): pure functions and the real services against the
in-memory DB fixture. No model, no vendor call.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.agent.prompt import _render_bookings, system_prompt
from app.models import Slot, SlotStatus
from app.services import booking as booking_service
from app.services import customers, slots as slots_service

# Not "ALREADY HAS" any more (2026-08-03): this anchor is rebuilt every generation,
# so a booking created a second ago landed under a heading claiming the customer
# arrived with it. See test_a_booking_made_here_is_attributed_to_the_agent.
_ANCHOR_HEADER = "WHAT THIS CUSTOMER HAS WITH US RIGHT NOW"
MOBILE = "+971509990001"


def _one(ref: str, **over) -> dict:
    out = {
        "booking_ref": ref,
        "vehicle_name": "Jetour X50",
        "showroom_name": "Jetour Abu Dhabi (Rabdan)",
        "location_type": "showroom",
        "start_human": "Saturday, 1 August 2026 at 1 PM",
    }
    out.update(over)
    return out


# --------------------------------------------------------------------------- #
# 1. Nothing to say -> nothing appended. A browsing customer's prompt is
#    byte-identical to before this layer existed.
# --------------------------------------------------------------------------- #
def test_no_bookings_appends_nothing():
    bare = system_prompt()
    assert _ANCHOR_HEADER not in bare
    assert system_prompt(live_bookings=[], held_slot=None) == bare
    assert system_prompt(live_bookings=None, held_slot=None) == bare
    assert _render_bookings([], None) == ""


# --------------------------------------------------------------------------- #
# 2. THE REGRESSION THIS EXISTS FOR: two bookings are BOTH named, so the agent
#    can never again say it has "only one booking in progress".
# --------------------------------------------------------------------------- #
def test_two_bookings_are_both_named_in_the_prompt():
    prompt = system_prompt(
        live_bookings=[
            _one("TD-2026-10E9"),
            _one("TD-2026-4A2C", vehicle_name="Jetour X70 FL",
                 start_human="Saturday, 1 August 2026 at 6:30 PM"),
        ]
    )
    assert _ANCHOR_HEADER in prompt
    assert "TD-2026-10E9" in prompt
    assert "TD-2026-4A2C" in prompt
    assert "two bookings" in prompt
    # Both cars, so "my other car" resolves without another lookup.
    assert "Jetour X50" in prompt and "Jetour X70 FL" in prompt


# --------------------------------------------------------------------------- #
# 3. An unconfirmed hold is stated as unfinished work — the dropped-reschedule bug.
# --------------------------------------------------------------------------- #
def test_held_slot_is_flagged_as_unfinished():
    held = {"start_human": "Saturday, 1 August 2026 at 11 AM",
            "showroom": {"name": "Jetour Al Quoz"}}
    prompt = system_prompt(live_bookings=[_one("TD-2026-10E9")], held_slot=held)
    assert "not booked yet" in prompt
    assert "Jetour Al Quoz" in prompt

    # A hold with no booking behind it still renders, and without a dangling
    # "also" referring back to a list that was never spoken.
    solo = _render_bookings(None, held)
    assert solo.startswith("You are holding")
    assert "also" not in solo.split(".")[0]


# --------------------------------------------------------------------------- #
# 4. A doorstep booking reads as a doorstep booking, not as a showroom with a
#    missing branch name (FR-SL-07).
# --------------------------------------------------------------------------- #
def test_doorstep_booking_renders_its_address():
    text = _render_bookings(
        [_one("TD-2026-9ZZ1", location_type="doorstep", showroom_name=None,
              doorstep_address="Jumeirah 2")],
        None,
    )
    assert "doorstep drive at Jumeirah 2" in text


# --------------------------------------------------------------------------- #
# 5. A booking whose slot is missing is still NAMED rather than dropped. Silently
#    omitting it would recreate the exact denial this anchor exists to prevent.
# --------------------------------------------------------------------------- #
def test_booking_without_a_slot_is_still_named():
    text = _render_bookings([_one("TD-2026-0007", start_human=None, showroom_name=None)], None)
    assert "TD-2026-0007" in text


# --------------------------------------------------------------------------- #
# 6. The service half, end to end against the real DB: a created booking comes
#    back from for_customer, and it carries NO PII (no viewer is passed, so
#    _booking_dict's gate leaves name and mobile out of the prompt).
# --------------------------------------------------------------------------- #
def test_for_customer_returns_live_bookings_without_pii(slots):
    db = slots
    cust = customers.find_or_create(db, mobile=MOBILE, name="Anchor Tester")
    cid = cust["customer"]["customer_id"]

    slot = db.scalars(
        select(Slot).where(Slot.status == SlotStatus.open).order_by(Slot.start, Slot.id)
    ).first()
    session_id = "sess-anchor-1"
    assert slots_service.lock(db, session_id=session_id, slot_id=slot.id)["locked"] is True
    slots_service.acknowledge_licence_reminder(db, session_id=session_id)
    made = booking_service.create(
        db, session_id=session_id, customer_id=cid,
        vehicle_id=slot.vehicle_id, slot_id=slot.id,
    )
    assert made.get("booking"), made

    rows = booking_service.for_customer(db, customer_id=cid)
    assert len(rows) == 1
    assert rows[0]["booking_ref"] == made["booking"]["booking_ref"]
    # The anchor speaks the car, the place and the time — never the name or mobile.
    assert "customer_mobile" not in rows[0]
    assert "customer_name_en" not in rows[0]

    # An unusable id is an empty list, never an exception: a prompt anchor must not
    # be able to break a live turn.
    assert booking_service.for_customer(db, customer_id=None) == []
    assert booking_service.for_customer(db, customer_id="nonsense") == []
    assert booking_service.for_customer(db, customer_id=999999) == []


# --------------------------------------------------------------------------- #
# 7. held_by_session reports this session's hold, is scoped to the session, and
#    performs NO WRITE (the agent node owns no transaction — only the tool node
#    may commit).
# --------------------------------------------------------------------------- #
def test_held_by_session_is_scoped_and_read_only(slots):
    db = slots
    slot = db.scalars(
        select(Slot).where(Slot.status == SlotStatus.open).order_by(Slot.start, Slot.id)
    ).first()
    mine, theirs = "sess-held-mine", "sess-held-theirs"

    assert slots_service.held_by_session(db, session_id=mine) is None
    slots_service.lock(db, session_id=mine, slot_id=slot.id)

    held = slots_service.held_by_session(db, session_id=mine)
    assert held is not None and held["slot_id"] == slot.id
    # Another session's hold is invisible here, exactly as it is to availability.
    assert slots_service.held_by_session(db, session_id=theirs) is None

    # Read-only: an expired lock is FILTERED OUT by the query, never released by
    # it, so calling this leaves the row untouched for the tool node to tidy up.
    slot.locked_at = slot.locked_at - timedelta(days=1)
    db.flush()
    assert slots_service.held_by_session(db, session_id=mine) is None
    db.refresh(slot)
    assert slot.status == SlotStatus.locked, "held_by_session must not write"


# --------------------------------------------------------------------------- #
# 8. The anchor sits AFTER the identity anchor: who you are speaking with, then
#    what they have. Both present at once must not clobber each other.
# --------------------------------------------------------------------------- #
def test_both_anchors_coexist_in_order():
    prompt = system_prompt(
        session_facts={"name": "Ravi Sharma", "mobile": "+971 50 555 0103"},
        live_bookings=[_one("TD-2026-10E9")],
    )
    assert "Ravi Sharma" in prompt
    assert "TD-2026-10E9" in prompt
    assert prompt.index("WHO YOU ARE ALREADY SPEAKING WITH") < prompt.index(_ANCHOR_HEADER)


# --------------------------------------------------------------------------- #
# 9b. KNOWING IS NOT DESCRIBING. The anchor must keep deferring to get_booking,
#     and must not contradict MANAGING AN EXISTING BOOKING in the body ("only ever
#     describe a booking from what that tool returns").
#
#     Found live, not in review: the first version of this anchor told the model to
#     answer about a booking directly, so it stopped calling get_booking — and
#     because booking cards are drawn from that tool_event (and from the `cards`
#     anchor it writes), the customer got correct prose with NO CARD beside it.
# --------------------------------------------------------------------------- #
def test_anchor_still_defers_to_get_booking():
    prompt = system_prompt(live_bookings=[_one("TD-2026-10E9")])
    assert "get_booking" in prompt
    # The body's standing rule must survive alongside the anchor, not be displaced.
    assert "only ever describe a booking from what that tool returns" in prompt
    # And the anchor must not re-introduce the instruction that broke it.
    assert "answer about it directly instead of asking" not in prompt


# --------------------------------------------------------------------------- #
# 9. Rule 17: the anchor is prose the model echoes, so it carries no markdown,
#    no bullet characters and no list numerals.
# --------------------------------------------------------------------------- #
def test_anchor_is_plain_prose():
    text = _render_bookings(
        [_one("TD-2026-10E9"), _one("TD-2026-4A2C")],
        {"start_human": "Saturday at 11 AM", "showroom": {"name": "Jetour Al Quoz"}},
    )
    for banned in ("**", "##", "- ", "* ", "\n1.", "•"):
        assert banned not in text, f"anchor must not contain {banned!r}"


# --------------------------------------------------------------------------- #
# 10. THE REGRESSION THIS SECOND LAYER EXISTS FOR (2026-08-03).
#
#     This anchor is rebuilt on EVERY generation, so the moment create_booking
#     commits, the new booking appears here — and under the old heading ("WHAT THIS
#     CUSTOMER ALREADY HAS WITH US") it read as something the customer walked in
#     with. Live, the model believed the heading over its own tool result: it saw
#     two refs it had created minutes earlier, decided they were pre-existing, and
#     explained the coincidence as duplicate detection — a mechanism it invented to
#     account for a contradiction we had manufactured for it (rule 2). Walking that
#     back then forced it to narrate tool names and JSON keys at the customer,
#     breaking the "never describe your own machinery" rule as well.
#
#     Authorship is folded from create_booking's own result in tool_node
#     (_fold_session_facts) and spoken here.
# --------------------------------------------------------------------------- #
def test_a_booking_made_here_is_attributed_to_the_agent():
    text = _render_bookings([_one("TD-2026-HX4B")], None, ["TD-2026-HX4B"])
    assert "You booked one drive for them in this conversation just now" in text
    assert "TD-2026-HX4B" in text
    # The old framing must not survive for a booking the agent itself created.
    assert "They have one booking with us" not in text


def test_prior_and_fresh_bookings_are_told_apart():
    text = _render_bookings(
        [_one("TD-2026-4GRW"), _one("TD-2026-HX4B", vehicle_name="Jetour T2")],
        None,
        ["TD-2026-HX4B"],
    )
    assert "They had one booking with us before this conversation" in text
    assert "TD-2026-4GRW" in text
    assert "You have also booked one drive for them in this conversation just now" in text
    assert "TD-2026-HX4B" in text
    # Ordering: what they arrived with, then what you did.
    assert text.index("TD-2026-4GRW") < text.index("TD-2026-HX4B")


def test_two_bookings_made_here_are_both_attributed():
    text = _render_bookings(
        [_one("TD-2026-4GRW"), _one("TD-2026-HX4B")],
        None,
        ["TD-2026-4GRW", "TD-2026-HX4B"],
    )
    assert "You booked two drives for them in this conversation just now" in text
    assert "before this conversation" not in text


def test_the_anchor_forbids_calling_your_own_booking_pre_existing():
    prompt = system_prompt(
        session_facts={"name": "Aqeel Ahmed", "booked_refs": ["TD-2026-HX4B"]},
        live_bookings=[_one("TD-2026-HX4B")],
    )
    assert "never call it pre-existing" in prompt
    assert "never explain it as a duplicate we matched" in prompt


# --------------------------------------------------------------------------- #
# 10b. Absent or empty booked_refs leaves the rendering byte-identical, so every
#      conversation that has booked nothing is untouched by the change above.
# --------------------------------------------------------------------------- #
def test_no_booked_refs_is_byte_identical():
    rows = [_one("TD-2026-10E9"), _one("TD-2026-4A2C")]
    base = _render_bookings(rows, None)
    assert _render_bookings(rows, None, None) == base
    assert _render_bookings(rows, None, []) == base
    # A ref we did NOT create in this session must not be claimed as ours.
    assert _render_bookings(rows, None, ["TD-2026-ZZZZ"]) == base
    assert "They have two bookings with us" in base


# --------------------------------------------------------------------------- #
# 10c. The held-slot sentence still lands when the only booking is one we just
#      made — the two layers compose rather than replacing one another.
# --------------------------------------------------------------------------- #
def test_held_slot_survives_attribution():
    text = _render_bookings(
        [_one("TD-2026-HX4B")],
        {"start_human": "Saturday at 11 AM", "showroom": {"name": "Jetour Al Quoz"}},
        ["TD-2026-HX4B"],
    )
    assert "in this conversation just now" in text
    assert "Saturday at 11 AM" in text
