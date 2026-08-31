"""Total erasure — "forget me" as a full delete (rule 12, S-08, PDPL).

WHAT CHANGED AND WHY IT NEEDED ITS OWN MODULE (developer decision 2026-07-23).
`memory.forget` used to delete the MemoryProfile, the voice notes and the
read-aloud clips, and stop there. That satisfied rule 12 as WRITTEN ("hard-delete
profile") and satisfied nobody who said the words. Verified on a scratch DB: after
a confirmed forget, the customer's NAME, MOBILE, TITLE and every BOOKING were
still sitting in the database. A person told "done, we've cleared everything" while
we keep their phone number has been misled, and this is the erasure S-08 walks
through on screen in front of the client.

It also left the system arguing with itself: the voice-note delete reasons that
keeping the audio would be "a tombstone of exactly the kind the hard-delete rule
exists to refuse", while the name and mobile — more identifying than a recording
of someone saying "Saturday works" — stayed put.

So erasure is now A TO Z: every row in every table that refers to this customer,
plus the agent's own memory of them. That is a different job from
services/memory.py, which owns a consent-gated preferences row; folding a
cross-table purge into it would have made "memory" mean two things.

THE ONE THING THAT SURVIVES is a single PII-FREE receipt (AuditType.erasure)
recording that a customer asked to be forgotten and what was removed. It carries
no name and no mobile — only a customer_id that now points at a deleted row, i.e.
an orphan integer. Without it there is no evidence we honoured the request at all,
which is a worse position to be in than holding one anonymous line.

⚠️ THIS DELETES CONFIRMED FUTURE BOOKINGS. Someone who says "forget me" on
Thursday and had a Saturday test drive no longer has one, and the showroom has no
record of why. That is the intended behaviour of a full erasure, not an oversight
— but it is exactly why the agent MUST name the booking and its date before
asking for confirmation. See tools/memory_tools.py.

⚠️ WHAT WE CANNOT ERASE, and must never claim to: WhatsApp messages already
delivered. Those are on the customer's phone and on Meta's servers. Deleting our
notification row removes OUR copy and unsends nothing.
"""

from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    Booking,
    BookingStatus,
    ConversationTurn,
    TurnTrace,
    Customer,
    DeviceLink,
    HandoffToken,
    MemoryProfile,
    FeedbackSurvey,
    Notification,
    Slot,
    SlotStatus,
    SpeechClip,
    VoiceNote,
    Waitlist,
)

# Booking states that still hold their slot. A cancelled booking already released
# it, so re-opening that slot here would hand back inventory twice.
_SLOT_HOLDING = (BookingStatus.confirmed, BookingStatus.rescheduled)


# --------------------------------------------------------------------------- #
# Finding everything that belongs to one person
# --------------------------------------------------------------------------- #
def sessions_for_customer(db: Session, customer_id: int, *, current: str | None = None) -> set[str]:
    """Every conversation we can attribute to this customer.

    BEST EFFORT, AND THE LIMIT IS STRUCTURAL — say so rather than imply a
    complete sweep. ConversationTurn carries a session_id but no customer_id, so
    there is no direct customer -> conversations edge anywhere in the schema. The
    only links that exist are:

      - VoiceNote.customer_id, backfilled at identification
      - AuditEvent payloads, which carry customer_id on the tool calls that
        identified or wrote for them
      - the session asking for the erasure, passed in as `current`

    A conversation where the customer was identified but no audited tool call
    ever ran is therefore unreachable BY THIS FUNCTION.

    THAT GAP IS NOW CLOSED ELSEWHERE (T4.6), and this docstring is left standing
    because the limit it describes is still true of THIS function. ConversationTurn
    carries a customer_id from 2026-08-28, so `forget` deletes transcripts by
    CUSTOMER as well as by session — the two run together, because turns spoken
    before identification carry a null customer_id and are only reachable by
    session, while turns in a session this function never found are only
    reachable by customer. Neither sweep is redundant.
    """
    found: set[str] = set()
    if current:
        found.add(current)

    for (sess,) in db.execute(
        select(VoiceNote.session_id).where(VoiceNote.customer_id == customer_id)
    ):
        if sess:
            found.add(sess)

    # SQLite has no portable JSON path query across our supported engines
    # (Postgres in prod, SQLite in dev), so this reads the payloads in Python.
    # The table is a demo-scale append-only log; a scan is cheaper than a
    # dialect-specific query that would then need two implementations.
    for row in db.scalars(select(AuditEvent)):
        payload = row.payload or {}
        if isinstance(payload, dict) and payload.get("customer_id") == customer_id:
            if row.session:
                found.add(row.session)

    return found


