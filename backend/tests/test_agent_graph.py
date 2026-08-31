"""Stage 1 proof: the LangGraph agent loop, end to end, WITHOUT streaming or the API.

These tests drive the WHOLE compiled graph — agent node, tools node, conditional
edge, checkpointer — with a scripted `generate` injected via config, so they are
deterministic and need no ANTHROPIC_API_KEY. The scripted model replays the exact
tool sequence a Jetour-T2 booking produces; everything downstream (the tool node,
trusted-context injection, the customer_id binding, correlation-id threading, and
the real service-layer write guards) is the production code path.

What this proves before any SSE exists (AD-H):
- a real TD-YYYY-NNNN comes out of a full booking through the real write guards;
- customer_id is INJECTED, never modelled — it is absent from both write schemas,
  yet the booking (which needs it) succeeds because the tool node resolves it from
  the session binding minted by find_or_create_customer;
- one write = two audit rows (choke-point tool_call + service intent/consent)
  sharing ONE correlation_id;
- a write attempted before identification is DATA (customer_not_identified), never
  a crash.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date

from sqlalchemy import select

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.agent.prompt import system_prompt
from app.models import AuditEvent, AuditType, Booking, BookingStatus, LocationType, Slot, SlotStatus
from app.services import slots as slots_service
from app.tools.booking_tools import CREATE_BOOKING_SCHEMA
from app.tools.customer_tools import RECORD_CONSENT_SCHEMA

TD_REF = re.compile(r"^TD-\d{4}-[A-Z0-9]{4}$")
MOBILE = "+971509998877"  # fresh number — not Ravi (the seeded returning customer)


# --------------------------------------------------------------------------- #
# A scripted "model": replays canned assistant turns, ignoring the prompt.
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """Returns the next canned assistant message on each agent generation.

    It stands in for client.messages.stream via the injectable `generate` hook —
    same message shape the real node produces, so the graph runs identically.
    """

    def __init__(self, steps: list[dict], *, cancel_before: int | None = None, cancel_event=None) -> None:
        self.steps = steps
        self.i = 0
        # For the abort test: trip `cancel_event` just as step `cancel_before` is
        # produced — i.e. the client drops exactly as the agent decides to act.
        self.cancel_before = cancel_before
        self.cancel_event = cancel_event

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        if self.cancel_event is not None and self.i == self.cancel_before:
            self.cancel_event.set()
        msg = self.steps[self.i]
        self.i += 1
        return msg


def _tool_use(tid: str, tool: str, /, **inp) -> dict:
    # tid/tool are positional-only so a tool input field named `name` (e.g.
    # search_vehicles, find_or_create_customer) lands in **inp, not on the helper.
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}


def _text(s: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": s}]}


def _run(db, session_id: str, steps: list[dict], *, model: "ScriptedModel | None" = None, cancel_event=None) -> dict:
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=model or ScriptedModel(steps), cancel_event=cancel_event)
    return graph.invoke(
        {
            "messages": [
                {"role": "user", "content": "Details on the Jetour T2, and I'd like a test drive."}
            ],
            "iterations": 0,
            "customer_id": None,
        },
        config,
    )


def _events(db, session_id: str) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.session == session_id).order_by(AuditEvent.id))
    )


def _tool_result_for(final: dict, tool_name: str, steps: list[dict]) -> dict:
    """Pull the JSON tool_result that answered the given tool call."""
    tid = next(
        b["id"]
        for s in steps
        for b in s["content"]
        if b.get("type") == "tool_use" and b["name"] == tool_name
    )
    for msg in final["messages"]:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tid:
                return json.loads(b["content"])
    raise AssertionError(f"no tool_result for {tool_name}")


# --------------------------------------------------------------------------- #
# The security regression the whole group exists to lock in
# --------------------------------------------------------------------------- #
def test_customer_id_absent_from_write_schemas():
    """customer_id must be INJECTED, never declared. If it silently reappears in
    either schema, a model could book / consent under another customer's id."""
    for schema in (CREATE_BOOKING_SCHEMA, RECORD_CONSENT_SCHEMA):
        props = schema["input_schema"]["properties"]
        required = schema["input_schema"].get("required", [])
        assert "customer_id" not in props, f"{schema['name']} re-declared customer_id"
        assert "customer_id" not in required, f"{schema['name']} requires customer_id"


