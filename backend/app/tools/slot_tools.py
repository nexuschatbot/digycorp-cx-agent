"""Slot availability + locking tools (schema + thin fn).

There is deliberately NO generation tool here. Slots are inventory; the agent
reads availability and never creates it. Generation is CLI-only
(`python -m app.cli generate-slots`), calling the same service.

There is also deliberately no release_slot tool — see slots.release_lock. A
release the model must remember to call is a release that gets forgotten, and
lock_slot auto-releases the session's previous hold anyway.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import slots

# --------------------------------------------------------------------------- #
# get_available_slots (FR-SL-01/02, S-22)
# --------------------------------------------------------------------------- #
GET_AVAILABLE_SLOTS_SCHEMA = {
    "name": "get_available_slots",
    "description": (
        "Real, currently-open test-drive slots. This is the ONLY source of "
        "availability: never reason about what is probably free, never infer a "
        "time from opening hours, and never offer a time that is not in this "
        "response.\n\n"
        "UAE calendar rules are enforced server-side. Weekends, showroom "
        "opening hours, public holidays and the Friday prayer window are "
        "already applied — a forbidden time does not exist in the data and "
        "cannot be returned. Do not filter results yourself, do not apologise "
        "for a Friday slot (you will never be offered one), and do not explain "
        "the rules unless the customer asks.\n\n"
        "date_from and date_to must be concrete ISO dates (YYYY-MM-DD). You "
        "resolve relative expressions yourself — 'tomorrow evening', 'ba'd "
        "bukra', 'after Maghrib', 'this weekend' — and confirm the resolved "
        "date with the customer before calling.\n\n"
        "This returns AT MOST 3 slots. Offer exactly the slots it returns — all "
        "of them, in your words — and no others; never mention a time that is "
        "not in this response. If the customer declines all of them, call this "
        "again for three others (a later date, a different showroom, or a "
        "narrower daypart/earliest_hour) — do not offer times you were not "
        "given.\n\n"
        "CARRY THE CUSTOMER'S TIME CONSTRAINT INTO THE QUERY. If they rule a time "
        "out — 'I've office at 9am', 'nothing before 11', 'I can't do mornings' — "
        "pass earliest_hour so the excluded slots are never returned. Do NOT just "
        "skip them in your reply while still asking for them here: a slot you "
        "leave in the query can still be offered to the customer as a tappable "
        "option, which is exactly the time they told you they cannot make.\n\n"
        "An empty 'slots' list is a valid, honest answer, not a failure: it "
        "means we have nothing matching. Say so and offer the nearest real "
        "alternative — a different day, showroom, or a doorstep drive. Each "
        "slot has start_human for speaking to the customer and start_iso for "
        "your own reasoning; quote the human form, in their language.\n\n"
        "A slot marked held_by_you is NOT taken: it is the slot THIS customer "
        "already chose, held for them while the booking is finished. Never "
        "report a held_by_you slot as unavailable and never offer alternatives "
        "to it — finishing the booking on that slot is the next step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vehicle_id": {
                "type": "integer",
                "description": (
                    "The vehicle the customer wants to drive. Get this from "
                    "search_vehicles or get_vehicle_details first — never guess."
                ),
            },
            "variant_id": {
                "type": "integer",
                "description": (
                    "Which TRIM they are coming to drive, when the model has "
                    "more than one. Each trim is a separate demo car with its "
                    "own times, so omitting this asks for any trim of the "
                    "model and the customer may be shown a car they did not "
                    "choose.\n\n"
                    "Get it from get_variants or get_vehicle_details — never "
                    "guess, and never infer it from the price they mentioned. "
                    "If the model has only ONE trim, omit this: there is "
                    "nothing to choose and asking would be noise.\n\n"
                    "Reading the errors: 'variant_not_of_vehicle' means the "
                    "trim belongs to a different model — re-read the ids, do "
                    "NOT tell the customer anything is unavailable. "
                    "'variant_coming_soon' means that trim cannot be driven "
                    "yet; say so plainly and offer the trims that can, never "
                    "that the branch is full."
                ),
            },
            "location_type": {
                "type": "string",
                "enum": ["showroom", "doorstep"],
                "description": (
                    "'showroom' = the customer comes to us. 'doorstep' = we "
                    "bring the car to them (Dubai, evenings only). Offer the "
                    "choice at the START of slot selection, as ONE question on "
                    "its own, before you ask about days or times, and pass what "
                    "they answer. Omit to see both."
                ),
            },
            "showroom_id": {
                "type": "integer",
                "description": (
                    "Restrict to one showroom, once the customer has chosen "
                    "one. Omit to see every showroom stocking the vehicle.\n\n"
                    "PASS IT WHENEVER A BRANCH IS ALREADY IN PLAY, and that "
                    "includes the branch an existing booking is at when you are "
                    "RESCHEDULING it. A customer changing the time of a booking "
                    "has not asked to change its city, so leaving this out and "
                    "offering whatever came back first is how they get moved "
                    "across the emirate without being asked. If the branch they "
                    "want has nothing, call again without it and say the branch "
                    "had nothing BEFORE naming the alternatives."
                ),
            },
            "date_from": {
                "type": "string",
                "description": (
                    "Earliest date to consider, as a concrete ISO date "
                    "YYYY-MM-DD. Never pass a relative expression."
                ),
            },
            "date_to": {
                "type": "string",
                "description": (
                    "Latest date to consider, as a concrete ISO date "
                    "YYYY-MM-DD. Must not be earlier than date_from."
                ),
            },
            "daypart": {
                "type": "string",
                "enum": ["", "morning", "afternoon", "evening"],
                "description": (
                    "Time of day the customer prefers: morning is before "
                    "12:00, afternoon 12:00-17:00, evening from 17:00. Pass "
                    "\"\" or omit when they have not said."
                ),
            },
            "earliest_hour": {
                "type": "integer",
                "minimum": 0,
                "maximum": 23,
                "description": (
                    "The earliest hour (24h clock, 0-23) a slot may START, when "
                    "the customer has ruled out earlier times. 'Nothing before "
                    "11am' -> 11. 'I've office until 10' -> 10 or 11. This is a "
                    "finer bound than daypart (11am is still 'morning'), applied "
                    "server-side so ruled-out slots never come back. Omit when "
                    "they have set no lower bound.\n\n"
                    "A DRIVE THEY ALREADY HAVE THAT DAY IS ALSO A LOWER BOUND. "
                    "When they are booking a SECOND car for a day they are "
                    "already coming in, or ask for something 'back to back', "
                    "look that booking up and pass the hour it ENDS. Two drives "
                    "cannot overlap, so a slot starting before the first one "
                    "finishes is not an option you may offer."
                ),
            },
        },
        "required": ["vehicle_id"],
    },
}


def get_available_slots(
    db: Session,
    session_id: str,
    vehicle_id: int | None = None,
    variant_id: int | None = None,
    location_type: str | None = None,
    showroom_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    daypart: str = "",
    earliest_hour: object = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over slots.query_available (AD-J: in-process, never HTTP).

    session_id goes through so the session's OWN held slot stays visible,
    flagged `held_by_you` — see the note on query_available.
    """
    if vehicle_id is None:
        return {"error": "missing_vehicle_id", "slots": []}
    return slots.query_available(
        db,
        vehicle_id=vehicle_id,
        variant_id=variant_id,
        location_type=location_type,
        showroom_id=showroom_id,
        date_from=date_from,
        date_to=date_to,
        daypart=daypart or "",
        earliest_hour=earliest_hour,
        session_id=session_id,
    )


