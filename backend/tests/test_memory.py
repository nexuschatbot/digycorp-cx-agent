"""Customer-memory tests (FR-MEM, behaviour rule 12, S-07/S-08).

The four properties this module exists to guarantee:

1. NOTHING IS REMEMBERED WITHOUT CONSENT, on read as well as on write. A gate
   that only covers writes leaves a stale row readable.
2. THE TWO WRITERS NEVER COLLIDE. derive_from_booking owns the structured
   columns, save owns notes, and neither touches the other's — so there is no
   precedence rule to get wrong.
3. ERASURE IS REAL. "Forget me" hard-deletes, is audited whether or not we held
   anything, and leaves the customer genuinely new — not re-derivable on their
   next booking.
4. IDS ARE STORED, NEVER DISPLAY TEXT. A stored showroom name could not be
   spoken in an Arabic turn and would go stale on a rename.
"""

from __future__ import annotations


import pytest
from sqlalchemy import select

from app.models import (
    AuditEvent,
    AuditType,
    Customer,
    LocationType,
    MemoryProfile,
    Showroom,
    Slot,
    SlotStatus,
)
from app.services import booking, consent, customers, memory
from app.services import slots as slots_service

SESSION = "sess-memory-1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _customer(db, mobile="+971501234567", *, remember=False) -> int:
    cid = customers.find_or_create(db, mobile=mobile)["customer"]["customer_id"]
    if remember:
        consent.record(db, session_id=SESSION, customer_id=cid, memory=True)
    return cid


def _book(db, slot, cid, *, session=SESSION, **kw):
    """Lock, acknowledge the licence, book. All three are the real write guards.

    Asserts the booking actually happened: every test here reads the profile
    AFTER a booking, so a create() that quietly returned an error would surface
    as a baffling "profile is None" instead of the guard it actually tripped.
    """
    slots_service.lock(db, session_id=session, slot_id=slot.id)
    slots_service.acknowledge_licence_reminder(db, session_id=session)
    out = booking.create(
        db, session_id=session, customer_id=cid, vehicle_id=slot.vehicle_id,
        slot_id=slot.id, **kw
    )
    assert "error" not in out, f"booking setup failed: {out}"
    return out


def _slots_in(db, *, location=LocationType.showroom, n=4) -> list[Slot]:
    return list(
        db.scalars(
            select(Slot)
            .where(Slot.status == SlotStatus.open, Slot.location_type == location)
            .order_by(Slot.start, Slot.id)
        ).all()
    )[:n]


def _profile(db, cid) -> MemoryProfile | None:
    return db.scalar(select(MemoryProfile).where(MemoryProfile.customer_id == cid))


def _erasure_events(db) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.erasure)).all()
    )


# --------------------------------------------------------------------------- #
# 1. The consent gate — rule 12 "recall only with recorded consent"
# --------------------------------------------------------------------------- #
def test_booking_writes_no_profile_without_consent(db, open_slot):
    """The default is forgetting. A booking alone is not permission."""
    cid = _customer(db)
    _book(db, open_slot, cid)
    assert _profile(db, cid) is None, "a booking must not create a profile on its own"


def test_save_is_refused_without_consent(db):
    cid = _customer(db)
    out = memory.save(db, session_id=SESSION, customer_id=cid, notes="prefers evenings")
    assert out["error"] == "memory_consent_not_granted"
    assert _profile(db, cid) is None


def test_recall_returns_nothing_without_consent_even_when_a_row_exists(db):
    """The gate is on READ too — a stale row must not leak.

    And the read path must NOT self-heal by deleting: the guarantee is that
    nothing is returned, not that reads have side effects.
    """
    cid = _customer(db)
    db.add(MemoryProfile(customer_id=cid, preferred_models=[1], preferred_daypart="evening"))
    db.flush()

    assert memory.recall(db, customer_id=cid) is None
    assert _profile(db, cid) is not None, "a read must never delete"


def test_consent_granted_mid_conversation_then_the_booking_derives(db, open_slot):
    """S-07's setup: they say yes, and from then on we learn."""
    cid = _customer(db)
    consent.record(db, session_id=SESSION, customer_id=cid, memory=True)
    _book(db, open_slot, cid)
    assert _profile(db, cid) is not None


# --------------------------------------------------------------------------- #
# 2. Writer one — derived from a completed booking
# --------------------------------------------------------------------------- #
def test_booking_derives_all_three_structured_columns(db, open_slot):
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    p = _profile(db, cid)
    assert p.preferred_models == [open_slot.vehicle_id]
    assert p.preferred_showroom_id == open_slot.showroom_id
    assert p.preferred_daypart == slots_service.daypart_of(open_slot.start)


