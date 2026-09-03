"""Slots service: inventory generation and availability queries.

Slots are INVENTORY, not hand-created records — in production they would come
from the dealership calendar. generate() is exposed only via the CLI, never as
a tool: the agent must never create inventory, only read it.

Calendar law lives in app.calendar_rules and is imported, never reimplemented:
it is the single source of truth for FR-SL-02/S-22 (no slot may exist inside the
Friday prayer window) and for per-weekday opening hours. Because a forbidden
time is never GENERATED, it cannot be offered — the agent has nothing to filter.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.calendar_rules import EVENING_START_HOUR, slot_times_for_day
from app.config import SLOT_LOCK_TTL_MINUTES, SLOT_MIN_LEAD_MINUTES
from app.models import (
    Booking,
    LocationType,
    Showroom,
    Slot,
    SlotStatus,
    VehicleModel,
    VehicleVariant,
)

# --------------------------------------------------------------------------- #
# Generation configuration
# --------------------------------------------------------------------------- #
# Deterministic pre-booking: the same inputs always mark the same slots, so
# "that time's taken, here are alternatives" (S-05) reproduces between runs
# instead of being staged by hand.
PREBOOK_SEED = 20260730  # delivery date; arbitrary but fixed.

# Doorstep fleet: Dubai only, evenings only (CLAUDE.md "doorstep fleet
# calendar (Dubai, evenings)").
DOORSTEP_SERVICE_AREA = "Dubai"
DOORSTEP_OPEN_HOUR = EVENING_START_HOUR  # 17:00 — same constant as the cadence.
DOORSTEP_CLOSE_HOUR = 21
DOORSTEP_HOURS_BY_DAY: dict[str, list[int]] = {
    d: [DOORSTEP_OPEN_HOUR, DOORSTEP_CLOSE_HOUR]
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}

# Every model EXCEPT the G700 is doorstep-eligible.
# BUSINESS ASSUMPTION, not confirmed by Elite: the G700 is the flagship, so
# doorstep would contradict a "come and see it" framing.
# ⚠️ ITS ORIGINAL EVIDENCE IS GONE. The rationale used to rest on the G700
# having a DEDICATED SHOWROOM (showroom 2), which was removed from the seed on
# 2026-07-23 — so the assumption now rests on "it is the flagship" alone, which
# is thinner. Left in place deliberately rather than quietly reversed: dropping
# the exclusion would ADD doorstep slots for the flagship, which is a product
# decision and a demo-visible change, not a tidy-up. Still to revisit with the
# client, now with one fewer reason behind it.
DOORSTEP_EXCLUDED_CODES: frozenset[str] = frozenset({"JET-G700"})

# --------------------------------------------------------------------------- #
# Daypart boundaries (FR-SL)
# --------------------------------------------------------------------------- #
# Evaluated on the slot's START time. EVENING_START_HOUR is imported rather than
# copied so "evening" has exactly one definition — the slot cadence and this
# filter cannot drift apart.
MORNING_END_HOUR = 12  # morning  : start <  12:00
AFTERNOON_END_HOUR = EVENING_START_HOUR  # afternoon: 12:00 <= start < 17:00
# evening: start >= 17:00

DAYPARTS = ("", "morning", "afternoon", "evening")

# FR-SL-01: offer AT MOST 3. The tool now returns exactly what the agent offers,
# no more — this is the fix for the prose/chips divergence. It used to return 6
# ("fallbacks"), the frontend sliced 3, and the two could disagree: chips could
# show a slot the agent never offered — including one the customer had just
# rejected — and a tapped chip pins the slot_id over the prose, so that booked
# the wrong slot. One set of slots, capped here, renders as both. If the customer
# rejects all three, the agent re-queries (a fresh "here are three others" turn),
# ideally narrowing with earliest_hour/date/daypart. `limit` stays overridable so
# inventory-inspection callers can still ask for everything.
DEFAULT_SLOT_LIMIT = 3


def _in_daypart(start: datetime, daypart: str) -> bool:
    if not daypart:
        return True
    hour = start.hour
    if daypart == "morning":
        return hour < MORNING_END_HOUR
    if daypart == "afternoon":
        return MORNING_END_HOUR <= hour < AFTERNOON_END_HOUR
    if daypart == "evening":
        return hour >= EVENING_START_HOUR
    return True


def daypart_of(start: datetime) -> str:
    """Which daypart a start time falls in — the inverse of _in_daypart.

    Shares the boundary constants above for the reason stated there: a second
    copy of "evening starts at 17:00" is a copy that drifts. Kept as the one
    classifier so memory.derive_from_booking learns exactly the daypart that
    query_available would later filter on — otherwise a remembered "evening"
    could fail to match any evening slot.

    Always returns a concrete daypart, never "" — a real datetime is always in
    one of the three, and "" means "no preference" to every caller.
    """
    hour = start.hour
    if hour < MORNING_END_HOUR:
        return "morning"
    if hour < AFTERNOON_END_HOUR:
        return "afternoon"
    return "evening"


def facts_for_slot(db: Session, slot_id: object) -> dict:
    """The session-facts a locked slot establishes: which car, where, and the
    daypart (2026-07-24, for the agent's session_facts layer).

    Read straight off the Slot ROW, which is the authoritative record of what was
    locked — NOT from any model argument (rule 2 grounding). The tool node needs
    this because lock_slot's own result (slots._slot_dict) carries the showroom name
    and the time but NOT the vehicle, and all DB access belongs in the service layer
    (AD-I) rather than inline in the tool node.

    Returns {} for an unknown slot so a caller merges nothing rather than clobbering
    good facts with blanks. showroom_name is omitted for a doorstep slot (there is
    no branch), matching the session_facts shape which carries showroom_name only.
    """
    slot = db.get(Slot, _coerce_int(slot_id)) if slot_id is not None else None
    if slot is None:
        return {}
    facts: dict = {"vehicle_id": slot.vehicle_id, "daypart": daypart_of(slot.start)}
    if slot.vehicle is not None:
        facts["vehicle_name"] = slot.vehicle.name
    if slot.location_type == LocationType.showroom and slot.showroom is not None:
        facts["showroom_name"] = slot.showroom.name
    return facts


def _coerce_hour(value: object) -> int | None:
    """A lower-bound hour 0-23 for earliest_hour (FR-SL-04), or None = no bound.

    GRACEFUL-IGNORE, deliberately unlike invalid daypart/date (which return an
    error): a stated time constraint is a SOFT filter, and an uninterpretable
    value must never hard-error a live availability turn — it simply drops the
    bound. Accepts an int hour, or a "HH" / "HH:MM" string so `earliest_hour:
    "11:00"` still lands on 11 (inventory is hour/half-hour grained, so the hour
    is the meaningful unit; the minutes are dropped).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 23 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if 0 <= value <= 23 else None
    if isinstance(value, str) and value.strip():
        try:
            hour = int(value.strip().split(":", 1)[0])
        except ValueError:
            return None
        return hour if 0 <= hour <= 23 else None
    return None


def _parse_iso_date(value: object) -> date | None:
    """Parse a concrete ISO date. Relative expressions are the agent's job to
    resolve before calling (see the tool description), so "tomorrow" is invalid
    input here, not something to guess at.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _human_start(start: datetime) -> str:
    """Gregorian date with day name + 12-hour clock (LOC-09)."""
    hour12 = start.strftime("%I").lstrip("0") or "12"
    minute = f":{start.minute:02d}" if start.minute else ""
    return f"{start.strftime('%A, %-d %B %Y')} at {hour12}{minute} {start.strftime('%p')}"


def _lead_cutoff(now: datetime) -> datetime:
    """The earliest start a slot may have to be OFFERED or LOCKED.

    "Not in the past" was the old bar and it is too low: a slot starting in four
    minutes is inventory nobody can reach, so offering it promises something we
    know cannot happen (FR-CX-07). The buffer is applied where availability is
    COMPUTED, so a too-soon slot is never returned and therefore can never reach
    the chips, the prose, or the call — the agent cannot offer what it is never
    given.

    DELIBERATELY NOT APPLIED IN booking.create/reschedule, which keep the plain
    `start < now` bar. A customer legitimately offered a slot 35 minutes out then
    spends the rule 9 sequence — personalisation, recap, licence, consent — on it,
    and refusing at `create_booking` would fail the one step that has no graceful
    recovery: the slot is locked, the customer has said yes, and there is nothing
    honest left to say. The buffer governs what is OFFERED and GRABBED, never what
    is COMPLETED.
    """
    return now + timedelta(minutes=max(0, SLOT_MIN_LEAD_MINUTES))


# --------------------------------------------------------------------------- #
# query_available — the ONLY source of availability (FR-SL-01)
# --------------------------------------------------------------------------- #
def query_available(
    db: Session,
    *,
    vehicle_id: int,
    location_type: str | None = None,
    showroom_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    daypart: str = "",
    earliest_hour: object = None,
    limit: int = DEFAULT_SLOT_LIMIT,
    now: datetime | None = None,
    session_id: str | None = None,
) -> dict:
    """Open slots matching the filters. Never raises; errors return as data.

    A slot THIS session holds is availability too, flagged `held_by_you`. The
    hold sets status=locked, which used to make the customer's own slot vanish
    from this query the moment they tapped it — so the confirm step re-checked,
    found it "gone", and reported the customer's own choice as taken. A held
    slot is scoped by session_id, so no OTHER session ever sees it as free.

    Past slots are excluded: yesterday's inventory is not availability. So are
    slots inside the SLOT_MIN_LEAD_MINUTES buffer — a start the customer could
    not physically reach is not availability either (see _lead_cutoff). An empty
    list is a valid, honest answer (the agent then offers alternatives) — it is
    NOT an error.

    earliest_hour (FR-SL-04) is a SERVER-SIDE lower time bound: when a customer
    rules out a time ("I've office at 9am", "nothing before 11"), the excluded
    slots are never RETURNED, so they can never reach the chips. This is the other
    half of the prose/chips fix — a stated constraint the agent only honoured in
    prose used to leave the ruled-out slot leading the results. Graceful-ignore on
    a bad value (see _coerce_hour); daypart still covers the coarse
    morning/afternoon/evening bands this refines.
    """
    now = now or datetime.now()

    # DELIBERATE WRITE IN A READ PATH — do not "clean this up".
    # Abandoned conversations must return inventory (FR-SL), and AD-F rules out
    # the background job that would normally do it, so expiry is lazy: it happens
    # on the next query. Without this call an expired lock would stay `locked`
    # and stay invisible here forever, quietly shrinking availability every time
    # someone walked away mid-booking. See _release_expired_locks.
    _release_expired_locks(db, now)

    vid = _coerce_int(vehicle_id)
    if vid is None:
        return {"error": "invalid_vehicle_id", "vehicle_id": vehicle_id, "slots": []}
    vehicle = db.get(VehicleModel, vid)
    if vehicle is None:
        return {"error": "vehicle_not_found", "vehicle_id": vid, "slots": []}

    if location_type is not None and str(location_type).strip():
        location_type = str(location_type).strip().lower()
        if location_type not in {lt.value for lt in LocationType}:
            return {
                "error": "invalid_location_type",
                "allowed": [lt.value for lt in LocationType],
                "slots": [],
            }
    else:
        location_type = None

    daypart = (daypart or "").strip().lower()
    if daypart not in DAYPARTS:
        return {"error": "invalid_daypart", "allowed": list(DAYPARTS), "slots": []}

    d_from = _parse_iso_date(date_from) if date_from else None
    d_to = _parse_iso_date(date_to) if date_to else None
    if date_from and d_from is None:
        return {"error": "invalid_date_from", "value": date_from, "slots": []}
    if date_to and d_to is None:
        return {"error": "invalid_date_to", "value": date_to, "slots": []}
    if d_from and d_to and d_from > d_to:
        return {
            "error": "invalid_date_range",
            "detail": "date_from is after date_to",
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "slots": [],
        }

    if showroom_id is not None:
        sid = _coerce_int(showroom_id)
        if sid is None or db.get(Showroom, sid) is None:
            return {"error": "showroom_not_found", "showroom_id": showroom_id, "slots": []}
        showroom_id = sid

    visible = Slot.status == SlotStatus.open
    if session_id:
        visible = or_(
            visible,
            and_(
                Slot.status == SlotStatus.locked,
                Slot.locked_by_session == session_id,
            ),
        )
    stmt = (
        select(Slot)
        .where(
            Slot.vehicle_id == vid,
            visible,
            Slot.start >= _lead_cutoff(now),
        )
        .order_by(Slot.start)
    )
    if location_type:
        stmt = stmt.where(Slot.location_type == LocationType(location_type))
    if showroom_id is not None:
        stmt = stmt.where(Slot.showroom_id == showroom_id)
    if d_from:
        stmt = stmt.where(Slot.start >= datetime.combine(d_from, time.min))
    if d_to:
        stmt = stmt.where(Slot.start <= datetime.combine(d_to, time.max))

    # daypart + earliest_hour are applied HERE (Python), then the slice — so the
    # three returned slots all satisfy every filter, and the cap is the last word.
    min_hour = _coerce_hour(earliest_hour)
    rows = [
        s
        for s in db.scalars(stmt).all()
        if _in_daypart(s.start, daypart) and (min_hour is None or s.start.hour >= min_hour)
    ]

    # --- Salesforce availability gate (fail-open) --------------------------------
    # The app computes availability from its own DB above, unchanged. Here we let
    # SALESFORCE have the final say on what is offered: any candidate slot that
    # Salesforce marks not-Available (Booked/Locked) is dropped. On ANY Salesforce
    # problem this is a no-op (booked ids come back empty), so the local result
    # stands and the app keeps working exactly as before. See services/salesforce.
    # SALESFORCE IS THE SOURCE OF TRUTH for availability: keep ONLY the slots SF
    # marks Available. A slot not in Salesforce at all (never seeded) or Booked/
    # Locked there is dropped — so the agent can only ever offer, and later book, a
    # slot that EXISTS and is Available in Salesforce. If SF is unreachable AND no
    # snapshot has ever loaded, available_slot_app_ids returns None and we leave the
    # local result as-is (cold-start degrade) rather than emptying the calendar;
    # once a snapshot exists, a transient blip serves the last-good Available set.
    # A slot this session holds is EXPECTED to be Locked in Salesforce — the
    # hold is what locked it — so the gate must not drop it: that would re-hide
    # the customer's own slot and reintroduce the exact lie the session_id
    # filter above exists to fix.
    def _held(s: Slot) -> bool:
        return bool(
            session_id
            and s.status == SlotStatus.locked
            and s.locked_by_session == session_id
        )

    try:
        from app.services import salesforce as _sf
        if _sf.enabled() and rows:
            _avail = _sf.available_slot_app_ids([s.id for s in rows])
            if _avail is not None:
                rows = [s for s in rows if s.id in _avail or _held(s)]
    except Exception:  # noqa: BLE001 — availability must never break on SF issues
        pass

    # THE CAP MUST NOT CROWD OUT THE HELD SLOT. It ties on start with the open
    # slots around it (same calendar grid) and sorts among them arbitrarily, so
    # a plain slice can drop the one slot the customer has actually chosen.
    # Held slots are kept unconditionally, the cap fills with open ones, and
    # the result goes back into start order.
    cap = max(1, _coerce_int(limit) or DEFAULT_SLOT_LIMIT)
    held_rows = [s for s in rows if _held(s)]
    if held_rows:
        rest = [s for s in rows if not _held(s)]
        rows = sorted(
            held_rows + rest[: max(cap - len(held_rows), 0)],
            key=lambda s: (s.start, s.id),
        )
    else:
        rows = rows[:cap]

    return {
        "vehicle": {"vehicle_id": vehicle.id, "name": vehicle.name},
        "slots": [
            {**_slot_dict(s), "held_by_you": True} if _held(s) else _slot_dict(s)
            for s in rows
        ],
        "count": len(rows),
    }


def _slot_dict(s: Slot) -> dict:
    out = {
        "slot_id": s.id,
        "start_iso": s.start.isoformat(),
        "start_human": _human_start(s.start),
        "end_iso": s.end.isoformat(),
        "location_type": s.location_type.value,
    }
    if s.location_type == LocationType.doorstep:
        out["service_area"] = s.service_area
    else:
        out["showroom"] = {
            "showroom_id": s.showroom_id,
            "name": s.showroom.name if s.showroom else None,
        }
    return out


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


# --------------------------------------------------------------------------- #
# Locking (FR-SL, FR-SL-06)
# --------------------------------------------------------------------------- #
# A lock holds an accepted slot while the agent completes personalisation, recap,
# licence reminder and consent — the sequence CLAUDE.md rule 9 requires BEFORE
# the booking is created. Without it, that whole exchange races every other
# customer looking at the same evening.
#
# NOTE ON THE TTL: it is never returned to the agent, deliberately. A model that
# knows the lock expires at 19:42 will eventually say "shall I hold it? it
# expires in ten minutes" — a scarcity tactic, which FR-CX-04/LOC-10 forbid
# outright. Expiry is inventory hygiene, not a conversational lever, so the agent
# is not told about it. If a lock lapses mid-conversation the customer simply
# gets a graceful re-offer from a fresh availability query.


def _release_expired_locks(db: Session, now: datetime) -> int:
    """Return timed-out locks to inventory. Called from the read path — see the
    comment in query_available. Flushes but does not commit.

    A `locked` slot with a NULL locked_at cannot be reasoned about (it should not
    exist), so it is treated as expired rather than left to hold inventory
    forever on the strength of a row we already know is wrong.
    """
    cutoff = now - timedelta(minutes=SLOT_LOCK_TTL_MINUTES)
    result = db.execute(
        update(Slot)
        .where(
            Slot.status == SlotStatus.locked,
            or_(Slot.locked_at.is_(None), Slot.locked_at < cutoff),
        )
        .values(
            status=SlotStatus.open,
            locked_at=None,
            locked_by_session=None,
            licence_ack=False,  # rule 9: the ack dies with the lock it belonged to
        )
    )
    if result.rowcount:
        db.flush()
    return result.rowcount or 0


def release_lock(db: Session, *, session_id: str, slot_id: int | None = None) -> int:
    """Release this session's live lock(s). Returns how many were released.

    Service-only: there is deliberately NO release_slot tool. A release the model
    has to remember to call is a release that gets forgotten, and a forgotten one
    is invisible until inventory looks wrong. lock() calls this itself, and
    cancel/reschedule will call it internally in a later phase.

    Scoped to `session_id`: a session can only ever release its own lock, which
    is exactly why locked_by_session exists.
    """
    stmt = update(Slot).where(
        Slot.status == SlotStatus.locked, Slot.locked_by_session == session_id
    )
    if slot_id is not None:
        stmt = stmt.where(Slot.id == slot_id)
    result = db.execute(
        stmt.values(
            status=SlotStatus.open,
            locked_at=None,
            locked_by_session=None,
            licence_ack=False,  # rule 9: the ack dies with the lock it belonged to
        )
    )
    if result.rowcount:
        db.flush()
    return result.rowcount or 0


# Rides every successful lock result. lock_slot's schema already says a hold is
# not a booking, but a schema description is read once at session start and a
# result is read at the moment it matters — and on the realtime call the model
# once told a customer they were booked off a bare {'locked': true} (the
# phantom-confirmation incident, 2026-07-31). Data-level, so both engines and
# any future channel get it for free. No expiry/TTL in here, ever (rule 14).
_HOLD_NOTE = (
    "Slot HELD for this conversation — NOT booked. Do not tell the customer "
    "they are booked or confirmed. A booking exists only when create_booking "
    "succeeds and returns a booking_ref."
)


def lock(db: Session, *, session_id: str, slot_id: int, now: datetime | None = None) -> dict:
    """Hold a slot for this session. Never raises; failure is returned as data.

    AUTO-RELEASE: taking a lock first releases any LIVE lock this session already
    holds, so "actually, make it 8pm" cannot strand the 7pm slot for the full
    TTL. One live lock per session, enforced structurally rather than by asking
    the model to tidy up after itself.

    That invariant is a bounded constraint, not a lost feature: two test drives
    in one conversation still work, because lock -> create_booking moves the slot
    to `booked`, which holds no LIVE lock for the next lock() to release. It only
    bites if the agent locks two slots before booking either, which is not the
    flow (rule 9 books each slot at the end of its own recap).

    `session_id` is injected by the executor and never appears in the tool
    schema. That is the whole basis of create_booking's first write guard: a
    session identity the model supplied would be an assertion it could simply
    change, not a fact we can enforce against.
    """
    now = now or datetime.now()
    _release_expired_locks(db, now)

    sid = _coerce_int(slot_id)
    if sid is None:
        return {"locked": False, "reason": "invalid_slot_id", "slot_id": slot_id}

    slot = db.get(Slot, sid)
    if slot is None:
        return {"locked": False, "reason": "slot_not_found", "slot_id": sid}

    # Already ours: idempotent. The model retries, and a retry must not be
    # punished by releasing our own lock and re-taking it.
    if slot.status == SlotStatus.locked and slot.locked_by_session == session_id:
        return {
            "locked": True,
            "slot": _slot_dict(slot),
            "already_held": True,
            "note": _HOLD_NOTE,
        }

    if slot.status == SlotStatus.booked:
        return {"locked": False, "reason": "slot_no_longer_available", "slot_id": sid}

    if slot.status == SlotStatus.locked:
        # Someone else is mid-booking on it. Same answer as booked, on purpose:
        # the distinction is ours, not the customer's, and "another customer is
        # currently booking this" is not information we should leak or pressure
        # anyone with.
        return {"locked": False, "reason": "slot_no_longer_available", "slot_id": sid}

    if slot.start < now:
        return {"locked": False, "reason": "slot_in_the_past", "slot_id": sid}

    # Inside the lead buffer: real, open, and still unreachable. Its OWN reason,
    # never slot_in_the_past — that would be a false statement about a slot which
    # has not happened yet, and the agent would repeat it to the customer (rule
    # 2). query_available already withholds these, so reaching here means a stale
    # chip or an id the model carried over from an earlier turn.
    if slot.start < _lead_cutoff(now):
        return {"locked": False, "reason": "slot_too_soon", "slot_id": sid}

    # Only now that this lock is certain to succeed do we release the previous
    # one. Releasing first would strand the customer with NOTHING held if the new
    # slot turned out to be taken.
    released = release_lock(db, session_id=session_id)

    slot.status = SlotStatus.locked
    slot.locked_at = now
    slot.locked_by_session = session_id
    # A fresh lock starts UNACKNOWLEDGED (rule 9). Explicit rather than relying on
    # the column default: this slot may have been locked, acknowledged and
    # released earlier in the same conversation, and a stale True would let the
    # second booking skip the reminder the customer never heard for it.
    slot.licence_ack = False
    db.flush()

    return {
        "locked": True,
        "slot": _slot_dict(slot),
        "released_previous_lock": bool(released),
        "note": _HOLD_NOTE,
    }


def held_by_session(db: Session, *, session_id: str, now: datetime | None = None) -> dict | None:
    """The slot this session is holding right now, or None. Read-only.

    Feeds the prompt's bookings anchor (2026-07-27) so an unfinished hold is a fact
    in front of the model rather than a detail it has to remember. A customer who
    picked a new time, then changed the subject before confirming, used to have
    that hold silently abandoned: the agent moved on, the slot sat locked until the
    TTL expired, and the reschedule the customer actually came for never happened.

    IT DOES NOT RELEASE EXPIRED LOCKS, unlike every other reader here, and that is
    the point of the cutoff below. _release_expired_locks is a WRITE, and this is
    called from the agent node, which owns no transaction — the tool node does, and
    it is the only place allowed to commit. So an expired lock is filtered out by
    the query instead of tidied up by it, and the real cleanup still happens on the
    next query_available exactly as before. Same answer, no write.

    Returns the same _slot_dict shape the lock/availability paths return, so the
    anchor renders a held slot with the same words it would have offered it in.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=SLOT_LOCK_TTL_MINUTES)
    slot = db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.locked,
            Slot.locked_by_session == session_id,
            Slot.locked_at.isnot(None),
            Slot.locked_at >= cutoff,
        )
    ).first()
    return _slot_dict(slot) if slot is not None else None