# --------------------------------------------------------------------------- #
# lock_slot (FR-SL-06)
# --------------------------------------------------------------------------- #
LOCK_SLOT_SCHEMA = {
    "name": "lock_slot",
    "description": (
        "Hold a slot the customer has just accepted, so nobody else takes it "
        "while you finish the booking. Call it the MOMENT they choose a time — "
        "before the personalisation questions, before the recap — because that "
        "whole exchange takes minutes and other customers are looking at the "
        "same evening. A customer saying yes to a time you offered IS choosing "
        "a time — a typed or spoken yes needs this call every bit as much as a "
        "tapped one.\n\n"
        "Holding a slot is not booking it. You must still do the personalisation "
        "exchange, the full recap, the driving-licence reminder and the consent "
        "question, and then call create_booking. Never tell the customer they "
        "are booked because a lock succeeded.\n\n"
        "Read the response:\n"
        "- {'locked': true}: it is held. Carry on.\n"
        "- {'locked': false, 'reason': 'slot_no_longer_available'}: someone took "
        "it while you were talking. This is normal and not the customer's fault "
        "— apologise briefly and naturally, call get_available_slots again, and "
        "offer what is genuinely free now. Never re-offer the lost slot.\n"
        "- {'locked': false, 'reason': 'slot_not_found' | 'invalid_slot_id'}: "
        "you used an id that does not exist. Re-query availability; never invent "
        "a slot id.\n"
        "- {'locked': false, 'reason': 'slot_too_soon'}: that start is now too "
        "close to arrange. Say so plainly and warmly — it is about our own "
        "preparation, never the customer being late — call get_available_slots "
        "again and offer the nearest real alternatives.\n\n"
        "If the customer changes their mind ('actually, make it 8pm'), just call "
        "lock_slot again with the new slot — the previous hold is released for "
        "you. There is nothing to undo and no need to mention it.\n\n"
        "Holds do expire, so do not leave a booking half-finished. Never use "
        "expiry as pressure: do not tell the customer their slot is about to "
        "lapse, do not hurry them, and do not imply scarcity. If a hold does "
        "lapse, re-query and re-offer as though nothing happened."
    ),
    "input_schema": {
        "type": "object",
        "required": ["slot_id"],
        "properties": {
            "slot_id": {
                "type": "integer",
                "description": (
                    "The slot_id of the slot the customer accepted, taken from a "
                    "get_available_slots response. Never guess or construct one."
                ),
            },
        },
    },
}