def test_derived_columns_hold_ids_and_never_display_text(db, open_slot):
    """Property 4, asserted on the ORM row rather than the payload."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    p = _profile(db, cid)
    names = {s.name for s in db.scalars(select(Showroom)).all()}
    assert isinstance(p.preferred_showroom_id, int)
    assert all(isinstance(v, int) for v in p.preferred_models)
    assert p.preferred_daypart in {"morning", "afternoon", "evening"}
    # Nothing anywhere on the row is a showroom's display name.
    assert not (names & {p.preferred_daypart, p.notes})


def test_preferred_models_is_most_recent_first_deduped_and_capped(db, slots):
    """Rule 5's cap. A list of every car they ever saw is a history, not a taste."""
    cid = _customer(db, remember=True)
    picked = []
    for slot in _slots_in(db, n=12):
        if slot.vehicle_id in picked:
            continue
        _book(db, slot, cid)
        picked.append(slot.vehicle_id)
        if len(picked) == 4:
            break

    p = _profile(db, cid)
    assert len(p.preferred_models) == memory.MAX_PREFERRED_MODELS
    assert p.preferred_models == list(reversed(picked))[: memory.MAX_PREFERRED_MODELS]
    assert len(set(p.preferred_models)) == len(p.preferred_models), "no duplicates"


def test_a_doorstep_booking_leaves_the_remembered_showroom_alone(db, slots, doorstep_slot):
    """"They had one delivered once" is not evidence they stopped liking Deira."""
    cid = _customer(db, remember=True)
    showroom_slot = _slots_in(db, n=1)[0]
    _book(db, showroom_slot, cid)
    remembered = _profile(db, cid).preferred_showroom_id
    assert remembered is not None

    _book(db, doorstep_slot, cid, doorstep_address="Villa 12, Jumeirah 1, Dubai")

    assert _profile(db, cid).preferred_showroom_id == remembered


def test_reschedule_relearns_the_daypart(db, slots):
    """A reschedule is a CORRECTED preference — the strongest signal we get."""
    cid = _customer(db, remember=True)

    # Same vehicle both times: reschedule guards on vehicle_slot_mismatch, and
    # changing the car would confound which signal moved the daypart.
    pair: dict[str, Slot] = {}
    for s in _slots_in(db, n=400):
        if s.vehicle_id != _slots_in(db, n=1)[0].vehicle_id:
            continue
        pair.setdefault(slots_service.daypart_of(s.start), s)
    first, second = pair["morning"], pair["evening"]

    out = _book(db, first, cid)
    assert _profile(db, cid).preferred_daypart == "morning"

    slots_service.lock(db, session_id=SESSION, slot_id=second.id)
    moved = booking.reschedule(
        db, session_id=SESSION, session_customer_id=cid, new_slot_id=second.id,
        ref=out["booking"]["booking_ref"],
    )
    assert "error" not in moved, f"reschedule setup failed: {moved}"

    assert _profile(db, cid).preferred_daypart == "evening"


def test_cancel_does_not_learn_and_does_not_retract(db, slots):
    """A cancelled booking is not evidence of a preference (and cannot be undone)."""
    cid = _customer(db, remember=True)
    slot = _slots_in(db, n=1)[0]
    out = _book(db, slot, cid)

    p = _profile(db, cid)
    before = (list(p.preferred_models), p.preferred_showroom_id, p.preferred_daypart, p.notes)

    booking.cancel(db, session_id=SESSION, session_customer_id=cid, ref=out["booking"]["booking_ref"])

    p = _profile(db, cid)
    after = (list(p.preferred_models), p.preferred_showroom_id, p.preferred_daypart, p.notes)
    assert after == before, "cancel must neither learn nor retract"


def test_derive_audits_only_when_something_changed(db, slots):
    """A row per booking per customer would bury the trail in noise."""
    cid = _customer(db, remember=True)
    slot_a, slot_b = _slots_in(db, n=2)

    _book(db, slot_a, cid)
    first = _memory_derived_events(db)
    assert len(first) == 1

    # Same car, same showroom, same daypart -> nothing learned, nothing logged.
    if (
        slot_b.vehicle_id == slot_a.vehicle_id
        and slot_b.showroom_id == slot_a.showroom_id
        and slots_service.daypart_of(slot_b.start) == slots_service.daypart_of(slot_a.start)
    ):
        _book(db, slot_b, cid)
        assert len(_memory_derived_events(db)) == 1, "an unchanged profile must not audit"


