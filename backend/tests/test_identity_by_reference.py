"""STANDING GUARD: the manage-a-booking identity, by reference OR mobile.

THE CHANGE THIS PINS (product decision 2026-08-05). Viewing, rescheduling and
cancelling a booking used to require the customer's FULL NAME plus their mobile:
`find_or_create_customer(verify_name=True)` refused to bind a session without a
matching name, and every downstream guard refused an unbound session. A customer
who wanted to cancel a test drive therefore had to produce their name first. That
is now the TD REFERENCE **or** the MOBILE, either one alone, and no name at all
(`booking.identify` -> the identify_by_reference_or_mobile tool).

WHAT DID NOT CHANGE, and is what these tests actually protect: the session is
still bound from a tool RESULT server-side, and reschedule/cancel still refuse a
booking the bound customer does not own. Only the QUESTION the customer is asked
moved. So the ownership tests in test_booking_management.py stay valid verbatim,
and this file covers the new front door.

⚠️ THE COST, PINNED DELIBERATELY (test_a_reference_alone_is_a_full_credential).
A reference now identifies its owner on its own, so whoever holds one becomes
that customer for the session — name, consent flags, consented memory and their
bookings. That is the accepted trade, and it is a test rather than a comment
because a future "hardening" pass that quietly re-adds a second factor would
change customer-visible behaviour the developer chose. It also makes
`booking._next_ref`'s randomness load-bearing rather than defence in depth: see
the warning there.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import LocationType, Slot, SlotStatus
from app.services import booking, customers
from app.services import slots as slots_service
from app.tools import TOOL_FNS, TOOL_SCHEMAS

SESSION = "sess-identity"
MOBILE = "+971501112233"
OTHER_MOBILE = "+971502223344"


# --------------------------------------------------------------------------- #
# Helpers — the real write path, never an INSERT (CLAUDE.md working conventions)
# --------------------------------------------------------------------------- #
def _customer(db, mobile=MOBILE, **kw) -> int:
    return customers.find_or_create(db, mobile=mobile, **kw)["customer"]["customer_id"]


def _far_slot(db, skip: int = 0) -> Slot:
    cutoff = datetime.now() + timedelta(hours=25)
    rows = db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.start > cutoff,
        )
        .order_by(Slot.start, Slot.id)
    ).all()
    assert len(rows) > skip, "no far-future open showroom slot"
    return rows[skip]


def _book(db, slot: Slot, cid: int, *, session=SESSION) -> str:
    locked = slots_service.lock(db, session_id=session, slot_id=slot.id)
    assert locked["locked"] is True
    slots_service.acknowledge_licence_reminder(db, session_id=session)
    out = booking.create(
        db,
        session_id=session,
        customer_id=cid,
        vehicle_id=slot.vehicle_id,
        slot_id=slot.id,
    )
    assert out.get("created") is True, out
    return out["booking"]["booking_ref"]


# --------------------------------------------------------------------------- #
# Either identifier alone
# --------------------------------------------------------------------------- #
def test_a_reference_alone_identifies_its_owner(db, slots):
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)

    out = booking.identify(db, ref=ref)

    assert out["customer"]["customer_id"] == cid
    assert out["created"] is False


def test_a_mobile_alone_identifies_the_customer(db, slots):
    cid = _customer(db, name="Ravi Sharma")
    _book(db, _far_slot(db), cid)

    out = booking.identify(db, mobile=MOBILE)

    assert out["customer"]["customer_id"] == cid


def test_no_name_is_ever_required(db, slots):
    """The whole point of the change: a customer we hold a NAME for is identified
    without giving it. Under the old gate this same call returned name_required."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)

    by_ref = booking.identify(db, ref=ref)
    by_mobile = booking.identify(db, mobile=MOBILE)

    assert "error" not in by_ref
    assert "error" not in by_mobile
    # And the old gate is genuinely what we moved away from, not something that
    # merely stopped firing: the same number through the BOOKING door still asks.
    assert customers.find_or_create(db, mobile=MOBILE, verify_name=True)["error"] == (
        "name_required"
    )


def test_a_reference_alone_is_a_full_credential(db, slots):
    """The accepted cost, pinned so it cannot be re-tightened by accident.

    Whoever holds the reference becomes that customer for the session: the
    envelope carries their name and their recorded consent. Deliberate (product
    decision 2026-08-05) and on the D1 PDPL checklist — a reference is a
    possession factor, and `_next_ref`'s randomness is now the only thing
    guarding this journey.
    """
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)

    out = booking.identify(db, ref=ref)

    assert out["customer"]["name_en"] == "Ravi Sharma"
    assert out["customer"]["mobile"] == MOBILE
    assert "consent_messaging" in out["customer"]