# --------------------------------------------------------------------------- #
# The full booking, through the graph
# --------------------------------------------------------------------------- #
def test_scripted_booking_end_to_end(slots):
    db = slots
    session_id = "sess-book-t2"

    t2 = 2  # Jetour T2 (seed)
    # earliest open showroom slot for the T2
    from app.models import LocationType, Slot

    slot = db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == t2,
        )
        .order_by(Slot.start, Slot.id)
    ).first()
    assert slot is not None, "fixture must generate an open T2 showroom slot"

    steps = [
        _tool_use("u1", "search_vehicles", name="Jetour T2"),
        _tool_use("u2", "get_available_slots", vehicle_id=t2, location_type="showroom"),
        _tool_use("u3", "lock_slot", slot_id=slot.id),
        _tool_use("u4", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        # Rule 9 precondition 3: create_booking is refused without it, so the
        # scripted sequence carries it exactly as a real booking turn would.
        _tool_use("u5", "acknowledge_licence_reminder"),
        _tool_use("u6", "record_consent", messaging=True),  # NO customer_id — injected
        _tool_use("u7", "create_booking", vehicle_id=t2, slot_id=slot.id),  # NO customer_id — injected
        _text("You're all set — your Jetour T2 test drive is booked."),
    ]

    final = _run(db, session_id, steps)

    # --- a real, guarded booking exists -------------------------------------- #
    result = _tool_result_for(final, "create_booking", steps)
    assert result.get("created") is True, result
    ref = result["booking"]["booking_ref"]
    assert TD_REF.match(ref), ref

    booking = db.scalar(select(Booking).where(Booking.ref == ref))
    assert booking is not None and booking.status == BookingStatus.confirmed
    assert booking.customer_id is not None  # injected id actually landed on the row
    db.refresh(slot)
    assert slot.status == SlotStatus.booked  # write guard consumed the slot

    # --- customer_id was injected, not modelled ------------------------------ #
    # The model never sent customer_id (absent from the schema), yet the booking
    # carries one — resolved from the session binding find_or_create minted.
    fc = _tool_result_for(final, "find_or_create_customer", steps)
    assert booking.customer_id == fc["customer"]["customer_id"]

    # --- audit: every tool call logged, one correlation id per invocation ----- #
    events = _events(db, session_id)
    tool_calls = [e for e in events if e.type == AuditType.tool_call]
    called = [e.payload["tool"] for e in tool_calls]
    assert called == [
        "search_vehicles",
        "get_available_slots",
        "lock_slot",
        "find_or_create_customer",
        "acknowledge_licence_reminder",
        "record_consent",
        "create_booking",
    ], called
    # every tool_call carries a correlation id, and they are all distinct
    cids = [e.payload.get("correlation_id") for e in tool_calls]
    assert all(cids) and len(set(cids)) == len(cids), cids

    # --- correlation: create_booking tool_call <-> intent share ONE id -------- #
    booking_call = next(e for e in tool_calls if e.payload["tool"] == "create_booking")
    intent = next(
        e
        for e in events
        if e.type == AuditType.intent and e.payload.get("intent") == "create_booking"
    )
    assert booking_call.payload["correlation_id"] == intent.payload["correlation_id"]
    assert intent.payload["ref"] == ref

    # notifications (a facet of the same create_booking action) share it too
    notes = [e for e in events if e.type == AuditType.notification]
    assert notes and all(
        e.payload.get("correlation_id") == intent.payload["correlation_id"] for e in notes
    )

    # --- correlation: record_consent tool_call <-> consent share ONE id ------- #
    consent_call = next(e for e in tool_calls if e.payload["tool"] == "record_consent")
    consent_evt = next(e for e in events if e.type == AuditType.consent)
    assert consent_call.payload["correlation_id"] == consent_evt.payload["correlation_id"]


# --------------------------------------------------------------------------- #
# A write before identification is DATA, never a crash
# --------------------------------------------------------------------------- #
def test_write_before_identification_is_data(slots):
    db = slots
    session_id = "sess-premature"

    from app.models import LocationType, Slot

    slot = db.scalars(
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == 2,
        )
        .order_by(Slot.start, Slot.id)
    ).first()

    steps = [
        _tool_use("u1", "create_booking", vehicle_id=2, slot_id=slot.id),  # no customer identified
        _text("Let me get a few details first."),
    ]
    final = _run(db, session_id, steps)

    result = _tool_result_for(final, "create_booking", steps)
    assert result.get("error") == "customer_not_identified", result
    # nothing was written
    assert db.scalar(select(Booking).where(Booking.slot_id == slot.id)) is None
    db.refresh(slot)
    assert slot.status == SlotStatus.open
    # but the attempt WAS audited at the choke point
    events = _events(db, session_id)
    assert any(
        e.type == AuditType.tool_call and e.payload["tool"] == "create_booking" for e in events
    )