def lock_slot(
    db: Session,
    session_id: str,
    slot_id: int | None = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over slots.lock (AD-J: in-process, never HTTP).

    session_id is injected by the executor, never sent by the model — it is what
    makes the lock OWNED, and therefore what makes create_booking's first write
    guard enforceable rather than a formality.
    """
    if slot_id is None:
        return {"locked": False, "reason": "invalid_slot_id", "slot_id": None}
    return slots.lock(db, session_id=session_id, slot_id=slot_id)


# --------------------------------------------------------------------------- #
# acknowledge_licence_reminder (rule 9 precondition 3, FR-BK-06)
# --------------------------------------------------------------------------- #
ACKNOWLEDGE_LICENCE_REMINDER_SCHEMA = {
    "name": "acknowledge_licence_reminder",
    "description": (
        "Record that you have reminded the customer to bring their driving "
        "licence on the day. create_booking will REFUSE until you have called "
        "this, so it is a required step of every booking, not an optional one.\n\n"
        "Call it only AFTER you have actually given the reminder in your reply — "
        "the point of the reminder is that the customer hears it, not that this "
        "tool ran. Do not call it in advance, and never call it for a reminder "
        "you did not give.\n\n"
        "Say it in your own warm words, in the customer's language, in the "
        "message AFTER the customer has confirmed the recap, never inside the "
        "recap itself — something like 'just bring your driving licence on the "
        "day'. It is something you TELL them, not something you ask, so it may "
        "share that message with the ONE question that follows it, which is the "
        "consent question. The recap message asks whether the details are right "
        "and carries nothing else the customer could answer. "
        "There is no fixed wording, and it should never read like a legal notice. "
        "If someone else is driving (you passed driver_name), it is THEIR licence "
        "that matters — remind them of that instead.\n\n"
        "Takes no arguments: it applies to the slot this conversation is "
        "currently holding.\n\n"
        "Read the response:\n"
        "- {'acknowledged': true}: recorded. Carry on to consent and booking.\n"
        "- {'acknowledged': false, 'reason': 'no_locked_slot'}: this conversation "
        "is not holding a slot yet. It does NOT mean any slot is taken — never "
        "tell the customer a time is unavailable because of this. If they have "
        "already accepted an offered slot, call lock_slot for that exact slot "
        "now, then remind and call this again; only when no time has been "
        "chosen do you ask them to pick one."
    ),
    "input_schema": {
        "type": "object",
        "required": [],
        "properties": {},
    },
}


def acknowledge_licence_reminder(
    db: Session,
    session_id: str,
    **_ignored: object,
) -> dict:
    """Thin wrapper over slots.acknowledge_licence_reminder (AD-J: in-process).

    No modelled arguments AT ALL, by design. The acknowledgement applies to the
    slot this session holds, resolved server-side from the injected session_id —
    a slot_id the model supplied would let it acknowledge one slot and book
    another, which is precisely the ordering this guard exists to enforce.
    """
    return slots.acknowledge_licence_reminder(db, session_id=session_id)