def test_the_reference_wins_when_both_are_given(db, slots):
    """It names ONE booking, so it settles the customer without a second lookup —
    and a customer reading their own reference while giving somebody else's number
    is identified as the reference's owner, never as the number's."""
    mine = _customer(db, name="Ravi Sharma")
    theirs = _customer(db, mobile=OTHER_MOBILE, name="Sara Khan")
    ref = _book(db, _far_slot(db), mine)

    out = booking.identify(db, ref=ref, mobile=OTHER_MOBILE)

    assert out["customer"]["customer_id"] == mine
    assert out["customer"]["customer_id"] != theirs


# --------------------------------------------------------------------------- #
# Reading a reference the customer actually wrote or said
# --------------------------------------------------------------------------- #
def test_spacing_case_and_missing_dashes_are_the_same_reference(db, slots):
    """Nobody says the dashes. A transcriber writes "TD 2026 HTXE" and a phone
    keyboard produces "td2026htxe"; both are the code the customer gave us, and
    refusing them would be us failing to read, then blaming the customer."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)  # TD-YYYY-XXXX
    year, suffix = ref.split("-")[1], ref.split("-")[2]

    for spelling in (
        ref.lower(),
        f"TD {year} {suffix}",
        f"td{year}{suffix}",
        f"  TD-{year}-{suffix.lower()}  ",
        f"TD.{year}.{suffix}",
    ):
        out = booking.identify(db, ref=spelling)
        assert out.get("customer", {}).get("customer_id") == cid, spelling


def test_a_wrong_character_is_not_forgiven(db, slots):
    """Punctuation is read; the four characters are not guessed (developer
    decision 2026-08-05, exact match only). A normalised reference must never
    resolve to a DIFFERENT booking than the one the customer named."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)
    suffix = ref.split("-")[2]
    wrong = suffix[:-1] + ("Z" if suffix[-1] != "Z" else "Y")

    out = booking.identify(db, ref=f"TD-{ref.split('-')[1]}-{wrong}")

    assert out["error"] == "no_booking_found"


# --------------------------------------------------------------------------- #
# The honest dead ends
# --------------------------------------------------------------------------- #
def test_an_unknown_reference_and_an_unknown_number_are_the_same_answer(db, slots):
    """One uniform no_booking_found. They are the same fact to the customer, and
    keeping them indistinguishable means this cannot be used to ask whether a
    given mobile is on file."""
    by_ref = booking.identify(db, ref="TD-2026-ZZZZ")
    by_mobile = booking.identify(db, mobile="+971509998877")

    assert by_ref["error"] == "no_booking_found"
    assert by_mobile["error"] == by_ref["error"]


def test_neither_identifier_is_refused_as_data(db, slots):
    out = booking.identify(db)
    assert out["error"] == "identifier_required"


def test_an_unreadable_number_still_says_so(db, slots):
    """Different from not-found on purpose: "could you read that back?" is the
    useful, honest answer and reveals nothing about who exists."""
    out = booking.identify(db, mobile="12")
    assert out["error"] == "invalid_uae_mobile"
    assert out["reason"] in {"too_short", "not_a_number"}


def test_a_cancelled_reference_no_longer_identifies(db, slots):
    """Active bookings only. A cancelled reference is not a live booking, so it is
    not a way in — matching _lookup's active_only rule for every other path."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)
    cancelled = booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=ref)
    assert cancelled.get("cancelled") is True, cancelled

    out = booking.identify(db, ref=ref)

    assert out["error"] == "no_booking_found"


def test_a_known_number_with_no_booking_still_identifies(db, slots):
    """So "you have no booking with us" is sayable. They are who they say they
    are; get_booking then honestly returns an empty list."""
    cid = _customer(db, name="Ravi Sharma")

    out = booking.identify(db, mobile=MOBILE)

    assert out["customer"]["customer_id"] == cid
    assert booking.find(db, mobile=MOBILE, owner_customer_id=cid)["count"] == 0


def test_identify_never_creates_a_customer(db, slots):
    """What makes the relaxed identity safe to attach to this door: the worst case
    is failing to find someone, never minting a nameless customer the way
    find_or_create would."""
    before = len(db.scalars(select(customers.Customer)).all())

    booking.identify(db, mobile="+971509998877")

    assert len(db.scalars(select(customers.Customer)).all()) == before


# --------------------------------------------------------------------------- #
# The journey end to end, through the TOOL the model actually calls
# --------------------------------------------------------------------------- #
def test_the_tool_is_registered_and_declares_no_name(db, slots):
    schema = next(
        s for s in TOOL_SCHEMAS if s["name"] == "identify_by_reference_or_mobile"
    )
    props = schema["input_schema"]["properties"]

    assert set(props) == {"ref", "mobile"}
    assert schema["input_schema"]["required"] == []
    # The description is prompt engineering (CLAUDE.md): the model must be told
    # both that either alone is enough and that a name is not to be asked for.
    assert "either one on its own is enough" in schema["description"].lower()
    assert "do not ask for their name" in schema["description"].lower()


def test_identifying_by_reference_then_cancelling(db, slots):
    """The whole point, through the tools: reference in, booking cancelled, and
    at no stage was a name involved."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)

    identified = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION, ref=ref)
    bound = identified["customer"]["customer_id"]
    assert bound == cid

    seen = TOOL_FNS["get_booking"](db, SESSION, ref=ref, viewer_customer_id=bound)
    assert [b["booking_ref"] for b in seen["bookings"]] == [ref]

    out = TOOL_FNS["cancel_booking"](db, SESSION, customer_id=bound, ref=ref)
    assert out["cancelled"] is True