# --------------------------------------------------------------------------- #
# Carry-forward 2: prompt "today" and inventory dates come from ONE clock
# --------------------------------------------------------------------------- #
def test_prompt_date_and_slot_generation_share_one_clock(db):
    """If the prompt's "today" and the generated inventory's dates ever diverge,
    the agent offers slots that don't exist. Both read date.today(): the prompt
    anchor and slots.generate's default window start. Tie them with one assertion."""
    result = slots_service.generate(db, days=3, prebooked=0.0)  # start_day=None -> date.today()
    today = date.today().isoformat()
    assert result["window_start"] == today  # inventory starts today
    assert today in system_prompt()  # prompt anchors the SAME today


# --------------------------------------------------------------------------- #
# Tightening 1: an abort between "decided to book" and "booking written" leaves
# NO booking row (the tool node stops BEFORE dispatching the write)
# --------------------------------------------------------------------------- #
def test_abort_before_booking_writes_nothing(slots):
    db = slots
    session_id = "sess-abort-book"
    slot = db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == 2,
        ).order_by(Slot.start, Slot.id)
    ).first()

    steps = [
        _tool_use("u1", "search_vehicles", name="Jetour T2"),
        _tool_use("u2", "get_available_slots", vehicle_id=2, location_type="showroom"),
        _tool_use("u3", "lock_slot", slot_id=slot.id),
        _tool_use("u4", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _tool_use("u5", "record_consent", messaging=True),
        _tool_use("u6", "create_booking", vehicle_id=2, slot_id=slot.id),  # index 5
        _text("You're all set."),
    ]
    cancel = threading.Event()
    # Trip the abort exactly as the agent produces the create_booking call (i==5).
    model = ScriptedModel(steps, cancel_before=5, cancel_event=cancel)
    _run(db, session_id, steps, model=model, cancel_event=cancel)

    # THE guarantee: no booking was written for the departed customer.
    assert db.scalar(select(Booking)) is None
    db.refresh(slot)
    assert slot.status != SlotStatus.booked  # the lock happened earlier; the BOOKING did not

    # create_booking was never even audited as a tool_call — we stopped before it.
    events = _events(db, session_id)
    tool_calls = [e.payload["tool"] for e in events if e.type == AuditType.tool_call]
    assert "create_booking" not in tool_calls
    assert not any(e.type == AuditType.intent for e in events)  # no booking intent row
    # the earlier tools DID run and audit — the abort was surgical, not a rollback
    assert "record_consent" in tool_calls and "lock_slot" in tool_calls


# --------------------------------------------------------------------------- #
# Option B: a tapped chip PINS lock_slot's slot_id (executor-injected), the
# model's prose parse can never pick a different slot
# --------------------------------------------------------------------------- #
def _earliest_open_showroom_slots(db, vehicle_id=2, n=2):
    return db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == vehicle_id,
        ).order_by(Slot.start, Slot.id)
    ).all()[:n]