def _memory_derived_events(db) -> list[AuditEvent]:
    return [
        e
        for e in db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.intent)).all()
        if e.payload.get("intent") == "memory_derived"
    ]


# --------------------------------------------------------------------------- #
# 3. Writer two — an explicit soft preference
# --------------------------------------------------------------------------- #
def test_save_writes_only_notes(db):
    cid = _customer(db, remember=True)
    memory.save(db, session_id=SESSION, customer_id=cid, notes="prefers a female consultant")

    p = _profile(db, cid)
    assert p.notes == "prefers a female consultant"
    assert p.preferred_models == []
    assert p.preferred_showroom_id is None
    assert p.preferred_daypart is None


def test_save_trims_and_caps(db):
    cid = _customer(db, remember=True)
    memory.save(db, session_id=SESSION, customer_id=cid, notes="  " + "x" * 500 + "  ")
    assert len(_profile(db, cid).notes) == memory.MAX_NOTES_CHARS


def test_save_preserves_arabic_untranslated(db):
    """LOC-01: stored in the script they wrote it in, never machine-translated."""
    cid = _customer(db, remember=True)
    arabic = "يفضل استشارية سيدة"
    memory.save(db, session_id=SESSION, customer_id=cid, notes=arabic)
    assert _profile(db, cid).notes == arabic


@pytest.mark.parametrize("value", [None, "", "   ", 42, True])
def test_save_rejects_anything_that_is_not_a_note(db, value):
    cid = _customer(db, remember=True)
    out = memory.save(db, session_id=SESSION, customer_id=cid, notes=value)
    assert out["error"] == "nothing_to_save"


def test_the_two_writers_share_one_row_and_never_clobber(db, slots):
    """Property 2, end to end: derive -> save -> derive."""
    cid = _customer(db, remember=True)
    slot_a, slot_b = _slots_in(db, n=2)

    _book(db, slot_a, cid)
    memory.save(db, session_id=SESSION, customer_id=cid, notes="brings the kids")
    _book(db, slot_b, cid)

    rows = db.scalars(select(MemoryProfile).where(MemoryProfile.customer_id == cid)).all()
    assert len(rows) == 1, "one row per customer"
    p = rows[0]
    assert p.notes == "brings the kids", "derive must never touch notes"
    assert p.preferred_models and p.preferred_daypart, "save must never blank the derived columns"


# --------------------------------------------------------------------------- #
# 4. The recall payload
# --------------------------------------------------------------------------- #
def test_recall_returns_names_alongside_the_ids_it_stores(db, open_slot):
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    out = memory.recall(db, customer_id=cid)
    showroom = db.get(Showroom, open_slot.showroom_id)
    assert out["preferred_showroom"] == {"showroom_id": showroom.id, "name": showroom.name}
    assert out["preferred_models"][0]["vehicle_id"] == open_slot.vehicle_id
    assert out["preferred_models"][0]["name"]


def test_recall_is_none_for_a_consented_but_empty_profile(db):
    """An empty row is nothing to recall, and must not present as something."""
    cid = _customer(db, remember=True)
    memory._upsert(db, cid)
    db.flush()
    assert memory.recall(db, customer_id=cid) is None


def test_recall_drops_an_id_that_no_longer_resolves(db):
    """FR-CX-07: never render a shell for something we cannot show."""
    cid = _customer(db, remember=True)
    db.add(MemoryProfile(customer_id=cid, preferred_models=[99999], preferred_daypart="evening"))
    db.flush()

    out = memory.recall(db, customer_id=cid)
    assert out["preferred_models"] == []
    assert out["preferred_daypart"] == "evening"


def test_memory_arrives_as_a_sibling_key_not_inside_customer(db, open_slot):
    """S-07's contract. The two gates must not hide behind one key."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    out = customers.find_or_create(db, mobile="+971501234567")
    assert set(out) == {"customer", "created", "memory"}
    assert "memory" not in out["customer"], "memory has a consent gate the customer dict does not"


def test_no_memory_key_at_all_when_there_is_nothing_to_recall(db):
    out = customers.find_or_create(db, mobile="+971509998888")
    assert "memory" not in out, "an absent key, never a null the model might narrate"


# --------------------------------------------------------------------------- #
# 5. Erasure — rule 12, S-08
# --------------------------------------------------------------------------- #
def test_an_unconfirmed_forget_deletes_nothing(db, open_slot):
    """The first call CANNOT delete — that is what makes the confirmation real."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    out = memory.forget(db, session_id=SESSION, customer_id=cid)

    assert out["forgotten"] is False
    assert out["confirmation_required"] is True
    assert out["has_profile"] is True
    assert out["summary"]["preferred_daypart"]
    assert _profile(db, cid) is not None
    assert _erasure_events(db) == [], "nothing happened, so nothing may be logged"