def acknowledge_licence_reminder(db: Session, *, session_id: str) -> dict:
    """Record that the driving-licence reminder was given for this session's lock.

    Rule 9, precondition 3, enforced as ORDER: create_booking refuses until this
    has been recorded against the live lock. Scoped to the slot THIS session
    holds — the session_id is injected by the executor and never modelled, so the
    model cannot acknowledge on behalf of another conversation.

    Requires a live lock on purpose. "Remind, then lock" is not the rule 9
    sequence; the reminder belongs to a specific held slot, and without a lock
    there is nothing for it to belong to.

    Never raises; failure is returned as data so the agent can recover in prose.
    """
    _release_expired_locks(db, datetime.now())

    slot = db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.locked,
            Slot.locked_by_session == session_id,
        )
    ).first()
    if slot is None:
        # THE FAILURE MUST SAY WHAT IT IS NOT (found live, 2026-09-01). A
        # customer accepted the one offered slot in words, no lock was ever
        # taken, and the agent read this bare reason as the slot being GONE —
        # told the customer their still-open time was unavailable and re-offered
        # other times. Data-level for the same reason as _HOLD_NOTE: a schema
        # description is read once at session start; a result is read at the
        # moment it matters, by every engine and channel alike.
        return {
            "acknowledged": False,
            "reason": "no_locked_slot",
            "detail": (
                "No slot is held by this conversation. This does NOT mean any "
                "slot is taken or unavailable — do not tell the customer that. "
                "Lock the slot they accepted with lock_slot, give the "
                "reminder, then call this again."
            ),
        }

    slot.licence_ack = True
    db.flush()
    return {"acknowledged": True, "slot_id": slot.id}