def test_chip_tap_pins_slot_id_over_model_parse(slots):
    db = slots
    session_id = "sess-pin"
    slot_a, slot_b = _earliest_open_showroom_slots(db, n=2)  # A = tapped chip, B = a decoy

    # The model mis-parses the prose and emits lock_slot for the WRONG slot (B).
    steps = [_tool_use("u1", "lock_slot", slot_id=slot_b.id), _text("Locked.")]
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=ScriptedModel(steps), provided_slot_id=slot_a.id)
    graph.invoke(
        {"messages": [{"role": "user", "content": "I'll take Monday at 11:00 AM at Al Quoz."}],
         "iterations": 0, "customer_id": None},
        config,
    )

    db.refresh(slot_a)
    db.refresh(slot_b)
    # The PINNED slot (A) is locked; the model's mis-parsed slot (B) is untouched.
    assert slot_a.status == SlotStatus.locked and slot_a.locked_by_session == session_id
    assert slot_b.status == SlotStatus.open

    # Audit proves the exact id + the chip-tap marker.
    call = next(
        e for e in _events(db, session_id)
        if e.type == AuditType.tool_call and e.payload["tool"] == "lock_slot"
    )
    assert call.payload["input"]["slot_id"] == slot_a.id
    assert call.payload.get("slot_id_pinned") is True


def test_chip_tap_stale_slot_fails_gracefully(slots):
    """Case 1 + 2: the pin does NOT force the lock. If the slot was taken between
    chip render and tap, lock_slot's availability guard still runs and returns
    not-available as DATA — the agent re-offers, no crash, no forced double-lock."""
    db = slots
    session_id = "sess-stale"
    (slot,) = _earliest_open_showroom_slots(db, n=1)

    # Taken since the chips rendered (a concurrent QR attendee grabbed it).
    slot.status = SlotStatus.booked
    db.commit()

    steps = [_tool_use("u1", "lock_slot", slot_id=slot.id), _text("Ah, that one just went.")]
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=ScriptedModel(steps), provided_slot_id=slot.id)
    final = graph.invoke(
        {"messages": [{"role": "user", "content": "I'll take that 6:30 PM one."}],
         "iterations": 0, "customer_id": None},
        config,
    )

    # lock_slot returned not-available as data (not an exception, not a forced lock).
    result = _tool_result_for(final, "lock_slot", steps)
    assert result.get("locked") is False
    assert result.get("reason")  # e.g. slot_no_longer_available
    db.refresh(slot)
    assert slot.status == SlotStatus.booked  # still the other party's; we did not steal it
    assert slot.locked_by_session != session_id


# --------------------------------------------------------------------------- #
# Booking-management journey through the WHOLE graph (FR-RS, FR-CN, rules 10/11).
# Reschedule then cancel, end to end, through the real write guards and the SAME
# correlation-id threading create_booking established. This proves the journey
# S-09/S-11/S-12/S-13 exercise runs on the production path — the tool node,
# correlation injection, and the service guards — not just the services in
# isolation (that is test_booking_management.py).
#
# NOTE on "no-loop" (rule 11): ScriptedModel replays canned turns and IGNORES the
# prompt, so this cannot prove the model won't offer twice — that is a live-LLM
# eval. What it shows is the journey's happy path (one retention offer as text,
# then a single cancel) producing exactly one clean cancellation; the idempotency
# backstop that makes a stray loop harmless is asserted in test_booking_management.
# --------------------------------------------------------------------------- #
def _far_t2_slots(db, n):
    """`n` earliest open T2 showroom slots > 25h out, so both reminders are future."""
    from datetime import datetime, timedelta

    from app.models import LocationType, Slot

    cutoff = datetime.now() + timedelta(hours=25)
    return db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == 2,
            Slot.start > cutoff,
        ).order_by(Slot.start, Slot.id)
    ).all()[:n]


