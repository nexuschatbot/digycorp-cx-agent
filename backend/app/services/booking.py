"""Booking service — the most guarded write in the system (FR-BK, FR-SL-06/07).

The LLM is a conversational engine, not a source of correctness. Every rule here
is enforced STRUCTURALLY: the prompt says "recap before confirming", this module
is what makes it true. Nothing below trusts a value because the model sent it.

THE THREE WRITE GUARDS
----------------------
1. INVARIANT — the slot must be locked by THIS session. `session_id` is injected
   by the executor and never appears in a tool schema, so this is a fact we can
   check rather than a claim the model makes about itself.
2. IDEMPOTENCY — LLMs retry. The same session booking the same slot twice gets
   the SAME booking back, never a duplicate.
3. AUDIT AT THE SERVICE LAYER — so the one-tap WhatsApp path (which skips the
   LLM entirely) is logged too. Auditing in the tool would lose all of it, and
   FR-AD-02 requires all of it.

CONSENT IS NOT REQUIRED TO BOOK. Nothing here gates on consent_messaging, and
that is deliberate: a customer who declines WhatsApp still gets their test drive
— we simply do not message them. The booking is the service; messaging is an
add-on. Gating the booking on it would quietly turn a privacy choice into a
denial of service.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Actor,
    AuditType,
    Booking,
    BookingAttendance,
    BookingStatus,
    Customer,
    LocationType,
    NotificationKind,
    Slot,
    SlotStatus,
    VehicleModel,
)
from app.services import audit, customers, memory, notifications
from app.services.slots import _human_start, _release_expired_locks

# Statuses that occupy a slot. MUST match the predicate of the partial unique
# index `uq_bookings_active_slot` in models.py — the index is the guarantee, this
# is the query that anticipates it. If they disagree, this module's checks pass
# and the DB rejects the write (safe but confusing), or vice versa.
ACTIVE_STATUSES = (BookingStatus.confirmed, BookingStatus.rescheduled)

# Bounded retry for the TD-ref race. Each attempt re-reads MAX, so N attempts
# survive N-1 concurrent winners. A demo has one or two.
REF_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------------- #
# TD-YYYY-NNNN (FR-BK-08)
# --------------------------------------------------------------------------- #
# Crockford base32: the 10 digits plus A-Z with the ambiguous glyphs I, L, O, U
# already dropped, so a ref read aloud or off a screen at a live demo has no 0/O
# or 1/I/L confusion. 32 symbols; a 4-char suffix is 32**4 ≈ 1.05M values, ample
# headroom for the collision retry loop below.
_REF_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_REF_SUFFIX_LEN = 4


def _next_ref(db: Session, year: int) -> str:
    """A booking reference for `year`: TD-YYYY-XXXX with a RANDOM base32 suffix.

    Changed from the old MAX+1 sequence (developer decision 2026-07-24). The
    suffix carries no ordering signal at all, which closes the enumeration hole
    the sequential scheme left: TD-2026-0003 used to tell an attacker that
    TD-2026-0002 and -0004 exist.

    ⚠️ AS OF 2026-08-05 THIS IS THE ONLY LINE, not defence in depth. It used to
    sit behind the name+number gate, which meant a guessed ref could not be
    viewed, moved or cancelled without also matching the owner. That gate is gone
    (product decision — see identify() below): a reference now identifies its
    owner on its own. So the unguessability of this suffix IS the security of the
    manage-a-booking journey, and shortening it, making it sequential, or deriving
    it from anything (a slot id, a customer id, a timestamp) would silently open
    every booking in the system. 32**4 ≈ 1.05M, drawn from `secrets`.

    Uniqueness is NOT this function's job and never was: it is guaranteed by the
    UNIQUE constraint on bookings.ref. A random draw can collide exactly as the
    old MAX+1 race could, and the loser retries the same way — create() catches
    the IntegrityError and calls this again for a fresh draw (REF_MAX_ATTEMPTS).
    So this only has to be a good guess, not a proof.

    `db` is unused now (no MAX query) but kept in the signature so the call site
    and the retry loop stay unchanged.

    The alphabet excludes I/L/O/U for read-aloud and on-screen legibility, and
    refs are compared case-insensitively at lookup, so the uppercase form here is
    only how it is displayed.
    """
    suffix = "".join(secrets.choice(_REF_ALPHABET) for _ in range(_REF_SUFFIX_LEN))
    return f"TD-{year}-{suffix}"


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _booking_dict(b: Booking, *, viewer_customer_id: int | None = None) -> dict:
    """One booking as data.

    PII GATE (added 20 Jul 2026 for the booking card). `customer_name` and
    `customer_mobile` are included ONLY when the viewing session has already
    identified itself as this booking's customer. Everyone else — including a
    session holding nothing but a guessed TD reference — gets the booking
    WITHOUT them, exactly as this payload has always looked.

    Why the gate exists at all: _lookup below deliberately accepts a TD-ref
    alone as sufficient identification (FR-RS-01), a demo-only relaxation
    already on the D1 PDPL checklist. That relaxation was tolerable while this
    payload carried no personal data — a guessed ref revealed a car and a time.
    The booking card puts the customer's NAME and MOBILE on screen, which would
    have turned a guessable reference into a way to read a stranger's phone
    number off a demo running in front of a live audience. So the ref-alone door
    stays open (the journey needs it) and the PII behind it does not.

    ⚠️ NARROWED IN PRACTICE ON 2026-08-05, and the gate itself is unchanged. A
    reference now IDENTIFIES its owner (identify(), below), so a session holding
    one becomes that customer and `identified` is then true — the name and mobile
    are returned. The gate still does exactly what it says (only the booking's own
    customer sees the PII); what changed is how cheaply a session can become that
    customer. Accepted with the decision, on the D1 checklist. Untouched callers
    that pass no viewer — the card rebuild, the WhatsApp reply — still get the
    payload without PII.

    Enforced HERE, in the service, not in the tool or the card: both callers and
    every future one inherit it, and the model is never asked to be discreet.
    """
    slot = b.slot
    identified = viewer_customer_id is not None and viewer_customer_id == b.customer_id
    out = {
        "booking_ref": b.ref,
        "status": b.status.value,
        # The second axis (2026-07-23) — did they turn up? An ENUM VALUE, never a
        # label: "No show" and "لم يحضر" are display text and are built per
        # language at render time (LOC-01), as everything else here is.
        "attendance": b.attendance.value if b.attendance else BookingAttendance.pending.value,
        "customer_id": b.customer_id,
        "vehicle_id": b.vehicle_id,
        "vehicle_name": b.vehicle.name if b.vehicle else None,
        # The trim, where one was recorded. `variant_name` is None for a model
        # with a single version — the seed's convention, not a missing value —
        # so a reader shows the model name alone rather than printing "None".
        "variant_id": b.variant_id,
        "variant_name": b.variant.name if b.variant else None,
        "slot_id": b.slot_id,
        "start_iso": slot.start.isoformat() if slot else None,
        # The card renders a RANGE ("Sunday, 7:00 pm - 8:00 pm"), so it needs the
        # end too. Both stay ISO: the day-name/12-hour form is display text and
        # is built per language at render time (LOC-01/LOC-09) — start_human
        # below is for the AGENT's prose, not for the card.
        "end_iso": slot.end.isoformat() if slot else None,
        # Human-readable slot for the reschedule/cancel recap (LOC-09): the agent
        # phrases this, per language, but grounding the day-name/12h form here
        # keeps a stale relative date from being invented (rule 2, FR-CX-07).
        "start_human": _human_start(slot.start) if slot else None,
        "location_type": b.location.value,
        "driver_name": b.driver_name,
        "driver_relationship": b.driver_relationship,
        "companions": b.companions,
        "child_seat": bool(b.child_seat),
        "consultant_pref": b.consultant_pref,
    }
    if identified and b.customer is not None:
        # name_en/name_ar are BOTH sent; the card picks by conversation language
        # rather than us freezing one script into the payload (LOC-01).
        out["customer_name_en"] = b.customer.name_en
        out["customer_name_ar"] = b.customer.name_ar
        out["customer_mobile"] = b.customer.mobile

    if b.location == LocationType.doorstep:
        out["doorstep_address"] = b.doorstep_address
        out["service_area"] = slot.service_area if slot else None
    else:
        out["showroom_id"] = slot.showroom_id if slot else None
        out["showroom_name"] = slot.showroom.name if slot and slot.showroom else None
        # Map pin for the card (LOC-09). Not gated: a showroom address is public.
        out["showroom_map_link"] = slot.showroom.map_link if slot and slot.showroom else None
    return out


def _active_booking_for_slot(db: Session, slot_id: int) -> Booking | None:
    return db.scalar(
        select(Booking).where(
            Booking.slot_id == slot_id, Booking.status.in_(ACTIVE_STATUSES)
        )
    )


def _existing_or_unavailable(existing: Booking, customer_id: int, slot_id: int) -> dict:
    """Resolve a slot that is already actively booked. TWO branches, never one.

    If the existing booking is THIS customer's, it is our own retry — return it
    (guard 2). If it belongs to someone ELSE, the honest answer is that the slot
    is gone. Returning the other booking would hand one customer another
    customer's name, driver, and appointment: that is a data leak wearing
    idempotency's clothes, and the difference between the two branches is only
    ever a customer_id comparison away.
    """
    if existing.customer_id == customer_id:
        return {"booking": _booking_dict(existing), "created": False, "idempotent": True}
    return {"error": "slot_unavailable", "slot_id": slot_id}


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    if isinstance(value, int):
        return bool(value)
    return False


def _clean_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


# --------------------------------------------------------------------------- #
# create — recap + licence + consent have happened; this is the write
# --------------------------------------------------------------------------- #
def create(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    vehicle_id: int,
    slot_id: int,
    variant_id: object = None,
    driver_name: object = None,
    driver_relationship: object = None,
    doorstep_address: object = None,
    companions: object = None,
    child_seat: object = False,
    consultant_pref: object = None,
    lang: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Create a booking. Never raises; every failure is returned as data.

    Flushes but does NOT commit — the CALLER owns the transaction (the CLI, and
    later the tool executor / REST route). The booking, its notification rows and
    its audit events are one business operation and must commit or roll back
    together: a confirmation row for a booking that rolled back would be a lie in
    the trail, and committing here would take that decision away from the caller
    that knows whether the rest of the turn succeeded.
    """
    now = now or datetime.now()

    cid = _coerce_int(customer_id)
    sid = _coerce_int(slot_id)
    vid = _coerce_int(vehicle_id)
    if cid is None or sid is None or vid is None:
        return {
            "error": "invalid_identifiers",
            "detail": "customer_id, vehicle_id and slot_id must be integers.",
        }

    customer = db.get(Customer, cid)
    if customer is None:
        return {"error": "customer_not_found", "customer_id": cid}
    slot = db.get(Slot, sid)
    if slot is None:
        return {"error": "slot_not_found", "slot_id": sid}
    if db.get(VehicleModel, vid) is None:
        return {"error": "vehicle_not_found", "vehicle_id": vid}

    # ---- Guard 2 BEFORE guard 1, and the order is not cosmetic --------------- #
    # A successful booking leaves the slot `booked` and holds NO lock. So on the
    # model's retry, the lock check below would find "not locked by this session"
    # and return an error for a booking that in fact succeeded — turning our own
    # idempotency into a failure the agent would apologise for. Idempotency must
    # therefore be answered before ownership is asked about.
    existing = _active_booking_for_slot(db, sid)
    if existing is not None:
        return _existing_or_unavailable(existing, cid, sid)

    # ---- Guard 1: this session must hold the lock ---------------------------- #
    _release_expired_locks(db, now)
    db.refresh(slot)
    if slot.status != SlotStatus.locked or slot.locked_by_session != session_id:
        # Covers: never locked, lock expired mid-recap, or another session's
        # lock. All the same answer — re-query availability and re-offer.
        return {
            "error": "slot_not_locked_by_session",
            "slot_id": sid,
            "detail": (
                "This slot is not held by this conversation. Check availability "
                "again and re-offer; do not assume it is still free."
            ),
        }

    # ---- Guard 1b: rule 9 precondition 3 — the licence reminder -------------- #
    # AFTER the lock guard, deliberately. A lapsed lock and a missing reminder
    # need different recoveries (re-offer the slot vs. give the reminder), and
    # reporting the reminder first would send the agent to re-remind on a slot it
    # no longer holds.
    #
    # This enforces ORDER, not delivery: it proves acknowledge_licence_reminder
    # ran against this lock, not that the customer read the words. Delivery is a
    # standing behaviour test (tests/test_licence_reminder.py) — the licence
    # reminder is a courtesy, and matching enforcement to stakes is why it is not
    # given the structural treatment that disclosure or double-booking get.
    if not slot.licence_ack:
        return {
            "error": "licence_reminder_not_acknowledged",
            "slot_id": sid,
            "detail": (
                "Remind the customer to bring their driving licence on the day, "
                "then call acknowledge_licence_reminder before booking."
            ),
        }

    if slot.start < now or slot.end <= now:
        # `end` as well as `start`: the buffer that governs what is OFFERED is
        # deliberately not applied here (see slots._lead_cutoff), so this is the
        # only bar a booking crosses, and a drive that is already over is not a
        # booking whichever of its two ends says so.
        return {"error": "slot_in_the_past", "slot_id": sid}

    # The slot IS the appointment: its vehicle is not negotiable. Without this,
    # a model that mixed up two ids mid-conversation could book the G700's slot
    # and tell the customer they are driving a T2 — discovered on arrival.
    if slot.vehicle_id != vid:
        return {
            "error": "vehicle_slot_mismatch",
            "slot_id": sid,
            "slot_vehicle_id": slot.vehicle_id,
            "requested_vehicle_id": vid,
            "detail": (
                "That slot belongs to a different model than the one being "
                "booked. Offer times for the requested car with "
                "get_available_slots and let the customer pick again."
            ),
        }

    # ---- The trim (2026-09-03) ---------------------------------------------- #
    # SAME RULE AS THE MODEL ABOVE, one level down. Inventory is grained per
    # variant, so the locked slot already knows which demo car this is; the
    # booking records it so the answer survives the fortnightly regeneration
    # that will delete the slot.
    #
    # READ OFF THE ROW, NEVER FROM THE ARGUMENT. A caller may pass `variant_id`
    # to say which trim it BELIEVES it is booking, and a disagreement is refused
    # rather than reconciled — booking the Comfort's slot while telling the
    # customer they are driving the Flagship is the same defect as
    # vehicle_slot_mismatch, and costs more, because the trims of one model look
    # alike in a way two models do not.
    requested_variant = _coerce_int(variant_id)
    if requested_variant is not None and slot.variant_id is not None:
        if requested_variant != slot.variant_id:
            return {
                "error": "variant_slot_mismatch",
                "slot_id": sid,
                "slot_variant_id": slot.variant_id,
                "requested_variant_id": requested_variant,
                "detail": (
                    "That slot is for a different trim of this model than the "
                    "one being booked. Offer times for the requested trim with "
                    "get_available_slots and let the customer pick again."
                ),
            }

    # ---- Doorstep (FR-SL-07) + data minimisation ---------------------------- #
    address = _clean_str(doorstep_address)
    is_doorstep = slot.location_type == LocationType.doorstep
    if is_doorstep and not address:
        return {
            "error": "doorstep_address_required",
            "slot_id": sid,
            "detail": "A doorstep test drive needs the address to bring the car to.",
        }
    if not is_doorstep:
        # PDPL data minimisation: never store an address for a showroom booking.
        # The customer is coming to us; we have no purpose for their address, so
        # holding it would be collection without a reason — even if the model
        # helpfully volunteered it.
        address = None

    booking: Booking | None = None
    for _attempt in range(REF_MAX_ATTEMPTS):
        candidate = Booking(
            ref=_next_ref(db, now.year),
            customer_id=cid,
            driver_name=_clean_str(driver_name),
            driver_relationship=_clean_str(driver_relationship),
            vehicle_id=vid,
            variant_id=slot.variant_id,
            slot_id=sid,
            location=slot.location_type,
            doorstep_address=address,
            companions=_clean_str(companions),
            child_seat=_coerce_bool(child_seat),
            consultant_pref=_clean_str(consultant_pref),
            status=BookingStatus.confirmed,
        )
        try:
            # SAVEPOINT: a constraint violation must undo only this INSERT, not
            # the caller's transaction.
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            booking = candidate
            break
        except IntegrityError:
            # TWO constraints can fire here and they mean opposite things.
            # uq_bookings_active_slot: someone booked this slot between our check
            # and our insert — that is a real answer, and retrying with a new ref
            # would just fail four more times and report the wrong reason.
            collided = _active_booking_for_slot(db, sid)
            if collided is not None:
                return _existing_or_unavailable(collided, cid, sid)
            # Otherwise it was bookings.ref: we lost the MAX+1 race. Re-read and
            # take the next number. This is the constraint doing its job.
            continue

    if booking is None:
        return {
            "error": "booking_ref_unavailable",
            "detail": f"Could not allocate a reference after {REF_MAX_ATTEMPTS} attempts.",
        }

    # The slot is now spent: booked, and holding no live lock. Clearing the lock
    # is what lets the same session lock a second slot for a second test drive.
    slot.status = SlotStatus.booked
    slot.locked_at = None
    slot.locked_by_session = None
    db.flush()

    # ---- Audit (guard 3) ----------------------------------------------------- #
    # AuditType.intent, not a bespoke "booking" type: the audit vocabulary is
    # fixed by the API contract in CLAUDE.md and shared with the frontend, so
    # adding a value would be a contract change in both repos. A created booking
    # is an intent carried out.
    audit.record(
        db,
        session=session_id,
        actor=Actor.agent,
        type=AuditType.intent,
        payload={
            "intent": "create_booking",
            "ref": booking.ref,
            "customer_id": cid,
            "vehicle_id": vid,
            "slot_id": sid,
            "location_type": slot.location_type.value,
            "start_iso": slot.start.isoformat(),
        },
        correlation_id=correlation_id,
    )

    if is_doorstep:
        # Address consent is audit-only, by design. It is per-BOOKING — this
        # address, this visit — so a customer-level consent flag would model it
        # wrong, and would also imply a standing permission to hold an address we
        # only ever needed once (PDPL data minimisation).
        audit.record(
            db,
            session=session_id,
            actor=Actor.customer,
            type=AuditType.consent,
            payload={
                "axis": "doorstep_address",
                "granted": True,
                "ref": booking.ref,
                "customer_id": cid,
                "service_area": slot.service_area,
                "at": now.isoformat(),
            },
            correlation_id=correlation_id,
        )

    # ---- Notifications: a side effect, never a tool -------------------------- #
    notes = notifications.schedule_for_booking(
        db, session_id=session_id, booking=booking, lang=lang, now=now,
        correlation_id=correlation_id,
    )

    # ---- Memory: the same side-effect argument (FR-MEM, rule 12) ------------- #
    # What they booked is observed behaviour, so it is learned here rather than
    # left to a tool the model might forget. No-ops without memory consent, and
    # the result is deliberately NOT returned to the caller: the agent has no
    # business knowing whether we just learned something about this customer.
    memory.derive_from_booking(
        db, session_id=session_id, customer_id=booking.customer_id, booking=booking,
        now=now, correlation_id=correlation_id,
    )

    return {"booking": _booking_dict(booking), "created": True, "notifications": notes}