def test_identifying_by_mobile_then_rescheduling(db, slots):
    cid = _customer(db, name="Ravi Sharma")
    first, second = _far_slot(db), None
    ref = _book(db, first, cid)
    second = next(
        s
        for s in db.scalars(
            select(Slot)
            .where(
                Slot.status == SlotStatus.open,
                Slot.vehicle_id == first.vehicle_id,
                Slot.location_type == LocationType.showroom,
                Slot.start > datetime.now() + timedelta(hours=25),
            )
            .order_by(Slot.start, Slot.id)
        ).all()
        if s.id != first.id
    )

    identified = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION, mobile="050 111 2233")
    bound = identified["customer"]["customer_id"]
    assert bound == cid

    assert slots_service.lock(db, session_id=SESSION, slot_id=second.id)["locked"] is True
    out = TOOL_FNS["reschedule_booking"](
        db, SESSION, customer_id=bound, new_slot_id=second.id, ref=ref
    )
    assert out["rescheduled"] is True
    assert out["booking"]["booking_ref"] == ref


def test_a_stranger_still_cannot_act_on_a_booking_they_did_not_identify(db, slots):
    """The ownership guard is untouched by the change. Identifying as one customer
    does not reach another's booking, whatever reference is passed."""
    mine = _customer(db, name="Ravi Sharma")
    theirs = _customer(db, mobile=OTHER_MOBILE, name="Sara Khan")
    their_ref = _book(db, _far_slot(db), theirs)

    out = TOOL_FNS["cancel_booking"](db, SESSION, customer_id=mine, ref=their_ref)

    assert out["error"] == "booking_not_found"


# --------------------------------------------------------------------------- #
# She cannot answer the question herself
# --------------------------------------------------------------------------- #
def test_neither_identifier_ends_the_turn(db, slots):
    """BROWSER RUN, 2026-09-06. Asked in Arabic to cancel, Aisha called this with
    nothing, asked for the reference or the number — and, handed a generation
    with nothing left to fill it, filled it with TD-2026-HTXE, the example out of
    this tool's own schema: said it to the customer, called this again with it,
    and reported no booking found. The reference matched nothing, so it failed
    harmlessly; against one that exists the same reflex opens a stranger's
    booking.

    The marker is the one _AWAITING_KEY was added for (an invented landmark, an
    invented consent) — the tool node reads it and the graph ENDs the turn, so
    there is no generation in which to invent an answer."""
    out = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION)

    assert out["error"] == "identifier_required"
    assert out["awaiting_customer"] is True


def test_a_failed_lookup_still_gets_its_generation(db, slots):
    """The line location_tools draws between no_location and its other two
    branches. 'We have nothing under that' and 'could you read that back?' are
    facts the customer must HEAR, so those turns must not end."""
    unknown = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION, ref="TD-2026-ZZZZ")
    unreadable = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION, mobile="12")

    assert unknown["error"] == "no_booking_found"
    assert "awaiting_customer" not in unknown
    assert unreadable["error"] == "invalid_uae_mobile"
    assert "awaiting_customer" not in unreadable


def test_a_successful_identification_is_untouched(db, slots):
    """The envelope the tool node binds the session from is unchanged — the
    marker rides only on the branch that has nothing to report."""
    cid = _customer(db, name="Ravi Sharma")
    ref = _book(db, _far_slot(db), cid)

    out = TOOL_FNS["identify_by_reference_or_mobile"](db, SESSION, ref=ref)

    assert out["customer"]["customer_id"] == cid
    assert "awaiting_customer" not in out