def test_reschedule_then_cancel_journey_end_to_end(slots):
    db = slots
    from app.models import Notification, NotificationStatus
    from app.services import booking as booking_service
    from app.services import customers as customers_service
    from app.services import slots as slots_svc

    # --- seed a real, guarded booking on old_slot --------------------------- #
    cid = customers_service.find_or_create(db, mobile=MOBILE, name="Test Driver")["customer"][
        "customer_id"
    ]
    old_slot, new_slot = _far_t2_slots(db, 2)
    slots_svc.lock(db, session_id="seed", slot_id=old_slot.id)
    slots_svc.acknowledge_licence_reminder(db, session_id="seed")  # rule 9 precondition 3
    seeded = booking_service.create(
        db, session_id="seed", customer_id=cid, vehicle_id=2, slot_id=old_slot.id
    )
    db.commit()
    ref = seeded["booking"]["booking_ref"]
    assert TD_REF.match(ref)

    # --- RESCHEDULE through the graph (S-09) -------------------------------- #
    r_session = "sess-resched"
    r_steps = [
        # Identity first (developer decision 2026-07-23): get_booking / reschedule
        # now require a verified session. Full name + mobile, and the name must
        # match the seeded "Test Driver" or the session would not bind.
        _tool_use("g0", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _tool_use("g1", "get_booking", ref=ref),
        _tool_use("g2", "get_available_slots", vehicle_id=2, location_type="showroom"),
        _tool_use("g3", "lock_slot", slot_id=new_slot.id),
        _tool_use("g4", "reschedule_booking", new_slot_id=new_slot.id, ref=ref),
        _text("Moved you to the new time."),
    ]
    final_r = _run(db, r_session, r_steps)

    r_result = _tool_result_for(final_r, "reschedule_booking", r_steps)
    assert r_result["rescheduled"] is True, r_result
    assert r_result["booking"]["booking_ref"] == ref  # ref stable (S-11)
    assert r_result["booking"]["slot_id"] == new_slot.id

    # old slot released, new slot booked
    db.refresh(old_slot)
    db.refresh(new_slot)
    assert old_slot.status == SlotStatus.open
    assert new_slot.status == SlotStatus.booked

    # only the new slot's reminders remain scheduled (S-09)
    sched = {
        n.kind.value
        for n in db.scalars(
            select(Notification).where(
                Notification.ref == ref, Notification.status == NotificationStatus.scheduled
            )
        ).all()
    }
    assert sched == {"reschedule", "t24", "t2"}

    # correlation: reschedule tool_call <-> intent <-> notifications share ONE id
    r_events = _events(db, r_session)
    r_call = next(
        e for e in r_events if e.type == AuditType.tool_call and e.payload["tool"] == "reschedule_booking"
    )
    r_intent = next(
        e for e in r_events if e.type == AuditType.intent and e.payload.get("intent") == "reschedule_booking"
    )
    assert r_call.payload["correlation_id"] == r_intent.payload["correlation_id"]
    assert r_intent.payload["ref"] == ref
    assert any(
        e.type == AuditType.notification
        and e.payload.get("correlation_id") == r_intent.payload["correlation_id"]
        for e in r_events
    )

    # --- CANCEL through the graph (S-13) ------------------------------------ #
    # The one retention offer / no-loop is a PROMPT behaviour across turns (rule
    # 11) and cannot be a mid-run text here — a text-only turn ends the graph.
    # This run is the mechanical path: look up, then a SINGLE cancel. The
    # idempotency backstop (a doubled cancel stays one cancellation) lives in
    # test_booking_management.test_cancel_is_idempotent_so_a_loop_cannot_double_apply.
    c_session = "sess-cancel"
    c_steps = [
        # Identity first, as above — this fresh session must verify before it can
        # look up or cancel the booking.
        _tool_use("c0", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _tool_use("c1", "get_booking", mobile=MOBILE),
        _tool_use("c2", "cancel_booking", ref=ref),
        _text("Done — it's cancelled."),
    ]
    final_c = _run(db, c_session, c_steps)

    c_result = _tool_result_for(final_c, "cancel_booking", c_steps)
    assert c_result["cancelled"] is True, c_result
    assert c_result["winback_flag"] is True

    # slot released to inventory, customer flagged for win-back
    db.refresh(new_slot)
    assert new_slot.status == SlotStatus.open
    from app.models import Customer

    assert db.get(Customer, cid).winback_flag is True

    # the only scheduled row left is the written cancellation notice (FR-CN-03)
    sched_after = {
        n.kind.value
        for n in db.scalars(
            select(Notification).where(
                Notification.ref == ref, Notification.status == NotificationStatus.scheduled
            )
        ).all()
    }
    assert sched_after == {"cancellation"}

    # correlation: cancel tool_call <-> intent share ONE id
    c_events = _events(db, c_session)
    c_call = next(
        e for e in c_events if e.type == AuditType.tool_call and e.payload["tool"] == "cancel_booking"
    )
    c_intent = next(
        e for e in c_events if e.type == AuditType.intent and e.payload.get("intent") == "cancel_booking"
    )
    assert c_call.payload["correlation_id"] == c_intent.payload["correlation_id"]
    assert c_intent.payload["ref"] == ref


# --------------------------------------------------------------------------- #
# The original slot-chip bug, replayed through the WHOLE graph (FR-SL-01/04).
# Two turns on one thread: offer Monday slots, then "I've office at 9am". The
# scripted model carries the constraint into the QUERY (earliest_hour), so the
# tool filters server-side and NO 9 AM slot reaches the tool_event — hence none
# can reach the chips (the frontend renders the tool output verbatim now).
#
# HONEST SCOPE: ScriptedModel is scripted, so this proves the PLUMBING end to end
# — when the model passes earliest_hour, the constraint reaches the tool and the
# ruled-out slot never comes back. Whether a *live* model passes it is nudged by
# the tool description (Fix 3) and is a live-eval concern; no API key runs here.
# --------------------------------------------------------------------------- #
def test_office_at_9am_constraint_reaches_the_query_no_9am_in_tool_event(slots):
    db = slots
    from app.models import Showroom, Slot, VehicleModel

    x70fl = db.scalar(select(VehicleModel).where(VehicleModel.name.like("%X70 FL%")))
    alquoz = db.scalar(select(Showroom).where(Showroom.name.like("%Al Quoz%")))
    assert x70fl and alquoz
    vid, shid = x70fl.id, alquoz.id

    # A day in the window that genuinely has an early (< 11 AM) Al Quoz slot for
    # the car — not a Sunday (opens noon), which would have no morning slot for the
    # bound to remove and would make this test about the wrong thing.
    early = next(
        (
            s
            for s in db.scalars(
                select(Slot).where(
                    Slot.vehicle_id == vid, Slot.showroom_id == shid,
                    Slot.status == SlotStatus.open,
                ).order_by(Slot.start)
            ).all()
            if s.start.hour < 11
        ),
        None,
    )
    assert early is not None, "fixture must have an early Al Quoz slot for the X70 FL"
    day = early.start.date().isoformat()

    turn1 = [
        _tool_use("q1", "get_available_slots", vehicle_id=vid, showroom_id=shid, date_from=day, date_to=day),
        _text("For Monday at Al Quoz I can do 9 AM, 11 AM, or 1 PM. Which suits you?"),
    ]
    # Turn 2: the model carries "office at 9am" into the query as earliest_hour.
    turn2 = [
        _tool_use("q2", "get_available_slots",
                  vehicle_id=vid, showroom_id=shid, date_from=day, date_to=day, earliest_hour=11),
        _text("No problem — later at Al Quoz I have 11 AM or 1 PM. Which works?"),
    ]

    graph = build_graph(new_sqlite_checkpointer())
    model = ScriptedModel(turn1 + turn2)
    session = "sess-office-9am"
    config = build_config(db, session, generate=model)

    graph.invoke({"messages": [{"role": "user", "content": f"X70 FL, Monday {day} at Al Quoz."}],
                  "iterations": 0, "customer_id": None}, config)
    final = graph.invoke({"messages": [{"role": "user", "content": "al quoz is near me, but I've office at 9am"}],
                          "iterations": 0, "customer_id": None}, config)

    # (1) The MODEL carried the constraint into the QUERY (not just its prose).
    q2_call = next(
        e for e in _events(db, session)
        if e.type == AuditType.tool_call and e.payload["tool"] == "get_available_slots"
        and e.payload["input"].get("earliest_hour") is not None
    )
    assert q2_call.payload["input"]["earliest_hour"] == 11

    # (2) The tool_event OUTPUT for that call has NO slot before 11 AM — so nothing
    # a 9 AM chip could be built from ever reaches the frontend.
    def _result(tid):
        for msg in final["messages"]:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tid:
                        return json.loads(b["content"])
        raise AssertionError("no tool_result for q2")

    out2 = _result("q2")
    from datetime import datetime as _dt
    assert out2["slots"], "should still have later slots"
    assert len(out2["slots"]) <= 3  # FR-SL-01 cap
    for s in out2["slots"]:
        assert _dt.fromisoformat(s["start_iso"]).hour >= 11, f"9 AM leaked into the tool_event: {s['start_iso']}"

    # And the first turn genuinely DID include 9 AM (so the bound is what removed it).
    out1 = _result("q1")
    assert any(_dt.fromisoformat(s["start_iso"]).hour < 11 for s in out1["slots"]), \
        "the unconstrained first offer should have contained an early slot"


# --------------------------------------------------------------------------- #
# Prompt caching — breakpoint PLACEMENT (structure only; hits need a live call)
# --------------------------------------------------------------------------- #
def test_history_breakpoint_sits_on_the_last_block():
    """The conversation caches too, so a later turn does not re-pay full price
    for every earlier one. One MOVING breakpoint: each call marks the end of
    what exists now, and the previous call's entry is a prefix of this one."""
    from app.agent.graph import _cached_messages

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    out = _cached_messages(msgs)

    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Exactly one, and only at the end — an accumulating set would burn the
    # 4-breakpoint budget the system prompt already draws on.
    marked = [
        b
        for m in out
        for b in (m["content"] if isinstance(m["content"], list) else [])
        if isinstance(b, dict) and "cache_control" in b
    ]
    assert len(marked) == 1


def test_caching_never_mutates_graph_state():
    """`messages` is checkpointed state. Stamping cache_control onto it would
    persist a request-shaping detail as if it were conversation content."""
    from app.agent.graph import _cached_messages

    original = [{"role": "user", "content": "hi"}]
    _cached_messages(original)
    assert original == [{"role": "user", "content": "hi"}]


def test_string_content_still_gets_a_breakpoint():
    """Customer turns arrive as a plain string from chat.py — the common case,
    and the one where a missing breakpoint would silently cost full price."""
    from app.agent.graph import _cached_messages

    out = _cached_messages([{"role": "user", "content": "book me a T2"}])
    block = out[-1]["content"][-1]
    assert block["text"] == "book me a T2"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_empty_history_is_left_alone():
    from app.agent.graph import _cached_messages

    assert _cached_messages([]) == []