@pytest.mark.parametrize("value", ["true", 1, "yes", None, False])
def test_only_a_real_true_confirms_an_irreversible_delete(db, open_slot, value):
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=value)

    assert _profile(db, cid) is not None, f"{value!r} must not be enough to erase"


def test_a_confirmed_forget_hard_deletes(db, open_slot):
    """Rule 12 says erased. A tombstone we can still read is not erasure."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)
    profile_id = _profile(db, cid).id

    out = memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)

    # A TO Z (2026-07-23). Erasure stopped being profile-only: it now takes the
    # customer ROW, their bookings, the notifications, the transcripts and the
    # audit history. The envelope grew a `removed` breakdown to match, so this
    # asserts the SHAPE and the outcome rather than an exact dict — a literal
    # comparison here would have to be rewritten every time a table is added,
    # which is how a test stops describing intent and starts describing code.
    assert out["forgotten"] is True
    assert out["scope"] == "full"
    assert out["customer_id"] == cid
    assert db.get(MemoryProfile, profile_id) is None
    # THE POINT OF THE RENAME: the person is gone, not just their preferences.
    assert db.get(Customer, cid) is None, "a full erasure must take the customer row"
    assert out["removed"]["bookings"] == 1
    assert out["removed"]["slots_released"] == 1, (
        "a deleted booking must hand its slot back — Booking points at Slot and "
        "not the reverse, so a slot left `booked` here can never be recovered"
    )


def test_forget_leaves_exactly_one_pii_free_receipt(db, open_slot):
    """S-08 SURVIVES ONLY AS THIS ONE ROW, and that is a deliberate trade.

    A to Z (2026-07-23) deletes the customer's audit HISTORY too, which is a
    knowing hole in an append-only log (FR-AD-02). This single receipt is what
    keeps "we honoured the request" provable at all — so it must exist, and it
    must carry no name and no mobile. The customer_id it keeps now points at a
    deleted row, i.e. an orphan integer rather than a record of a person.
    """
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    memory.forget(
        db, session_id=SESSION, customer_id=cid, confirmed=True, correlation_id="corr-1",
    )

    events = _erasure_events(db)
    assert len(events) == 1, "the receipt must not be swept away with the history"
    e = events[0]
    assert e.session == SESSION
    assert e.actor.value == "customer", "a person exercised a right; the agent did not decide"
    assert e.payload["scope"] == "full"
    assert e.payload["correlation_id"] == "corr-1"
    assert e.payload["removed"]["bookings"] == 1

    import json

    blob = json.dumps(e.payload).lower()
    for pii in ("ravi", "sharma", "+9715", "name_en", "mobile"):
        assert pii not in blob, f"the surviving receipt leaked {pii!r}"


def test_forget_is_audited_even_when_we_held_nothing(db):
    """Honouring the request is the auditable fact, whatever we happened to hold."""
    cid = _customer(db, remember=True)

    out = memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)

    assert out["forgotten"] is True
    assert out["removed"]["memory_profile"] == 0, "there was no profile to take"
    events = _erasure_events(db)
    assert len(events) == 1, "the request was honoured, so it is on the record"


def test_forget_takes_the_customer_row_not_just_the_consent_flag(db, open_slot):
    """Was test_forget_puts_the_consent_flag_down. That test read
    customer.memory_consent AFTER the erasure — which only made sense while the
    customer row survived it. Putting the flag down existed to stop the next
    booking silently re-deriving the profile; deleting the row is the stronger
    form of the same guarantee, so the assertion moves rather than disappears.
    """
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)

    assert db.get(Customer, cid) is None
    assert _profile(db, cid) is None


def test_after_a_forget_there_is_nobody_left_to_relearn_about(db, slots):
    """"Treat as new thereafter", now structural.

    Was test_after_a_forget_the_next_booking_does_not_relearn, which booked a
    SECOND slot for the same customer and checked no profile came back. That
    setup can no longer run at all — the customer row is gone, so booking.create
    refuses with customer_not_found — and that refusal IS the guarantee, in a
    stronger form than the original test could express.
    """
    cid = _customer(db, remember=True)
    slot_a, slot_b = _slots_in(db, n=2)
    _book(db, slot_a, cid)

    memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)

    assert db.get(Customer, cid) is None
    assert _profile(db, cid) is None, "a forgotten customer must stay forgotten"
    assert memory.recall(db, customer_id=cid) is None
    from app.services import booking as booking_service

    assert (
        booking_service.create(
            db, session_id=SESSION, customer_id=cid, vehicle_id=1, slot_id=slot_b.id
        ).get("error")
        is not None
    ), "a booking for an erased customer must be refused, not silently re-created"


# --------------------------------------------------------------------------- #
# 6. Consent withdrawal is its own erasure path (PDPL)
# --------------------------------------------------------------------------- #
def test_withdrawing_memory_consent_deletes_the_profile(db, open_slot):
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)
    assert _profile(db, cid) is not None

    consent.record(db, session_id=SESSION, customer_id=cid, memory=False)

    assert _profile(db, cid) is None, "withdrawal removes the basis for holding it"


def test_withdrawal_writes_both_a_consent_and_an_erasure_row(db, open_slot):
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    consent.record(
        db, session_id=SESSION, customer_id=cid, memory=False, correlation_id="corr-w",
    )

    erasures = _erasure_events(db)
    assert len(erasures) == 1
    assert erasures[0].payload["reason"] == "consent_withdrawn"
    assert erasures[0].payload["correlation_id"] == "corr-w"
    consents = db.scalars(
        select(AuditEvent).where(AuditEvent.type == AuditType.consent)
    ).all()
    assert any(e.payload.get("withdrawal") for e in consents)


def test_re_recording_an_existing_false_is_not_a_fresh_erasure(db):
    """Otherwise the trail would lie about how often they asked."""
    cid = _customer(db)  # memory consent already False
    consent.record(db, session_id=SESSION, customer_id=cid, memory=False)
    assert _erasure_events(db) == []


def test_withdrawing_messaging_leaves_memory_alone(db, open_slot):
    """The axis-independence guarantee consent.py's docstring promises."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    consent.record(db, session_id=SESSION, customer_id=cid, messaging=False)

    assert _profile(db, cid) is not None
    assert db.get(Customer, cid).memory_consent is True


