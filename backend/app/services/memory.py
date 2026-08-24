"""Consent-gated customer memory (FR-MEM, behaviour rule 12, S-07/S-08).

THE GATE IS ON READ AS WELL AS WRITE. Every entry point here checks
memory_consent itself. Gating only the writers would leave a stale row readable
after a failed delete or a seed drift, and the gate lives in this service rather
than in the tools for the same reason booking._booking_dict's PII gate does:
every caller inherits it, and the model is never asked to be discreet.

TWO WRITERS, DISJOINT COLUMNS, SO THERE IS NO PRECEDENCE RULE.
derive_from_booking owns preferred_models / preferred_showroom_id /
preferred_daypart — observed behaviour. save owns notes — what the customer
said. Neither touches the other's columns, so there is no last-write-wins, no
timestamp comparison, and no ordering hazard. See MemoryProfile's docstring for
why the line falls there.

CANCEL DOES NOT LEARN, AND DOES NOT RETRACT. A cancelled booking is not evidence
of a preference — recording a daypart from a slot the customer rejected would
let Aisha recall a preference derived from a refusal, the opposite of "used to
shorten the current request". The columns carry no negative axis, and that
signal already has a home in Customer.winback_flag (FR-CN-05). Retraction is
impossible anyway: this is a current-state row, not an event log, so there is no
prior value to roll back to. A customer who cancels their only evening booking
still recalls "evening" — a known and accepted limitation.

RECALL HAS NO TOOL OF ITS OWN, DELIBERATELY. It rides on find_or_create_customer
(customers.find_or_create), so the model sees memory exactly once, at
identification, in a payload it already reads. There is no get_memory to go
fishing with — which is what makes rule 12's "reactive-first, never volunteered
to show off" a capability the agent lacks rather than a discipline we ask the
prompt for.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Actor,
    AuditType,
    Booking,
    Customer,
    LocationType,
    MemoryProfile,
    Showroom,
    VehicleModel,
)
from app.services import audit, slots

# Mirrors rule 5's "<=3 recommendations" cap. A memory that lists every car they
# ever looked at is not a preference, it is a history.
MAX_PREFERRED_MODELS = 3

# Long enough for a real preference in either script, short enough that `notes`
# cannot quietly become a transcript.
MAX_NOTES_CHARS = 280


# --------------------------------------------------------------------------- #
# Consent gate
# --------------------------------------------------------------------------- #
def has_memory_consent(db: Session, *, customer_id: int) -> bool:
    """True only if this customer has recorded memory consent. Never raises.

    An unknown customer is False, not an error: every caller's response to "no
    consent" and "no such customer" is identical — write nothing, recall
    nothing.
    """
    if customer_id is None:
        return False
    customer = db.get(Customer, customer_id)
    return customer is not None and bool(customer.memory_consent)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _profile_dict(db: Session, p: MemoryProfile) -> dict:
    """Serialise a profile for a tool result. IDs are stored; NAMES are joined.

    Storing "Jetour Deira" would freeze display text into a data field — the bug
    CLAUDE.md names four times. Returning it is free and correct: resolved live,
    so it cannot go stale, and it saves the agent a get_showrooms round-trip to
    say "your usual Deira". Same asymmetry as booking._booking_dict.

    A vehicle or showroom id that no longer resolves is DROPPED, never rendered
    as a shell (FR-CX-07) — the agent must not offer a car we cannot show.
    """
    models: list[dict] = []
    for vid in p.preferred_models or []:
        vehicle = db.get(VehicleModel, vid)
        if vehicle is not None:
            models.append({"vehicle_id": vehicle.id, "name": vehicle.name})

    showroom = (
        db.get(Showroom, p.preferred_showroom_id)
        if p.preferred_showroom_id is not None
        else None
    )

    return {
        "preferred_models": models,
        "preferred_showroom": (
            {"showroom_id": showroom.id, "name": showroom.name}
            if showroom is not None
            else None
        ),
        "preferred_daypart": p.preferred_daypart,
        "notes": p.notes,
    }


def _is_empty(profile: dict) -> bool:
    """True when a profile carries nothing worth recalling."""
    return not (
        profile["preferred_models"]
        or profile["preferred_showroom"]
        or profile["preferred_daypart"]
        or profile["notes"]
    )


# --------------------------------------------------------------------------- #
# Row access
# --------------------------------------------------------------------------- #
def _get(db: Session, customer_id: int) -> MemoryProfile | None:
    return db.scalar(
        select(MemoryProfile).where(MemoryProfile.customer_id == customer_id)
    )


def _upsert(db: Session, customer_id: int) -> MemoryProfile:
    """Get-or-create this customer's single profile row.

    memory_profiles.customer_id is UNIQUE and there are two writers, so
    check-then-insert races exactly as customers.find_or_create does. Same
    resolution: SAVEPOINT so a violation undoes only this insert (letting an
    IntegrityError reach the caller's transaction would poison it and take the
    booking down with it), then re-SELECT the winner's row.
    """
    existing = _get(db, customer_id)
    if existing is not None:
        return existing

    profile = MemoryProfile(customer_id=customer_id, preferred_models=[])
    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError:
        existing = _get(db, customer_id)
        if existing is None:  # pragma: no cover — violation from something else
            raise
        return existing
    return profile


# --------------------------------------------------------------------------- #
# Recall (read path)
# --------------------------------------------------------------------------- #
def recall(db: Session, *, customer_id: int) -> dict | None:
    """The consented profile, or None. Read-only, never raises.

    None — not {} — when there is no consent, no row, or an empty row, so the
    caller can simply omit the key. See customers.find_or_create: an absent
    `memory` key is unambiguous, where "memory": null is tokens the model may
    still narrate.

    DOES NOT DELETE an unconsented row it finds. A delete in a read path is a
    surprise, and the guarantee that matters is that nothing is returned —
    erasure is forget()'s job, on request.
    """
    if not has_memory_consent(db, customer_id=customer_id):
        return None
    profile = _get(db, customer_id)
    if profile is None:
        return None
    out = _profile_dict(db, profile)
    return None if _is_empty(out) else out


# --------------------------------------------------------------------------- #
# Writer 1 — derived from a completed booking (the deterministic path)
# --------------------------------------------------------------------------- #
def derive_from_booking(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    booking: Booking,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Learn the three structured columns from a booking the customer completed.

    A SIDE EFFECT, NEVER A TOOL — the same argument notifications.py makes: a
    write the model has to remember can be forgotten, double-fired, or called
    before the booking exists. booking.create/reschedule call this directly and
    the agent has no say in the matter.

    Never touches `notes`. Audits only when something actually changed — a row
    on every booking for every customer would bury the trail in noise.
    """
    now = now or datetime.now()

    if not has_memory_consent(db, customer_id=customer_id):
        return {"written": False, "reason": "no_memory_consent"}

    slot = booking.slot
    if slot is None:  # pragma: no cover — a booking always has a slot
        return {"written": False, "reason": "booking_has_no_slot"}

    profile = _upsert(db, customer_id)
    fields: list[str] = []

    # Most recent first, deduped, capped. The car they just booked is the
    # strongest signal we have, so it leads.
    if booking.vehicle_id is not None:
        previous = [v for v in (profile.preferred_models or []) if v != booking.vehicle_id]
        updated = [booking.vehicle_id, *previous][:MAX_PREFERRED_MODELS]
        if updated != list(profile.preferred_models or []):
            profile.preferred_models = updated
            fields.append("preferred_models")

    # A doorstep booking has no showroom, so there is nothing to learn — LEAVE
    # the stored one alone rather than nulling it. "They had one delivered once"
    # is not evidence they stopped preferring Deira.
    if slot.location_type == LocationType.showroom and slot.showroom_id is not None:
        if profile.preferred_showroom_id != slot.showroom_id:
            profile.preferred_showroom_id = slot.showroom_id
            fields.append("preferred_showroom_id")

    daypart = slots.daypart_of(slot.start)
    if profile.preferred_daypart != daypart:
        profile.preferred_daypart = daypart
        fields.append("preferred_daypart")

    if not fields:
        db.flush()
        return {"written": False, "reason": "unchanged"}

    audit.record(
        db,
        session=session_id,
        actor=Actor.system,
        type=AuditType.intent,
        payload={
            "intent": "memory_derived",
            "customer_id": customer_id,
            "booking_ref": booking.ref,
            "fields": fields,
            "at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {"written": True, "fields": fields}


# --------------------------------------------------------------------------- #
# Writer 2 — an explicit soft preference (the agent path)
# --------------------------------------------------------------------------- #
def save(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    notes: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Record a soft preference the booking cannot observe. Never raises.

    `notes` ONLY. The three structured columns belong to derive_from_booking,
    and letting the model's paraphrase overwrite an observed booking fact would
    be the customers._fill_if_empty bug in a new table.

    Stored in the script the customer wrote it in and never translated, for the
    same reason name_en/name_ar are split (LOC-01).
    """
    now = now or datetime.now()

    customer = db.get(Customer, customer_id) if customer_id is not None else None
    if customer is None:
        return {"error": "customer_not_found", "customer_id": customer_id}
    if not bool(customer.memory_consent):
        return {"error": "memory_consent_not_granted", "customer_id": customer_id}

    text = notes.strip() if isinstance(notes, str) else ""
    if not text:
        return {"error": "nothing_to_save", "customer_id": customer_id}
    text = text[:MAX_NOTES_CHARS]

    profile = _upsert(db, customer_id)
    if profile.notes == text:
        db.flush()
        return {"saved": False, "reason": "unchanged", "customer_id": customer_id}

    profile.notes = text
    audit.record(
        db,
        session=session_id,
        actor=Actor.agent,
        type=AuditType.intent,
        payload={
            "intent": "save_memory",
            "customer_id": customer_id,
            "fields": ["notes"],
            "at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {"saved": True, "customer_id": customer_id}


# --------------------------------------------------------------------------- #
# Erasure (rule 12, S-08)
# --------------------------------------------------------------------------- #
def forget(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    confirmed: object = False,
    withdraw_consent: bool = True,
    reason: str = "customer_request",
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Rule 12: confirm -> hard-delete -> audit -> treat as new. Never raises.

    AN UNCONFIRMED CALL DELETES NOTHING. It returns what WOULD go, so Aisha can
    say what we hold and ask once, in her own words. The two-call handshake is
    what makes the confirmation structural rather than a hope in the prompt: the
    first call *cannot* delete, however the model fills the schema. Same shape as
    lock_slot -> create_booking.

    Scope, stated honestly: this enforces that the model made the round trip, not
    that Aisha actually asked in words. Delivery is a behaviour test, the same
    limitation Slot.licence_ack documents.

    HARD DELETE, not a soft flag. Rule 12 says erased, and a tombstone we could
    still read is not erasure.

    "TREAT AS NEW THEREAFTER" NEEDS THE CONSENT FLAG DOWN TOO — otherwise the
    very next booking silently re-derives the profile we just deleted. So a
    confirmed forget also withdraws memory consent. withdraw_consent=False is the
    internal entry point used by consent.record, which is already withdrawing;
    that flag is what stops the two from recursing into each other.
    """
    now = now or datetime.now()

    customer = db.get(Customer, customer_id) if customer_id is not None else None
    if customer is None:
        # IDEMPOTENT ON A CONFIRMED CALL (2026-07-23). Erasure now deletes the
        # customer ROW, so a retry — and LLMs retry — finds nothing and used to
        # get customer_not_found back, which reads as a failure and would have
        # Aisha apologise for a deletion that in fact succeeded. "Already gone"
        # is the honest answer: they asked once and have the outcome they wanted.
        # Guard order matters here for the same reason create_booking checks
        # idempotency before its lock check.
        if confirmed is True:
            return {
                "forgotten": True,
                "already_erased": True,
                "scope": "full",
                "customer_id": customer_id,
            }
        return {"error": "customer_not_found", "customer_id": customer_id}

    profile = _get(db, customer_id)

    if not (confirmed is True):
        # Deliberately strict: only a real True confirms. A string, a 1, or a
        # truthy stray must not be enough to trigger an irreversible delete.
        summary = _profile_dict(db, profile) if profile is not None else None
        if summary is not None and _is_empty(summary):
            summary = None
        # `would_delete` (2026-07-23) is the scope the customer must hear BEFORE
        # they say yes. It exists because erasure stopped being profile-only: it
        # now takes their contact details and CANCELS THEIR BOOKINGS, and consent
        # to lose preferences is not consent to lose Saturday's test drive. The
        # old `summary` key is kept beside it so nothing reading the previous
        # shape breaks.
        from app.services import erasure as erasure_service

        return {
            "forgotten": False,
            "confirmation_required": True,
            "has_profile": summary is not None,
            "summary": summary,
            "would_delete": erasure_service.inventory(
                db, customer_id, session_id=session_id
            ),
            "customer_id": customer_id,
        }

    deleted = profile is not None

    # A TO Z (developer decision 2026-07-23). Erasure is now a FULL PURGE — the
    # customer row, every booking, the notifications, the transcripts, the audit
    # history and the agent's own checkpoint — not just this profile. It lives in
    # services/erasure.py because a cross-table wipe is a different job from the
    # consent-gated preferences row this module owns.
    #
    # The withdraw_consent branch below is now UNREACHABLE FROM THE CUSTOMER PATH
    # and deliberately kept anyway: consent.record calls in here with
    # withdraw_consent=False when a customer withdraws memory consent WITHOUT
    # asking to be forgotten, and that caller must still only lose the profile.
    # So the purge is gated on it — withdrawing consent is not the same request
    # as "delete me", and quietly upgrading one to the other would erase a
    # customer who never asked for it.
    if withdraw_consent:
        from app.services import erasure as erasure_service

        out = erasure_service.purge(
            db,
            customer_id=customer_id,
            session_id=session_id,
            now=now,
            correlation_id=correlation_id,
            reason=reason,
        )
        # erasure.purge writes its own receipt and has already removed the audit
        # history, so returning here rather than falling through is what stops
        # the trailing audit.record below from re-creating a second, redundant
        # erasure row after the one that matters.
        return {
            "forgotten": True,
            "deleted": deleted,
            "scope": "full",
            **{k: v for k, v in out.items() if k not in ("forgotten",)},
        }

    if profile is not None:
        db.delete(profile)
        db.flush()

    # Device recognition goes with the profile (C10, 2026-08-14). THIS BRANCH IS
    # THE CONSENT-WITHDRAWAL PATH — the customer did not ask to be deleted, they
    # asked us to stop remembering them — and being greeted by name on their next
    # visit is remembering them. `memory_consent` is the basis for holding the
    # link, so it goes when the basis does. Deferred import for the same reason as
    # consent below: recognition imports models only, but keeping every
    # cross-service import in this function local keeps the pattern one thing.
    from app.services import recognition as recognition_service

    devices_unlinked = recognition_service.unlink_customer(db, customer_id=customer_id)

    # Voice notes go too (2026-07-21). A recording is personal data and arguably
    # biometric-adjacent, so an erasure that kept the audio would be a tombstone
    # of exactly the kind the hard-delete rule above exists to refuse — and S-08
    # walks this trail on screen. Deferred import for the same reason as consent
    # below: services/voice imports audit, and a top-level import here would tie
    # the memory service to it in both directions.
    #
    # Its own audit row (AuditType.erasure, action=forget_me_voice) rather than a
    # field on the row below: the count belongs to the deletion that happened,
    # not to the profile summary.
    from app.services import voice as voice_service

    voice_deleted = voice_service.delete_for_customer(
        db,
        customer_id=customer_id,
        session_id=session_id,
        correlation_id=correlation_id,
    )

    # Read-aloud clips go too (2026-07-21). These are audio of the customer's own
    # name, mobile and booking reference being READ OUT — the most quotable form
    # the data takes — so an erasure that kept them would be the emptiest kind of
    # compliance. Keyed by session, which is how the clips themselves are keyed.
    from app.services import speech as speech_service

    speech_deleted = speech_service.delete_for_session(
        db, session_id=session_id, correlation_id=correlation_id
    )

    if withdraw_consent:
        # Deferred import, not a module-level one: consent.record calls back into
        # this function on a withdrawal, and a top-level import would be circular.
        # The withdraw_consent flag is what keeps that from recursing — consent
        # passes False because it is already doing the withdrawing itself.
        from app.services import consent as consent_service

        consent_service.record(
            db,
            session_id=session_id,
            customer_id=customer_id,
            memory=False,
            now=now,
            correlation_id=correlation_id,
            _erase_profile=False,
        )

    # Audited even when there was no profile: "we honoured the request" is the
    # auditable fact (S-08), and pinning it on prior state would make the trail
    # depend on whether we happened to hold anything.
    audit.record(
        db,
        session=session_id,
        actor=Actor.customer,
        type=AuditType.erasure,
        payload={
            "action": "forget_me",
            "customer_id": customer_id,
            "deleted": deleted,
            "voice_notes_deleted": voice_deleted,
            "speech_clips_deleted": speech_deleted,
            "devices_unlinked": devices_unlinked,
            "reason": reason,
            "at": now.isoformat(),
        },
        correlation_id=correlation_id,
    )
    db.flush()
    return {
        "forgotten": True,
        "deleted": deleted,
        "voice_notes_deleted": voice_deleted,
        "speech_clips_deleted": speech_deleted,
        "devices_unlinked": devices_unlinked,
        "customer_id": customer_id,
    }