def _booking_refs(db: Session, customer_id: int) -> list[str]:
    return [
        b.ref
        for b in db.scalars(select(Booking).where(Booking.customer_id == customer_id))
    ]


# --------------------------------------------------------------------------- #
# The inventory the customer is shown BEFORE anything is deleted
# --------------------------------------------------------------------------- #
def inventory(db: Session, customer_id: int, *, session_id: str | None = None) -> dict:
    """What a confirmed erasure would remove, for the agent to read out.

    THE BOOKINGS ARE THE POINT. A customer can consent to losing preferences
    without realising they are also cancelling Saturday's test drive, and finding
    that out at the showroom is the one outcome this whole handshake exists to
    prevent. They are returned with a human-readable time precisely so the agent
    has no excuse to summarise them as "your bookings".
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        return {}

    sessions = sessions_for_customer(db, customer_id, current=session_id)

    upcoming = []
    for b in db.scalars(select(Booking).where(Booking.customer_id == customer_id)):
        if b.status not in _SLOT_HOLDING:
            continue
        slot = db.get(Slot, b.slot_id)
        upcoming.append(
            {
                "booking_ref": b.ref,
                # Rendered here rather than left as an ISO string: this exists to
                # be SPOKEN, and the agent re-deriving a day name from a timestamp
                # is how FR-SL-04 gets broken.
                "when_human": slots_human(slot) if slot is not None else None,
            }
        )

    return {
        "contact_details": bool(customer.name_en or customer.name_ar or customer.mobile),
        "bookings": upcoming,
        "has_profile": db.scalar(
            select(MemoryProfile).where(MemoryProfile.customer_id == customer_id)
        )
        is not None,
        "voice_notes": db.query(VoiceNote).filter(VoiceNote.customer_id == customer_id).count(),
        # Feedback surveys (2026-07-26). Counted so the agent can NAME them before
        # asking: the scores alone are arguably anonymous, but the open comment is
        # free prose in the customer's own words and can identify both them and a
        # member of staff. A customer told "we've cleared everything" while we
        # keep "the salesman in Deira was rude" has been misled.
        "feedback": db.query(FeedbackSurvey)
        .filter(FeedbackSurvey.customer_id == customer_id)
        .count(),
        "conversations": len(sessions),
    }


def slots_human(slot: Slot) -> str:
    """Day name + date + 12-hour time (LOC-09). Imported lazily by inventory so
    this module does not depend on the slots service just for one formatter, and
    REUSED rather than re-rolled: the booking recap already speaks slot times
    through _human_start, and two formatters would eventually disagree about the
    same appointment."""
    from app.services.slots import _human_start

    return _human_start(slot.start)


# --------------------------------------------------------------------------- #
# The purge
# --------------------------------------------------------------------------- #
def purge(
    db: Session,
    *,
    customer_id: int,
    session_id: str,
    now: datetime | None = None,
    correlation_id: str | None = None,
    reason: str = "customer_request",
) -> dict:
    """Delete every trace of this customer. ONE TRANSACTION, never raises.

    ORDER IS FK-SAFE AND SLOT-SAFE. Children before parents, and the slots are
    captured BEFORE the bookings that point at them are deleted — Booking holds
    slot_id, not the reverse, so once the booking row is gone there is nothing
    left to say which slot to reopen. Skip that and the slot stays `booked`
    forever with nothing pointing at it: inventory that can never be sold and
    never be diagnosed.
    """
    now = now or datetime.now()

    customer = db.get(Customer, customer_id)
    if customer is None:
        # IDEMPOTENT. A retry, a double-tap, or a model calling twice must read
        # as "already gone", never as an error — the customer asked once and the
        # outcome they wanted is the outcome they have.
        return {"forgotten": True, "already_erased": True, "customer_id": customer_id}

    sessions = sessions_for_customer(db, customer_id, current=session_id)
    refs = _booking_refs(db, customer_id)

    # --- capture the slots BEFORE the bookings vanish ----------------------- #
    slot_ids = [
        b.slot_id
        for b in db.scalars(select(Booking).where(Booking.customer_id == customer_id))
        if b.status in _SLOT_HOLDING and b.slot_id is not None
    ]

    counts: dict[str, int] = {}

    def _run(label: str, stmt) -> None:
        counts[label] = int(db.execute(stmt).rowcount or 0)

    # Notifications hang off the booking REF, not a foreign key, so they must go
    # by ref while we still know what the refs were.
    if refs:
        _run("notifications", sa_delete(Notification).where(Notification.ref.in_(refs)))
    else:
        counts["notifications"] = 0

    # Feedback surveys, BEFORE the bookings — FeedbackSurvey.booking_ref is a real
    # foreign key to bookings.ref (unlike Notification.ref, which is a bare
    # string), so deleting the bookings first would either violate the constraint
    # or, on a SQLite file with enforcement off, silently leave survey rows
    # pointing at a booking that no longer exists. Deleted by CUSTOMER rather than
    # by ref so a survey whose booking has already gone cannot be stranded.
    _run(
        "feedback_surveys",
        sa_delete(FeedbackSurvey).where(FeedbackSurvey.customer_id == customer_id),
    )

    _run("bookings", sa_delete(Booking).where(Booking.customer_id == customer_id))

    # --- give the inventory back -------------------------------------------- #
    released = 0
    for sid in slot_ids:
        slot = db.get(Slot, sid)
        if slot is None:
            continue
        slot.status = SlotStatus.open
        slot.locked_at = None
        slot.locked_by_session = None
        released += 1
    counts["slots_released"] = released

    # TRANSCRIPTS BY CUSTOMER (T4.6) — the gap this module's own docstring named.
    # ConversationTurn now carries customer_id, so a conversation where the
    # customer was identified but no audited tool call ever ran is reachable at
    # last. Runs IN ADDITION to the session sweep above, never instead of it:
    # turns spoken before identification carry a null customer_id and are only
    # reachable by session.
    _run(
        "conversation_turns_by_customer",
        sa_delete(ConversationTurn).where(ConversationTurn.customer_id == customer_id),
    )

    _run("waitlist", sa_delete(Waitlist).where(Waitlist.customer_id == customer_id))
    _run(
        "handoff_tokens",
        sa_delete(HandoffToken).where(HandoffToken.customer_id == customer_id),
    )
    _run(
        "memory_profile",
        sa_delete(MemoryProfile).where(MemoryProfile.customer_id == customer_id),
    )
    # Device recognition (C10). Not optional tidying: DeviceLink carries an FK to
    # customers, so leaving these behind would make `db.delete(customer)` below
    # fail and take the whole erasure with it. It is also the point — being
    # recognised on the next visit is precisely what "forget me" rules out.
    _run("device_links", sa_delete(DeviceLink).where(DeviceLink.customer_id == customer_id))
    _run("voice_notes", sa_delete(VoiceNote).where(VoiceNote.customer_id == customer_id))

    if sessions:
        _run("speech_clips", sa_delete(SpeechClip).where(SpeechClip.session_id.in_(sessions)))
        _run(
            "conversation_turns",
            sa_delete(ConversationTurn).where(ConversationTurn.session_id.in_(sessions)),
        )
        # A TO Z (developer decision 2026-07-23): the audit history goes too.
        # ⚠️ THIS IS A DELIBERATE HOLE IN AN APPEND-ONLY LOG. FR-AD-02 wants the
        # trail intact and S-08 ("forget-me visible in audit") reads it on screen,
        # so both are knowingly traded for a literal erasure. The single receipt
        # written below is what keeps "we honoured it" provable at all.
        _run("audit_events", sa_delete(AuditEvent).where(AuditEvent.session.in_(sessions)))
        # TURN TRACE (T4.6). models.py has declared this in scope for erasure
        # since it was written; nothing ever deleted it. It holds the customer's
        # own message text and, from Phase 3, the routing decision for every
        # turn — so leaving it behind made "forget me" a partial delete that
        # LOOKED complete.
        _run(
            "turn_trace",
            sa_delete(TurnTrace).where(TurnTrace.session_id.in_(sessions)),
        )
    else:
        counts["speech_clips"] = counts["conversation_turns"] = counts["audit_events"] = 0
        counts["turn_trace"] = 0

    # And by customer_id, for traces recorded under a session the sweep above
    # could not reach — the same belt-and-braces the audit stragglers get.
    _run(
        "turn_trace_by_customer",
        sa_delete(TurnTrace).where(TurnTrace.customer_id == customer_id),
    )

    # Also catch audit rows attributable by payload but recorded under a session
    # the sweep above did not reach.
    stragglers = 0
    for row in list(db.scalars(select(AuditEvent))):
        payload = row.payload or {}
        if isinstance(payload, dict) and payload.get("customer_id") == customer_id:
            db.delete(row)
            stragglers += 1
    counts["audit_events"] = counts.get("audit_events", 0) + stragglers

    db.delete(customer)
    db.flush()

    # --- the agent's OWN memory, which is not in this database at all -------- #
    checkpoints = _forget_checkpoints(sessions)
    counts["checkpoint_threads"] = checkpoints

    # --- the one surviving line --------------------------------------------- #
    # NO NAME, NO MOBILE. customer_id is retained and now points at a deleted
    # row, which is what makes this line anonymous rather than a record OF them.
    # Written AFTER the audit purge above, or it would delete its own receipt.
    from app.services import audit

    audit.record(
        db,
        session=session_id,
        actor=Actor.customer,
        type=AuditType.erasure,
        payload={
            "action": "forget_me",
            "scope": "full",
            "customer_id": customer_id,
            "removed": counts,
            "reason": reason,
            "at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()

    return {
        "forgotten": True,
        "already_erased": False,
        "customer_id": customer_id,
        "removed": counts,
    }


def _forget_checkpoints(sessions: set[str]) -> int:
    """Drop the agent's LangGraph memory for these conversations.

    THE TRAP THIS CLOSES. The checkpointer is a SEPARATE SQLITE FILE
    (AGENT_CHECKPOINT_PATH), keyed on thread_id = session_id, holding the full
    tool_use/tool_result history — name, mobile, booking reference, all of it.
    Wipe every row in the business DB and skip this, and the next turn on that
    session_id rehydrates an agent that still remembers the person we just swore
    to forget. CLAUDE.md flags the same trap for reset-demo, from a bug that bit
    on stage.

    Failures are counted, never raised: an erasure that already emptied the
    database must not report itself as failed because a sqlite file was locked.
    """
    from app import config

    path = config.AGENT_CHECKPOINT_PATH
    if not sessions or not path or not os.path.exists(path):
        return 0

    done = 0
    try:
        from app.agent import build_graph, new_sqlite_checkpointer

        saver = new_sqlite_checkpointer(path)
        # A graph bound to this saver, used ONLY to explicitly overwrite the
        # identity session_facts before the whole-thread delete. Compiling it calls
        # no model (the SDK client is built lazily at generation time), so this
        # stays offline and key-free.
        graph = build_graph(saver)
        for sess in sessions:
            # EXPLICIT session_facts clear (2026-07-24). delete_thread below already
            # removes the entire checkpoint, session_facts included — but it is a
            # best-effort call that "never raises", so a session whose delete fails
            # would keep a checkpoint STILL carrying the customer's name and mobile
            # in session_facts. Overwriting those to {} FIRST guarantees the
            # PII-bearing facts are gone even on that failure path; on the happy path
            # the delete then removes this checkpoint too, so there is no residue.
            # The CURRENT live session is handled separately in the tool node (it
            # returns session_facts={}, so the end-of-turn checkpoint is already
            # clear); this loop cannot reach that in-flight state.
            try:
                graph.update_state(
                    {"configurable": {"thread_id": sess}}, {"session_facts": {}}
                )
            except Exception:  # pragma: no cover - best effort, never blocks erasure
                pass
            try:
                saver.delete_thread(sess)
                done += 1
            except Exception:  # pragma: no cover - per-thread best effort
                continue
    except Exception:  # pragma: no cover - checkpoint store unavailable
        return done
    return done