# --------------------------------------------------------------------------- #
# 7. Plumbing
# --------------------------------------------------------------------------- #
def test_upsert_is_idempotent_and_returns_one_row(db):
    cid = _customer(db, remember=True)
    a = memory._upsert(db, cid)
    b = memory._upsert(db, cid)
    db.flush()
    assert a.id == b.id
    assert len(db.scalars(select(MemoryProfile)).all()) == 1


def test_the_service_never_commits(db, open_slot, monkeypatch):
    """The caller owns the transaction (tool_node commits once per turn)."""
    cid = _customer(db, remember=True)
    _book(db, open_slot, cid)

    def boom():
        raise AssertionError("services must flush, never commit")

    monkeypatch.setattr(db, "commit", boom)

    memory.save(db, session_id=SESSION, customer_id=cid, notes="fine")
    memory.recall(db, customer_id=cid)
    memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)


def test_the_seed_fixture_never_profiles_an_unconsented_customer():
    """A seeded profile without consent is the state the gate exists to prevent —
    shipping one would be seeding a bug the code then has to defend against."""
    import json
    from pathlib import Path

    seed_dir = Path(__file__).resolve().parent.parent / "seed"
    consented = {
        c["id"]
        for c in json.loads((seed_dir / "customers.json").read_text(encoding="utf-8"))
        if c.get("memory_consent")
    }
    profiled = {
        p["customer_id"]
        for p in json.loads((seed_dir / "memory_profiles.json").read_text(encoding="utf-8"))
    }
    assert profiled <= consented, f"profiled without consent: {sorted(profiled - consented)}"


def test_an_unknown_customer_is_data_never_an_exception(db):
    assert memory.recall(db, customer_id=99999) is None
    assert memory.save(db, session_id=SESSION, customer_id=99999, notes="x")["error"]
    # A CONFIRMED forget on an id we do not hold no longer errors (2026-07-23).
    # Since erasure deletes the customer row, "no such customer" and "already
    # erased" are the same observation, and we cannot tell them apart — so a
    # retry must read as done rather than as a failure Aisha would apologise
    # for. The UNCONFIRMED call still errors: there, the id is being used to
    # LOOK SOMETHING UP, and an unknown one is a real dead end.
    assert (
        memory.forget(db, session_id=SESSION, customer_id=99999, confirmed=True)["forgotten"]
        is True
    )
    assert memory.forget(db, session_id=SESSION, customer_id=99999)["error"]
    assert memory.has_memory_consent(db, customer_id=None) is False