# --------------------------------------------------------------------------- #
# generate — CLI only, never a tool
# --------------------------------------------------------------------------- #
def generate(
    db: Session,
    *,
    days: int = 14,
    prebooked: float = 0.18,
    reset: bool = False,
    force: bool = False,
    start_day: date | None = None,
) -> dict:
    """Generate a rolling window of test-drive inventory.

    GRAIN IS (showroom, VARIANT) as of 2026-09-03, and the premise it rests on
    is stated here rather than assumed: a showroom is taken to keep one demo car
    per BOOKABLE TRIM, not one per model. The grain was (showroom, model) until
    then, on the older premise of one demo car per model — which made the trim
    both unaskable and unrecordable, so a customer booked "a G700" and drove
    whichever of four trims, AED 169,000 to 240,450 apart, was on the floor.

    THE PREMISE IS ELITE'S TO CONFIRM and is logged as such (OPEN-ISSUES §7). If
    a branch turns out to hold fewer demo cars than it has trims, the fix is a
    demo-fleet list in the seed, not a change of grain here: this function would
    then iterate that list, and nothing downstream of it would move.

    Sale stock is deliberately NOT the gate. `stock` counts units for SALE, per
    (variant, showroom, colour); a trim that sells out is still drivable, and
    gating on it would invent a new wrong answer — "you cannot test drive the
    Flagship, we sold out" — in place of the one this change removes.

    Coming-soon trims are excluded, because a car that cannot be ordered cannot
    be driven. Showroom 2 ends up G700-only because only the G700 lists it in
    showroom_ids — that falls out of the data, with no special case.

    Times come from calendar_rules.slot_times_for_day() using each showroom's
    hours_by_day, so the Friday prayer window (FR-SL-02, S-22) and Sunday's
    12:00 open are honoured by construction.

    Re-runnable: the 14-day window goes stale, and inventory gets consumed by
    test runs. `reset` clears existing slots first, but ABORTS if any booking
    references a slot — silently orphaning a booking would corrupt the
    reschedule/cancel journeys. `force` overrides.
    """
    start_day = start_day or date.today()
    days = max(1, _coerce_int(days) or 14)
    prebooked = min(max(float(prebooked), 0.0), 1.0)

    if reset:
        booked_refs = db.scalar(
            select(func.count()).select_from(Booking).where(Booking.slot_id.isnot(None))
        )
        if booked_refs and not force:
            return {
                "error": "bookings_reference_slots",
                "bookings": booked_refs,
                "detail": (
                    f"{booked_refs} booking(s) reference existing slots. Re-run with "
                    "--force to delete anyway (this will orphan those bookings)."
                ),
            }
        db.execute(delete(Slot))
        db.flush()
    else:
        # REFUSE TO DOUBLE-GENERATE. Without --reset this used to APPEND a second
        # rolling window on top of the first, silently doubling every
        # (vehicle, showroom, start) — 3,612 duplicate groups, two 9 AM chips for
        # the same car, and a chip label that hides which slot_id a tap pins.
        # That cost a live-severity bug once; make it impossible to do by accident.
        # --reset replaces the inventory; --force is the explicit "append anyway"
        # escape hatch. Neither, against a non-empty table, aborts as data.
        existing = db.scalar(select(func.count()).select_from(Slot))
        if existing and not force:
            return {
                "error": "inventory_not_empty",
                "existing": existing,
                "detail": (
                    f"{existing} slot(s) already exist. Generating without --reset would "
                    "DOUBLE the inventory (duplicate slots). Re-run with --reset to replace "
                    "them, or --force to append anyway."
                ),
            }

    showrooms = list(db.scalars(select(Showroom).order_by(Showroom.id)).all())
    vehicles = list(db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all())
    # THE DEMO FLEET IS PER TRIM, NOT PER MODEL (2026-09-03). See the grain note
    # in this function's docstring. `coming_soon` is the bookability predicate
    # and not a name: the G700 Top Fire carries it, has no price because it
    # cannot be ordered, and therefore cannot be driven either. A model whose
    # every variant is coming soon contributes no inventory at all, which is the
    # correct answer rather than an empty special case.
    bookable: dict[int, list[VehicleVariant]] = {}
    for variant in db.scalars(
        select(VehicleVariant)
        .where(VehicleVariant.coming_soon.is_(False))
        .order_by(VehicleVariant.id)
    ).all():
        bookable.setdefault(variant.model_id, []).append(variant)

    created: list[Slot] = []

    # --- showroom inventory: each vehicle's OWN showroom_ids, per trim ------- #
    for s in showrooms:
        for v in vehicles:
            if s.id not in (v.showroom_ids or []):
                continue
            for variant in bookable.get(v.id, ()):
                for offset in range(days):
                    day = start_day + timedelta(days=offset)
                    for start, end in slot_times_for_day(day, s.hours_by_day or {}):
                        created.append(
                            Slot(
                                location_type=LocationType.showroom,
                                showroom_id=s.id,
                                service_area=None,
                                vehicle_id=v.id,
                                variant_id=variant.id,
                                start=start,
                                end=end,
                                status=SlotStatus.open,
                            )
                        )

    # --- doorstep fleet: Dubai, evenings, all models but the flagship ------- #
    # PER TRIM TOO, and deliberately not "one doorstep car per model". The
    # exclusion above is by model_code because it is the FLAGSHIP MODEL that
    # does not travel; within a model that does, a customer who asked for the
    # 1.6 Luxury at their door must not be sent the 1.5 Standard. Splitting the
    # grain here and not there would put the two paths back in disagreement,
    # which is the shape of defect this file has already paid for twice.
    for v in vehicles:
        if v.model_code in DOORSTEP_EXCLUDED_CODES:
            continue
        for variant in bookable.get(v.id, ()):
            for offset in range(days):
                day = start_day + timedelta(days=offset)
                for start, end in slot_times_for_day(day, DOORSTEP_HOURS_BY_DAY):
                    created.append(
                        Slot(
                            location_type=LocationType.doorstep,
                            showroom_id=None,
                            service_area=DOORSTEP_SERVICE_AREA,
                            vehicle_id=v.id,
                            variant_id=variant.id,
                            start=start,
                            end=end,
                            status=SlotStatus.open,
                        )
                    )

    # --- deterministic pre-booking (S-05) ----------------------------------- #
    # Sort first so the sample does not depend on insertion order.
    created.sort(
        key=lambda s: (
            s.start, s.location_type.value, s.showroom_id or 0,
            s.vehicle_id, s.variant_id or 0,
        )
    )
    n_prebook = int(round(len(created) * prebooked))
    if n_prebook:
        rng = random.Random(PREBOOK_SEED)
        for s in rng.sample(created, n_prebook):
            s.status = SlotStatus.booked

    db.add_all(created)
    db.commit()

    # --- report -------------------------------------------------------------- #
    per_showroom: list[dict] = []
    for s in showrooms:
        rows = [x for x in created if x.showroom_id == s.id]
        per_showroom.append(
            {
                "showroom_id": s.id,
                "name": s.name,
                "vehicles": len({x.vehicle_id for x in rows}),
                # BOTH NUMBERS, because they stopped being the same one on
                # 2026-09-03: nine models are fourteen demo cars once the grain
                # is per trim, and a report that said only "vehicles: 9" would
                # describe a branch's fleet as a third smaller than it is.
                "variants": len({x.variant_id for x in rows}),
                "open": sum(1 for x in rows if x.status == SlotStatus.open),
                "booked": sum(1 for x in rows if x.status == SlotStatus.booked),
                "total": len(rows),
            }
        )
    door = [x for x in created if x.location_type == LocationType.doorstep]
    doorstep = {
        "service_area": DOORSTEP_SERVICE_AREA,
        "vehicles": len({x.vehicle_id for x in door}),
        "variants": len({x.variant_id for x in door}),
        "open": sum(1 for x in door if x.status == SlotStatus.open),
        "booked": sum(1 for x in door if x.status == SlotStatus.booked),
        "total": len(door),
    }
    total_booked = sum(1 for x in created if x.status == SlotStatus.booked)
    return {
        "days": days,
        "window_start": start_day.isoformat(),
        "window_end": (start_day + timedelta(days=days - 1)).isoformat(),
        "total": len(created),
        "open": len(created) - total_booked,
        "booked": total_booked,
        "prebooked_ratio": round(total_booked / len(created), 4) if created else 0.0,
        "per_showroom": per_showroom,
        "doorstep": doorstep,
    }