# --------------------------------------------------------------------------- #
# Booking-management journey (FR-RS, FR-CN — behaviour rules 10/11, S-09..S-13)
# --------------------------------------------------------------------------- #
def _normalise_ref(value: object) -> str | None:
    """A TD reference the customer read out, tidied. Uppercased so 'td-2026-0141'
    finds TD-2026-0141 — the ref is a code, not case-sensitive prose.

    SEPARATORS ARE READ, NOT REQUIRED (2026-08-05, the reference-identity change).
    A reference is now something the customer SAYS or types to prove who they are,
    and nobody says the dashes: a transcriber writes "TD 2026 HTXE", a phone
    keyboard produces "td2026htxe", and both are the SAME code as TD-2026-HTXE.
    Refusing them would be us failing to read, then blaming the customer — exactly
    the reasoning customers.normalise_mobile already applies to "+971 50 123 4567".

    This is NOT fuzzy matching (developer decision 2026-08-05): the four
    characters themselves must be exactly right. Only the punctuation between
    them is forgiven, so a normalised ref can never resolve to a DIFFERENT
    booking than the one the customer named. Anything that does not fit the
    TD + 4-digit year + 4-character shape is returned tidied-but-unaltered, so an
    unrecognisable ref stays an honest not-found rather than a silent guess.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().upper()
    squashed = re.sub(r"[\s\-._]", "", raw)
    m = re.fullmatch(r"TD(\d{4})([0-9A-Z]{4})", squashed)
    if m is not None:
        return f"TD-{m.group(1)}-{m.group(2)}"
    return raw


def _lookup(
    db: Session,
    *,
    ref: object = None,
    mobile: object = None,
    active_only: bool = True,
    owner_customer_id: int | None = None,
) -> tuple[list[Booking], dict | None]:
    """Resolve bookings by TD-ref OR mobile. Returns (bookings, error_or_None).

    IDENTITY POLICY — a booking is FOUND by mobile OR TD reference (FR-RS-01 /
    FR-CN-01), but `owner_customer_id` narrows the result to that customer's own
    bookings when it is supplied.

    THE OWNER SCOPE (developer decision 2026-07-23, reversing the ref-alone
    relaxation for the agent journey). Left None, this is the raw lookup the
    trusted server-side callers still need — the reload card rebuild
    (api/bookings.py, ref-anchored) and the WhatsApp inbound reply
    (whatsapp_inbound.py, identity already proven by the sending number). Set to
    the session's VERIFIED customer, it restricts every result to bookings that
    customer actually owns, so:
      - a guessed TD-ref for someone ELSE resolves to nothing (honest not-found,
        no leak);
      - a mobile can only ever return the caller's own bookings.
    The write journeys (reschedule/cancel) always pass it; get_booking passes the
    viewer. This is what makes a sequential, guessable ref harmless (see
    _next_ref's production note).

    When both ref and mobile are given, ref wins (it is the precise identifier).
    An unparseable mobile comes back as the customers-service error dict so the
    agent can ask for it again; an unknown-but-valid mobile is an honest empty
    result, not an error.
    """

    def _scoped(rows: list[Booking]) -> list[Booking]:
        # The owner scope, applied AFTER the DB read so both branches share it and
        # neither can forget it. None means "no scope" — the trusted-caller case.
        if owner_customer_id is None:
            return rows
        return [b for b in rows if b.customer_id == owner_customer_id]

    r = _normalise_ref(ref)
    if r is not None:
        stmt = select(Booking).where(Booking.ref == r)
        if active_only:
            stmt = stmt.where(Booking.status.in_(ACTIVE_STATUSES))
        return _scoped(list(db.scalars(stmt.order_by(Booking.created_at)).all())), None

    if isinstance(mobile, str) and mobile.strip():
        norm = customers.normalise_mobile(mobile)
        if "error" in norm:
            return [], norm
        customer = db.scalar(select(Customer).where(Customer.mobile == norm["mobile"]))
        if customer is None:
            return [], None  # valid number, no customer — honest empty, not an error
        stmt = select(Booking).where(Booking.customer_id == customer.id)
        if active_only:
            stmt = stmt.where(Booking.status.in_(ACTIVE_STATUSES))
        return _scoped(list(db.scalars(stmt.order_by(Booking.created_at)).all())), None

    return [], {
        "error": "identifier_required",
        "detail": "Provide a TD reference (TD-YYYY-NNNN) or a mobile number to look up a booking.",
    }


def find(
    db: Session,
    *,
    ref: object = None,
    mobile: object = None,
    active_only: bool = True,
    viewer_customer_id: int | None = None,
    owner_customer_id: int | None = None,
) -> dict:
    """Read-only lookup for the "when's my booking?" front door (FR-RS-01).

    `viewer_customer_id` is WHOSE EYES this lookup is through — it only decides
    whether each booking carries the customer's name and mobile (see
    _booking_dict's PII gate). `owner_customer_id` is a stronger, separate control
    (developer decision 2026-07-23): it RESTRICTS which bookings come back to that
    customer's own, so the agent's get_booking can no longer surface a booking the
    caller does not own even by ref. The two are usually the same id (the verified
    session), but they answer different questions — "may they see the PII?" vs
    "may they see the booking at all?" — and the trusted card-reload path passes
    only the viewer, leaving the owner scope off.

    ACTIVE means status ∈ (confirmed, rescheduled) — NOT "in the future". A
    confirmed booking whose slot has already passed but was never cancelled is
    still active by this definition and will appear here; there is deliberately
    no time filtering (a past-but-uncancelled booking surfacing is correct, not a
    bug). Returns ALL matches so a customer with two live bookings sees both.
    Never raises; no lock, no side effects.
    """
    rows, err = _lookup(
        db, ref=ref, mobile=mobile, active_only=active_only, owner_customer_id=owner_customer_id
    )
    if err is not None:
        return err
    return {
        "bookings": [
            _booking_dict(b, viewer_customer_id=viewer_customer_id) for b in rows
        ],
        "count": len(rows),
    }


def identify(db: Session, *, ref: object = None, mobile: object = None) -> dict:
    """Identify the customer behind an EXISTING booking, by TD reference OR mobile.

    THE ENTRY POINT TO THE MANAGE-A-BOOKING JOURNEY (product decision 2026-08-05),
    replacing the name+number second factor that guarded it since 2026-07-23. A
    customer who wants to see, move or cancel a booking is asked for their
    reference or their mobile — whichever they have, EITHER ONE ALONE IS ENOUGH —
    and is never asked their name. Making a NEW booking is untouched and still
    goes through customers.find_or_create with verify_name=True: a first-time
    customer has no reference to give, and their name has to be recorded anyway.

    ⚠️ WHAT THIS DELIBERATELY GIVES UP, so nobody reads it as an oversight. Under
    the name gate, a wrong or guessed mobile could not bind a session; now it can,
    and so can a reference alone. A reference is a possession factor (4 random
    base32 characters, ~1M values, delivered by WhatsApp to that number) and is
    therefore the stronger half of the OR — the weaker half is the mobile, which
    anyone who knows the customer knows. What a bound session then exposes is the
    customer's name, consent flags and consented memory profile, plus their
    bookings. Accepted with the decision; on the D1 PDPL checklist (docs/pdpl.md).

    ONE UNIFORM `no_booking_found` for an unknown reference AND an unknown number.
    They are the same fact to the customer ("we have nothing under that"), and
    keeping them indistinguishable means this cannot be used to ask whether a
    given mobile is on file. An UNPARSEABLE mobile is different and does come back
    as the customers-service error, because "could you read that number back?" is
    the useful, honest answer and says nothing about who exists.

    A CUSTOMER WITH NO ACTIVE BOOKING still binds on their own mobile: they are
    who they say they are, and get_booking then honestly returns an empty list.
    Refusing to identify them would make "you have no booking with us" — a true
    and useful answer — impossible to say.

    Read-only: never creates a customer, never writes, never raises.
    """
    r = _normalise_ref(ref)
    has_mobile = isinstance(mobile, str) and mobile.strip()
    if r is None and not has_mobile:
        return {
            "error": "identifier_required",
            "detail": (
                "Ask the customer for their booking reference (TD-YYYY-XXXX) or "
                "the mobile number on the booking. Either one on its own is "
                "enough; never ask for both, and never ask for their name."
            ),
        }

    customer: Customer | None = None

    # The reference wins when both are given: it is the precise identifier, and
    # it names ONE booking, so it settles which customer without a second lookup.
    if r is not None:
        booking = db.scalar(
            select(Booking)
            .where(Booking.ref == r)
            .where(Booking.status.in_(ACTIVE_STATUSES))
        )
        if booking is not None:
            customer = db.get(Customer, booking.customer_id)

    if customer is None and has_mobile:
        normalised = customers.normalise_mobile(mobile)
        if "error" in normalised:
            return normalised
        customer = db.scalar(select(Customer).where(Customer.mobile == normalised["mobile"]))

    if customer is None:
        return {
            "error": "no_booking_found",
            "detail": (
                "Nothing matches that reference or number. Say so honestly, do "
                "not invent a booking, and offer to check the other identifier "
                "or arrange a human follow-up."
            ),
        }

    return customers.envelope_for(db, customer)


def for_customer(db: Session, *, customer_id: object, active_only: bool = True) -> list[dict]:
    """Every booking this customer holds right now, oldest first.

    THE PROMPT'S BOOKINGS ANCHOR READS THIS EVERY TURN (2026-07-27), which is why
    it exists as its own function rather than as another `find()` call. `find` is
    the CUSTOMER-FACING lookup and takes a ref or a mobile, because that is what a
    customer can say out loud; here we already hold the verified customer_id from
    the session binding, and re-deriving a mobile from it only to look the mobile
    back up would be a round trip through a weaker identifier.

    WHY THE ANCHOR NEEDS IT. The model sees only a trailing window of the
    conversation (graph.TRIM_KEEP), so a booking confirmed six exchanges ago is
    simply not in front of it. Asked about "my other booking" it then answered from
    what it could still see, and told a customer it had "only one booking in
    progress" minutes after confirming a second one. A booking is a FACT we hold in
    a table, so the fix is to read the table every turn rather than hope the
    sentence describing it survives the window.

    NO VIEWER IS PASSED, deliberately: the anchor speaks the car, the place and the
    time, never the name and mobile, so _booking_dict's PII gate leaves those out
    and nothing personal is written into the prompt that is not already in
    session_facts. Read-only, no lock, never raises. An unusable customer_id is an
    empty list, not an error: a prompt anchor must never be able to break a turn.
    """
    cid = _coerce_int(customer_id)
    if cid is None:
        return []
    stmt = select(Booking).where(Booking.customer_id == cid)
    if active_only:
        stmt = stmt.where(Booking.status.in_(ACTIVE_STATUSES))
    rows = db.scalars(stmt.order_by(Booking.created_at)).all()
    return [_booking_dict(b) for b in rows]


def _resolve_target(
    db: Session,
    *,
    ref: object = None,
    mobile: object = None,
    owner_customer_id: int | None = None,
) -> Booking | dict:
    """Resolve EXACTLY ONE active booking to act on, or an error dict.

    Zero matches -> booking_not_found. More than one (a mobile with two live
    bookings and no ref given) -> multiple_bookings WITH the list, so the agent
    asks which TD reference rather than guessing — same disambiguation the
    catalogue uses when a name matches two variants. `owner_customer_id` scopes
    the lookup so a write can only ever resolve the caller's OWN booking; a ref
    for someone else's booking is booking_not_found here, never a cross-customer
    action or a leak of their refs through multiple_bookings.
    """
    rows, err = _lookup(db, ref=ref, mobile=mobile, active_only=True, owner_customer_id=owner_customer_id)
    if err is not None:
        return err
    if not rows:
        return {
            "error": "booking_not_found",
            "detail": (
                "No active booking under that reference or number. Do not invent "
                "one — check the other identifier, or offer a human follow-up."
            ),
        }
    if len(rows) > 1:
        return {
            "error": "multiple_bookings",
            "bookings": [_booking_dict(b) for b in rows],
            "detail": "More than one active booking. Ask which TD reference they mean.",
        }
    return rows[0]


def reschedule(
    db: Session,
    *,
    session_id: str,
    new_slot_id: object,
    session_customer_id: object = None,
    ref: object = None,
    mobile: object = None,
    doorstep_address: object = None,
    lang: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Move an existing booking to a new slot (FR-RS, behaviour rule 10).

    IDENTITY GUARD (developer decision 2026-07-23, D1 PDPL). `session_customer_id`
    is the VERIFIED customer bound to this session server-side (injected by the
    tool node from find_or_create_customer, never modelled). A reschedule is
    refused unless it is present AND the booking belongs to it — so even a
    confused model cannot move a booking off a bare TD reference. The lookup is
    also scoped to this customer, so an unowned ref is simply not found. This is
    the server-side enforcement CLAUDE.md's write-guard section always specified
    ("reschedule/cancel verify the booking belongs to the identified customer");
    the ref-OR-mobile relaxation applied to the READ front door, never to a write.

    MUTATES IN PLACE, keeping the TD reference: status flips confirmed ->
    rescheduled, slot_id repoints to the new slot, the old slot is released to
    inventory, the new slot becomes booked. The reference is stable across any
    number of reschedules (S-11), which is exactly why the partial unique index
    counts `rescheduled` as an active status occupying its slot.

    The flow that reaches here is get_available_slots -> slot chip -> lock_slot
    (new slot) -> reschedule_booking, so the caller holds the new slot's lock;
    the ownership guard below is create_booking's guard 1, reused verbatim.

    Never raises; every failure is data. Flushes, never commits (the tool node /
    REST route owns the transaction — the move, its released slot, its new
    reminders and its audit rows land or roll back together).
    """
    now = now or datetime.now()

    scid = _coerce_int(session_customer_id)
    if scid is None:
        return {
            "error": "identity_not_verified",
            "detail": (
                "This booking can only be changed by the identified customer. "
                "Identify them with identify_by_reference_or_mobile first — "
                "their booking reference OR their mobile, either alone, no name "
                "needed — then reschedule."
            ),
        }

    target = _resolve_target(db, ref=ref, mobile=mobile, owner_customer_id=scid)
    if isinstance(target, dict):
        return target
    booking = target

    nsid = _coerce_int(new_slot_id)
    if nsid is None:
        return {"error": "invalid_slot_id", "slot_id": new_slot_id}

    # IDEMPOTENCY BEFORE THE LOCK CHECK — the same guard-order lesson as create().
    # A completed reschedule leaves the new slot `booked` and holding no lock, so
    # a retry would otherwise trip "not locked by this session" for a move that
    # already happened. If the booking already sits on the requested slot, this
    # is our own retry: return it, do not move again.
    if booking.slot_id == nsid:
        return {"booking": _booking_dict(booking), "rescheduled": False, "idempotent": True}

    new_slot = db.get(Slot, nsid)
    if new_slot is None:
        return {"error": "slot_not_found", "slot_id": nsid}

    # ---- Guard 1 (reused): the NEW slot must be locked by THIS session -------- #
    _release_expired_locks(db, now)
    db.refresh(new_slot)
    if new_slot.status != SlotStatus.locked or new_slot.locked_by_session != session_id:
        return {
            "error": "slot_not_locked_by_session",
            "slot_id": nsid,
            "detail": (
                "The new slot is not held by this conversation. Check availability "
                "again and re-offer; do not assume it is still free."
            ),
        }

    if new_slot.start < now or new_slot.end <= now:
        return {"error": "slot_in_the_past", "slot_id": nsid}

    # SAME VEHICLE ONLY (decision 4): a reschedule moves the time, it never
    # silently swaps the car. Driving a different model is a new booking.
    if new_slot.vehicle_id != booking.vehicle_id:
        return {
            "error": "vehicle_slot_mismatch",
            "slot_id": nsid,
            "slot_vehicle_id": new_slot.vehicle_id,
            "booking_vehicle_id": booking.vehicle_id,
            "detail": (
                "A reschedule keeps the same car, and that slot is for a "
                "different model. Offer times for the booking's own vehicle — "
                "or, if the customer wants the other car, that is a NEW "
                "booking, not a move."
            ),
        }

    # ---- Doorstep on the NEW slot (FR-SL-07) + data minimisation ------------- #
    # The location MAY change on a reschedule; if the new slot is doorstep, an
    # address is required — carried over from the booking if it already had one,
    # else supplied on this call. rule 9's recap (prompt) must state the new
    # LOCATION explicitly when it changes, not just the new time.
    address = _clean_str(doorstep_address) or booking.doorstep_address
    is_doorstep = new_slot.location_type == LocationType.doorstep
    if is_doorstep and not address:
        return {
            "error": "doorstep_address_required",
            "slot_id": nsid,
            "detail": "The new slot is a doorstep visit; ask for the address to bring the car to.",
        }
    if not is_doorstep:
        address = None  # PDPL: never hold an address for a showroom visit

    old_slot_id = booking.slot_id
    old_slot = db.get(Slot, old_slot_id)

    # ---- Mutate in place. No savepoint retry is needed here (unlike create) --- #
    # create() races the active-slot unique index because a slot goes
    # open -> locked -> booked with a window in between. Here the new slot is
    # `locked` BY THIS SESSION for the whole (uncommitted) transaction, and a slot
    # cannot be both locked and carry an active booking (booking sets it
    # `booked`), so no other active booking can exist on it — the index cannot
    # fire. Setting the relationship (not just the FK) keeps _booking_dict reading
    # the new slot after the flush.
    booking.slot = new_slot
    booking.location = new_slot.location_type
    # THE TRIM MOVES WITH THE SLOT. It is a property of the demo car being
    # driven, not of the booking's history, so a booking left pointing at the
    # old slot's trim would name a car the customer is no longer coming to see.
    # Only inventory that carries one overwrites: a move onto a pre-migration
    # slot must not erase a trim that was correctly recorded at create.
    if new_slot.variant_id is not None:
        booking.variant_id = new_slot.variant_id
    booking.doorstep_address = address
    booking.status = BookingStatus.rescheduled

    new_slot.status = SlotStatus.booked
    new_slot.locked_at = None
    new_slot.locked_by_session = None

    # The old slot returns to inventory instantly (it was `booked`, not `locked`).
    if old_slot is not None:
        old_slot.status = SlotStatus.open
        old_slot.locked_at = None
        old_slot.locked_by_session = None

    db.flush()

    # ---- Audit (guard 3): intent, sharing the choke-point correlation_id ------ #
    audit.record(
        db,
        session=session_id,
        actor=Actor.agent,
        type=AuditType.intent,
        payload={
            "intent": "reschedule_booking",
            "ref": booking.ref,
            "customer_id": booking.customer_id,
            "vehicle_id": booking.vehicle_id,
            "from_slot_id": old_slot_id,
            "to_slot_id": nsid,
            "location_type": new_slot.location_type.value,
            "start_iso": new_slot.start.isoformat(),
        },
        correlation_id=correlation_id,
    )

    if is_doorstep:
        # Per-booking, per-visit address consent — same shape create() records.
        audit.record(
            db,
            session=session_id,
            actor=Actor.customer,
            type=AuditType.consent,
            payload={
                "axis": "doorstep_address",
                "granted": True,
                "ref": booking.ref,
                "customer_id": booking.customer_id,
                "service_area": new_slot.service_area,
                "at": now.isoformat(),
            },
            correlation_id=correlation_id,
        )

    # ---- Notifications: retire the old reminders, issue new ones (FR-RS-04) ---- #
    # After this, the ONLY `scheduled` rows for the ref are the new slot's — the
    # old confirmation/T-24h/T-2h are `cancelled` (S-09). The new receipt is a
    # `reschedule` notice, not a `confirmation`.
    superseded = notifications.supersede_for_booking(
        db, session_id=session_id, ref=booking.ref, now=now, correlation_id=correlation_id,
    )
    fresh = notifications.schedule_for_booking(
        db, session_id=session_id, booking=booking, lang=lang, now=now,
        correlation_id=correlation_id, primary_kind=NotificationKind.reschedule,
    )

    # ---- Memory: a reschedule is a CORRECTED preference ---------------------- #
    # "Actually, make it evenings" is the strongest daypart signal we get, so the
    # new slot overwrites what create() learned from the old one. Cancel
    # deliberately does NOT do this — see services/memory.py.
    memory.derive_from_booking(
        db, session_id=session_id, customer_id=booking.customer_id, booking=booking,
        now=now, correlation_id=correlation_id,
    )

    return {
        "booking": _booking_dict(booking),
        "rescheduled": True,
        "notifications": {"superseded": superseded, "scheduled": fresh},
    }


def cancel(
    db: Session,
    *,
    session_id: str,
    session_customer_id: object = None,
    ref: object = None,
    mobile: object = None,
    reason_code: object = None,
    lang: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Cancel an existing booking (FR-CN, behaviour rule 11).

    IDENTITY GUARD (developer decision 2026-07-23, D1 PDPL) — identical to
    reschedule's: `session_customer_id` is the verified session customer, injected
    server-side, and a cancel is refused unless it owns the booking. A guessed TD
    reference cannot cancel a stranger's test drive. See reschedule for the full
    rationale.

    On success: status -> cancelled, the slot is released to inventory instantly
    (FR-CN-04), the customer is flagged for win-back (FR-CN-05), pending reminders
    are retired and a written cancellation notice is sent (FR-CN-03). reason_code
    is optional and NEVER demanded — the single soft retention offer and the
    no-loop discipline are the prompt's job (rule 11); the service just executes.

    IDEMPOTENT: cancelling an already-cancelled booking returns its current state,
    not an error. Never raises; flushes, never commits.
    """
    now = now or datetime.now()

    scid = _coerce_int(session_customer_id)
    if scid is None:
        return {
            "error": "identity_not_verified",
            "detail": (
                "This booking can only be cancelled by the identified customer. "
                "Identify them with identify_by_reference_or_mobile first — "
                "their booking reference OR their mobile, either alone, no name "
                "needed — then cancel."
            ),
        }

    target = _resolve_target(db, ref=ref, mobile=mobile, owner_customer_id=scid)
    if isinstance(target, dict):
        # Already cancelled is not "not found" — it is idempotent success. Only
        # when a SINGLE cancelled booking matches the identifiers (an unambiguous
        # replay); anything ambiguous falls through to the original error. The
        # lookup stays owner-scoped, so a stranger's cancelled ref is not found
        # here either — never confirmed back as "already cancelled".
        if target.get("error") == "booking_not_found":
            rows, _ = _lookup(db, ref=ref, mobile=mobile, active_only=False, owner_customer_id=scid)
            cancelled = [b for b in rows if b.status == BookingStatus.cancelled]
            if len(cancelled) == 1:
                return {
                    "booking": _booking_dict(cancelled[0]),
                    "cancelled": False,
                    "idempotent": True,
                    "winback_flag": bool(cancelled[0].customer and cancelled[0].customer.winback_flag),
                }
        return target
    booking = target

    slot = db.get(Slot, booking.slot_id)

    booking.status = BookingStatus.cancelled
    booking.reason_code = _clean_str(reason_code)  # optional; may stay None

    # Slot back to inventory instantly (FR-CN-04). It was `booked`; the partial
    # unique index excludes `cancelled`, so the freed slot is immediately bookable
    # by the next customer.
    if slot is not None:
        slot.status = SlotStatus.open
        slot.locked_at = None
        slot.locked_by_session = None

    # Win-back flag (FR-CN-05) — a cancelled customer is one to re-approach later.
    customer = booking.customer
    if customer is not None:
        customer.winback_flag = True

    db.flush()

    # ---- Audit (guard 3): intent, sharing the choke-point correlation_id ------ #
    audit.record(
        db,
        session=session_id,
        actor=Actor.agent,
        type=AuditType.intent,
        payload={
            "intent": "cancel_booking",
            "ref": booking.ref,
            "customer_id": booking.customer_id,
            "slot_id": booking.slot_id,
            "reason_code": booking.reason_code,
            "winback_flag": True,
        },
        correlation_id=correlation_id,
    )

    # Retire the pending reminders FIRST, then write the cancellation notice —
    # supersede only touches `scheduled` rows, so the notice it adds afterwards
    # survives (FR-CN-03: confirm the cancellation in writing).
    superseded = notifications.supersede_for_booking(
        db, session_id=session_id, ref=booking.ref, now=now, correlation_id=correlation_id,
    )
    notice = notifications.notify_cancellation(
        db, session_id=session_id, booking=booking, lang=lang, now=now,
        correlation_id=correlation_id,
    )

    return {
        "booking": _booking_dict(booking),
        "cancelled": True,
        "winback_flag": True,
        "notifications": {"superseded": superseded, "cancellation": notice},
    }


# --------------------------------------------------------------------------- #
# Attendance — did they turn up? (2026-07-23, admin console)
# --------------------------------------------------------------------------- #
def set_attendance(
    db: Session,
    *,
    session_id: str,
    ref: object,
    attendance: object,
    actor: Actor = Actor.admin,
    lang: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Record whether a customer attended their test drive. Console-only.

    THE AGENT CANNOT REACH THIS, and there is deliberately no tool for it.
    Attendance is a human's observation about the physical world — the model has
    no way to know it and no business asserting it (rule 2). The only caller is
    api/console.py, acting as `actor=admin`.

    FOUR RULES, all enforced here rather than in the route or the UI, so no
    future screen can forget one (AD-I):

    1. NOT ON A CANCELLED BOOKING -> {"error": "booking_cancelled"}. Nobody
       failed to attend a drive that was called off, and without this a single
       mis-click would send "we missed you today" to a customer who politely
       cancelled a week ago. It would also make the no-show count meaningless.
       This is the ONE restriction: marking a no-show BEFORE the slot time has
       passed is allowed on purpose (product decision 2026-07-23) — an operator
       who knows the customer is not coming should not have to wait for the
       clock to agree with them.
    2. IDEMPOTENT — setting the value it already has is success with
       `changed: false`, and sends nothing.
    3. EXACTLY ONE NO-SHOW MESSAGE per booking, guarded by
       `noshow_notified_at`. An operator correcting themselves (noshow ->
       pending -> noshow) must not text the customer twice, and a message
       already sent is not un-sent by moving off `noshow`: retracting it is not
       possible, and pretending otherwise would be worse than leaving it.
    4. AUDITED on every change, actor=admin (rule 15) — a person did this, and
       the trail must not read as though the agent or the scheduler decided it.

    The slot is NOT touched and reminders are NOT superseded: by the time this
    is called the drive's time has passed, so there is no inventory to release
    and nothing left to remind anyone about.

    Never raises; flushes, never commits.
    """
    now = now or datetime.now()

    target = _normalise_attendance(attendance)
    if target is None:
        return {
            "error": "invalid_attendance",
            "detail": (
                "attendance must be one of: "
                + ", ".join(a.value for a in BookingAttendance)
            ),
        }

    r = _normalise_ref(ref)
    if r is None:
        return {
            "error": "identifier_required",
            "detail": "Provide a TD reference (TD-YYYY-NNNN).",
        }

    # Looked up by ref DIRECTLY rather than through _resolve_target, which
    # resolves only ACTIVE bookings: a cancelled booking has to be findable in
    # order to be refused with the honest reason, instead of reported missing.
    booking = db.scalar(select(Booking).where(Booking.ref == r))
    if booking is None:
        return {"error": "booking_not_found", "detail": f"No booking under {r}."}

    if booking.status == BookingStatus.cancelled:
        return {
            "error": "booking_cancelled",
            "detail": "This booking was cancelled, so attendance does not apply.",
            "booking": _booking_dict(booking),
        }

    previous = booking.attendance or BookingAttendance.pending
    if previous == target:
        return {"booking": _booking_dict(booking), "changed": False, "notified": False}

    booking.attendance = target

    notified = False
    notice: dict | None = None
    if target == BookingAttendance.noshow and booking.noshow_notified_at is None:
        booking.noshow_notified_at = now
        notice = notifications.notify_noshow(
            db, session_id=session_id, booking=booking, lang=lang, now=now,
            correlation_id=correlation_id,
        )
        notified = True

    # A completed drive opens the feedback survey (2026-07-26). Guarded by
    # feedback_requested_at for exactly the reason noshow_notified_at guards the
    # notice above: an operator flipping completed -> pending -> completed is
    # correcting a mis-click, not asking us to survey the customer twice. Moving
    # OFF `completed` retracts nothing, because a WhatsApp message already
    # delivered cannot be recalled and pretending otherwise is worse.
    #
    # Imported HERE rather than at module scope: services/feedback imports
    # services/notifications, which this module also imports, and a top-level
    # import would close that cycle at startup.
    survey: dict | None = None
    if target == BookingAttendance.completed and booking.feedback_requested_at is None:
        from app.services import feedback as feedback_service

        booking.feedback_requested_at = now
        survey = feedback_service.open_for_booking(
            db, booking=booking, session_id=session_id, now=now,
            correlation_id=correlation_id,
        )

    db.flush()

    audit.record(
        db,
        session=session_id,
        actor=actor,
        type=AuditType.intent,
        payload={
            "intent": "set_attendance",
            "ref": booking.ref,
            "customer_id": booking.customer_id,
            "from": previous.value,
            "to": target.value,
            "notified": notified,
        },
        correlation_id=correlation_id,
    )

    out = {
        "booking": _booking_dict(booking),
        "changed": True,
        "notified": notified,
    }
    if notice is not None:
        out["notifications"] = {"noshow": notice}
    if survey is not None:
        out["feedback"] = survey
    return out


def _normalise_attendance(value: object) -> BookingAttendance | None:
    """The console's string to the enum, or None. Tolerant of case and spacing
    ("No Show" -> noshow) because this arrives from a UI, but never guessing:
    an unrecognised value is refused as data, not coerced to `pending`."""
    if isinstance(value, BookingAttendance):
        return value
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    for member in BookingAttendance:
        if cleaned == member.value:
            return member
    return None
