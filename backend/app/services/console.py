"""Read models for the admin console (FR-AD-02, behaviour rule 15/16).

READS ONLY. Nothing here writes, and that is structural rather than incidental:
the console's write actions (regenerate slots, release a lock, cancel a booking)
reuse `slots.generate`, `slots.release_lock` and `booking.cancel` verbatim, so
the write guards, the invariants and the audit choke point are the SAME code the
agent goes through. A second write path that happened to skip a guard is exactly
the defect AD-I exists to prevent, so this module offers no way to build one.

WHY A SERVICE AND NOT QUERIES IN THE ROUTE (AD-I). Every other caller in this
codebase reaches the database through a service; a console that queried models
directly would be the one exception, and the first place a future reader learns
that the rule is optional. It also keeps the console testable without HTTP.

TWO RESPONSE SHAPES, DELIBERATELY
---------------------------------
The three endpoints named in the CLAUDE.md API contract — audit, metrics,
notifications — return exactly what that contract says, and `audit`/
`notifications` return a BARE LIST. The data-browser endpoints added alongside
them return a paginated envelope, `{rows, total, limit, offset}`. Resisting the
temptation to "harmonise" the two is the point: the contract is signed and the
frontend rule for it is already written down, so it is not ours to reshape as a
side effect of adding a browser next to it. `audit_for_session` (contract) and
`audit_log` (browser) are therefore two functions over one table, not one
function with a flag.

PII, STATED PLAINLY AND FLAGGED FOR THE D1 PDPL CHECKLIST. `list_customers` and
`list_bookings` return names and +971 mobiles, and the console is UNGATED by the
same decision that leaves every other endpoint ungated (2026-07-21). That is a
heavier exposure than the ref-alone booking lookup already on the checklist:
GET /api/booking/{ref} gates PII on the viewing session precisely so a guessable
reference cannot surface a stranger's phone number in front of an audience, and
this lists every customer at once with no gate at all. It is a demo posture, not
a defensible production one. The mitigation is one dependency on the console
router — the router and the frontend's single api module are both structured so
that gate is two insertion points and no rework.

NO N+1 ON CLOSED-STATUS. There is no Conversation table (see services/
conversation.py): "has this session closed?" is derived by scanning that
session's `intent` audit rows. Calling `conversation.is_closed` once per row
would be a query per conversation, so `list_conversations` fetches the close
markers for the CURRENT PAGE in one query and resolves them in Python — reusing
`conversation.CLOSE_ACTION` so the marker can never drift from the writer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Actor,
    AuditEvent,
    AuditType,
    Booking,
    BookingAttendance,
    BookingStatus,
    ConversationTurn,
    Customer,
    FuelType,
    Language,
    LocationType,
    Notification,
    NotificationKind,
    NotificationStatus,
    Showroom,
    Slot,
    SlotStatus,
    Speaker,
    TurnTrace,
    VehicleModel,
    VehicleVariant,
)
from app.services import conversation
from app.services.catalogue import _image_url

# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #
# The first pagination code in the repo — nothing generic existed to reuse (the
# only caps that ship are DEFAULT_SLOT_LIMIT=3 and MAX_RECOMMENDATIONS=3, both
# domain rules about what a CUSTOMER may be shown, not transport concerns).
DEFAULT_PAGE = 50
MAX_PAGE = 200

# The agent registry (rule 16). ONE LIVE ROW, by explicit decision 2026-07-21:
# the console shows what exists and makes no forward-looking claim. `planned`
# remains in the contract's enum because the contract says so, and because the
# roadmap framing may come back — but nothing may be added here that is not
# actually running, or the dashboard starts over-claiming, which is the one
# thing rule 16 names outright.
AGENT_REGISTRY: tuple[dict[str, str], ...] = (
    {"agent": "Test Drive Booking Agent", "status": "live"},
)


def _clamp_page(limit: object, offset: object) -> tuple[int, int]:
    """Clamp paging into a sane window.

    RANGE policy only — TYPE validation belongs to the route, and FastAPI has
    already rejected a non-integer `?limit=abc` with a 422 before this is
    reached. So the values that actually arrive here are well-typed but
    out-of-range: `limit=0`, `limit=-5` and `limit=99999` are client bugs that
    should degrade to a normal page rather than dumping 3,800 slot rows on
    stage. The try/except is for the OTHER callers — tests and any future CLI
    reach these services directly, with no FastAPI layer in front to type-check
    them.
    """
    try:
        lim = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        lim = DEFAULT_PAGE
    try:
        off = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        off = 0
    return max(1, min(lim, MAX_PAGE)), max(0, off)


def _envelope(rows: list, total: int, limit: int, offset: int) -> dict:
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def _count(db: Session, column, filters: list) -> int:
    stmt = select(func.count(column))
    if filters:
        stmt = stmt.where(*filters)
    return int(db.scalar(stmt) or 0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _enum(value) -> str | None:
    """Enum -> its wire value, tolerating a plain string or None.

    Rows are read back as enums, but a hand-written filter or a legacy row can
    hand us the raw string; the console must render either rather than 500.
    """
    if value is None:
        return None
    return getattr(value, "value", value)


def _coerce_enum(raw: object, enum_cls):
    """Query-string value -> enum member, or None if it is not a valid one.

    Returning None (rather than raising) means an unknown ?status=foo is simply
    an unapplied filter. A 422 mid-demo for a typo in a URL is a worse outcome
    than a slightly wider result set, and the UI only ever sends known values.
    """
    if raw is None or raw == "":
        return None
    try:
        return enum_cls(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def _variant_row(x: VehicleVariant) -> dict:
    """One trim, as the console shows it (T0.11).

    ON-ROAD IS THE MODEL'S PROPERTY, NOT A SUM HERE. It returns None unless
    price, insurance and registration are all present — a partial total would
    land BELOW the list price and read as a discount on an internal screen the
    operator trusts.
    """
    return {
        "variant_id": x.id,
        # NULL NAME MEANS "the only version", not "unknown". The UI must render
        # the model name alone rather than the word None.
        "name": x.name,
        "price_aed": x.price_aed,
        "insurance_aed": x.insurance_aed,
        "registration_aed": x.registration_aed,
        "on_road_aed": x.on_road_aed,
        "seats_min": x.seats_min,
        "seats_max": x.seats_max,
        "km_per_l_min": x.km_per_l_min,
        "km_per_l_max": x.km_per_l_max,
        # WHY THE OPERATOR CAME TO THIS SCREEN, half the time. "Coming soon" is
        # not "sold out" and not "price missing" — it is the answer to "why
        # won't Aisha quote this car?", which the module comment above already
        # names as the reason the price absence is rendered visibly.
        "coming_soon": bool(x.coming_soon),
    }


def _vehicle_row(v: VehicleModel, variants: list | None = None) -> dict:
    return {
        "vehicle_id": v.id,
        "model_code": v.model_code,
        "brand": v.brand,
        "name": v.name,
        "body_type": v.body_type,
        "seats_min": v.seats_min,
        "seats_max": v.seats_max,
        # Both nullable, and null means "not published" — never 0, and the UI
        # must render an absence rather than a price of nothing (FR-CX-07).
        "price_aed_min": v.price_aed_min,
        "price_aed_max": v.price_aed_max,
        "fuel_type": _enum(v.fuel_type),
        "showroom_ids": v.showroom_ids or [],
        "headline_features": v.headline_features or [],
        "headline_features_ar": v.headline_features_ar or [],
        "images": v.images or [],
        # Derived by catalogue._image_url rather than re-implemented here: the
        # stored path is "seed/media/vehicles/x.png" and the browser needs
        # "/media/vehicles/x.png". Duplicating that mapping would give the repo
        # two places to change when the media mount moves, and one of them would
        # be missed. Private, but same package, and one source of truth beats
        # tidier imports. None when the row has no image — render no image, never
        # a broken one.
        "image_url": _image_url(v.images),
        # T0.11: the catalogue is variant-level now, and a model card showing
        # only a RANGE hides what the operator came to check. Ordered by PRICE,
        # never by name — the T1's Luxury is its 4WD range-topper and costs more
        # than its Flagship, so a name sort would draw a ladder that does not
        # exist. Unpriced sorts last: coming soon, not cheapest.
        "variants": [
            _variant_row(x)
            for x in sorted(
                variants or [], key=lambda x: (x.price_aed is None, x.price_aed or 0, x.id)
            )
        ],
        # Model-level and identical across trims. NOT paired with an exterior
        # colour and not stocked — see VehicleModel.interior_colours.
        "interior_colours": v.interior_colours or [],
    }


def list_vehicles(
    db: Session,
    *,
    brand: str | None = None,
    fuel: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    """The catalogue, paginated. Both languages — this is an internal screen."""
    lim, off = _clamp_page(limit, offset)
    filters = []
    if brand:
        filters.append(VehicleModel.brand == brand)
    fuel_enum = _coerce_enum(fuel, FuelType)
    if fuel_enum is not None:
        filters.append(VehicleModel.fuel_type == fuel_enum)

    stmt = select(VehicleModel)
    if filters:
        stmt = stmt.where(*filters)
    rows = db.scalars(
        stmt.order_by(VehicleModel.brand, VehicleModel.name).limit(lim).offset(off)
    ).all()
    # ONE QUERY FOR EVERY CARD'S TRIMS. A page is twelve vehicles, so a
    # per-row lookup would be twelve round trips for a fifteen-row table.
    by_model: dict[int, list] = {}
    if rows:
        for x in db.scalars(
            select(VehicleVariant)
            .where(VehicleVariant.model_id.in_([v.id for v in rows]))
            .order_by(VehicleVariant.model_id, VehicleVariant.id)
        ).all():
            by_model.setdefault(x.model_id, []).append(x)

    return _envelope(
        [_vehicle_row(v, by_model.get(v.id, [])) for v in rows],
        _count(db, VehicleModel.id, filters),
        lim,
        off,
    )


# --------------------------------------------------------------------------- #
# Handoffs queue (T8.1)
# --------------------------------------------------------------------------- #
# WHAT CHANGED, AND WHY IT MATTERS NOW. services/escalation.py has always said
# so plainly in its own docstring: it writes an audit row and "there is no
# supervisor UI and no real callback is placed". That was defensible while a
# handoff was a rare dead end. From Phase 6 it is the FINISH LINE of the
# highest-value conversations in the system — a customer who has decided to buy,
# or wants a number negotiated, is handed here and nowhere else.
#
# A queue nobody can see is a promise nobody keeps. `_escalations` above already
# aggregates these for the dashboard COUNT; this returns the rows themselves,
# oldest first, with enough of the conversation to act on.
#
# OLDEST FIRST, DELIBERATELY, and it is the opposite of every other browser
# here. Newest-first is right for an audit log, which is read to find out what
# just happened. This is a WORK QUEUE: the person waiting longest is the one
# being let down most, and sorting them to the bottom is how a queue quietly
# starves its oldest entry.
#
# DONE IS DERIVED, NOT STORED. There is no `handled` column and no new table:
# an escalation counts as handled when a later audit row for the same session
# says a human acted. Adding a column would mean a second source of truth for
# "was this dealt with", and the first thing to go stale.


def _handoff_row(
    event: AuditEvent,
    customer: Customer | None,
    takeover: dict | None = None,
    session_active: bool = True,
) -> dict:
    payload = event.payload or {}
    return {
        # Human takeover (2026-08-31): who holds this conversation live, if
        # anyone — the first "later audit row saying a human acted" the derived
        # DONE above anticipated. Null when nobody has taken it yet.
        "taken_over_by": (takeover or {}).get("admin_name"),
        "taken_over_ts": (takeover or {}).get("ts"),
        # Is the conversation still open? Derived from the same trail (the
        # close marker in the intent rows), so the queue can withhold "Take
        # over" from a chat nobody is in any more — a closed session is
        # callback-only, whatever its reason.
        "session_active": session_active,
        "audit_id": event.id,
        "ts": event.ts.isoformat() if event.ts else None,
        "session": event.session,
        "reason": payload.get("reason") or "other",
        # The customer's OWN WORDS for what they want a person for ("discount on
        # the T2"). Shown to the operator so they know what they are calling
        # about; never spoken back as policy (rule 13).
        "topic": payload.get("topic"),
        "customer_id": payload.get("customer_id"),
        # PII, like list_customers and list_bookings — and on the same ungated
        # console. Flagged for the D1 PDPL checklist with them, not separately:
        # a callback queue without a name and a number is not a callback queue.
        "customer_name": getattr(customer, "name_en", None) if customer else None,
        "customer_mobile": getattr(customer, "mobile", None) if customer else None,
    }


def list_handoffs(
    db: Session,
    *,
    reason: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    """The human-callback queue, oldest first.

    Every `request_human_callback` that fired, with the conversation behind it.
    """
    lim, off = _clamp_page(limit, offset)
    filters = [AuditEvent.type == AuditType.escalation]

    stmt = select(AuditEvent).where(*filters)
    rows = list(
        db.scalars(
            stmt.order_by(AuditEvent.ts.asc(), AuditEvent.id.asc())
        ).all()
    )
    if reason:
        rows = [r for r in rows if (r.payload or {}).get("reason") == reason]

    total = len(rows)
    page = rows[off : off + lim]

    # One query for every customer on the page, not one per row.
    ids = {
        (r.payload or {}).get("customer_id")
        for r in page
        if (r.payload or {}).get("customer_id")
    }
    customers = {
        c.id: c
        for c in (
            db.scalars(select(Customer).where(Customer.id.in_(ids))).all()
            if ids else []
        )
    }

    # One query for every takeover AND every close on the page, not one per
    # row — both facts live in the same `intent` rows, so one scan reads both.
    # Payload scan in Python, the same portable-JSON call state() itself makes.
    from app.services.conversation import CLOSE_ACTION
    from app.services.takeover import TAKEOVER_ACTION

    sessions = {r.session for r in page}
    takeovers: dict[str, dict] = {}
    closed_sessions: set[str] = set()
    if sessions:
        for ev in db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.session.in_(sessions),
                AuditEvent.type == AuditType.intent,
            )
            .order_by(AuditEvent.ts.asc(), AuditEvent.id.asc())
        ).all():
            p = ev.payload or {}
            action = p.get("action")
            if action == TAKEOVER_ACTION and ev.session not in takeovers:
                takeovers[ev.session] = {
                    "admin_name": p.get("admin_name"),
                    "ts": ev.ts.isoformat() if ev.ts else None,
                }
            elif action == CLOSE_ACTION:
                closed_sessions.add(ev.session)

    return _envelope(
        [
            _handoff_row(
                r,
                customers.get((r.payload or {}).get("customer_id")),
                takeovers.get(r.session),
                session_active=r.session not in closed_sessions,
            )
            for r in page
        ],
        total,
        lim,
        off,
    )


# --------------------------------------------------------------------------- #
# Routing view (T8.2)
# --------------------------------------------------------------------------- #
# ALL OF THIS WAS ALREADY BEING RECORDED AND NONE OF IT WAS VISIBLE ANYWHERE.
# turn_trace has carried the routing decision since the router shipped, and
# Phase 3 added `steps` (which agent, chosen by which layer, at what score and
# margin, how long, whether it worked) and `prompt_versions`. Every field below
# is a read; nothing new is computed and nothing new is stored.
#
# THIS IS WHAT MAKES THE THRESHOLD TUNABLE RATHER THAN A GUESS. MIN_MARGIN is
# 0.02 because 32 synthetic card utterances said so — twelve samples at the
# chosen boundary. The only way that stops being a guess is reading the scores
# real briefs produce, and this is where they surface.


def _routing_row(t: TurnTrace) -> dict:
    steps = t.steps or []
    return {
        "trace_id": t.trace_id,
        "session": t.session_id,
        "ts": t.created_at.isoformat() if t.created_at else None,
        # What the customer actually said. PII on an ungated console, exactly
        # like list_conversations — flagged with it, not separately.
        "user_message": t.user_message,
        "detected_lang": t.detected_lang,
        # The SHIPPED router's decision, which is still the live one.
        "routed_to": t.routed_to,
        "previous_agent": t.previous_agent,
        "router_latency_ms": t.router_latency_ms,
        "latency_ms": t.latency_ms,
        # The MULTI-AGENT cascade's per-step record. Empty on every turn that
        # ran single-agent, which is all of them until the kill switch flips —
        # so an empty list here means "not applicable", never "it failed".
        "steps": steps,
        "step_count": len(steps),
        # Straight from the steps, so the routing view answers the one question
        # the calibration cannot: how often does the cheap path actually settle
        # a REAL brief, and how close were the ones it declined?
        "fast_lane_steps": sum(1 for s in steps if s.get("layer") == "fast_lane"),
        "judged_steps": sum(1 for s in steps if s.get("layer") == "judge"),
        "failed_steps": sum(1 for s in steps if s.get("status") not in (None, "ok")),
        # Which prompt answered this turn (T2.4). Prompts are editable without a
        # deploy, so this is what pins a quality change to a prompt change.
        "prompt_versions": t.prompt_versions,
    }


def list_routing(
    db: Session,
    *,
    session: str | None = None,
    routed_to: str | None = None,
    only_multi_agent: bool = False,
    limit: object = None,
    offset: object = None,
) -> dict:
    """Per-turn routing detail, newest first.

    NEWEST FIRST, unlike the handoffs queue — this is read to find out what just
    happened, not worked through. The two orders are opposite on purpose.
    """
    lim, off = _clamp_page(limit, offset)
    filters = []
    if session:
        filters.append(TurnTrace.session_id == session)
    if routed_to:
        filters.append(TurnTrace.routed_to == routed_to)

    stmt = select(TurnTrace)
    if filters:
        stmt = stmt.where(*filters)
    rows = list(
        db.scalars(
            stmt.order_by(TurnTrace.created_at.desc(), TurnTrace.id.desc())
        ).all()
    )
    if only_multi_agent:
        rows = [r for r in rows if r.steps]

    total = len(rows)
    return _envelope([_routing_row(r) for r in rows[off : off + lim]], total, lim, off)


def routing_summary(db: Session) -> dict:
    """How the cascade is behaving, in aggregate.

    THE NUMBER THIS EXISTS FOR is `fast_lane_share`. The calibration predicted
    the embedding shortcut would settle about a third of briefs; this says what
    it settles on real ones. If it is far lower, the fast lane is not earning
    its complexity and the honest move is to delete it and always judge.
    """
    traces = [t for t in db.scalars(select(TurnTrace)).all() if t.steps]
    steps = [s for t in traces for s in (t.steps or [])]
    if not steps:
        return {
            "turns": 0, "steps": 0, "fast_lane": 0, "judged": 0,
            "fast_lane_share": None, "failed": 0, "by_agent": {},
        }
    fast = sum(1 for s in steps if s.get("layer") == "fast_lane")
    judged = sum(1 for s in steps if s.get("layer") == "judge")
    by_agent: dict[str, int] = {}
    for s in steps:
        agent = s.get("agent") or "unrouted"
        by_agent[agent] = by_agent.get(agent, 0) + 1
    return {
        "turns": len(traces),
        "steps": len(steps),
        "fast_lane": fast,
        "judged": judged,
        "fast_lane_share": round(fast / len(steps), 3),
        "failed": sum(1 for s in steps if s.get("status") not in (None, "ok")),
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1])),
    }


def _showroom_row(s: Showroom) -> dict:
    return {
        "showroom_id": s.id,
        "brand": s.brand,
        "name": s.name,
        "city": s.city,
        "emirate": s.emirate,
        "address": s.address,
        "address_ar": s.address_ar,
        "map_link": s.map_link,
        "phone": s.phone,
        # hours_by_day is the source of truth slot generation reads; `hours` is
        # display-only. Both are surfaced so a wrong slot window can be traced
        # to whichever of the two is actually wrong.
        "hours_by_day": s.hours_by_day or {},
        "hours": s.hours,
    }


def list_showrooms(
    db: Session,
    *,
    emirate: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    lim, off = _clamp_page(limit, offset)
    filters = []
    if emirate:
        filters.append(Showroom.emirate == emirate)

    stmt = select(Showroom)
    if filters:
        stmt = stmt.where(*filters)
    rows = db.scalars(
        stmt.order_by(Showroom.emirate, Showroom.name).limit(lim).offset(off)
    ).all()
    return _envelope(
        [_showroom_row(s) for s in rows],
        _count(db, Showroom.id, filters),
        lim,
        off,
    )


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def _slot_row(s: Slot) -> dict:
    return {
        "slot_id": s.id,
        "start": _iso(s.start),
        "end": _iso(s.end),
        "status": _enum(s.status),
        "location_type": _enum(s.location_type),
        "vehicle_id": s.vehicle_id,
        "vehicle_name": s.vehicle.name if s.vehicle else None,
        # Inventory is per demo car, not per model — a branch's nine models are
        # fourteen cars — so a Slots tab that showed only the model would list
        # what looks like duplicate rows at the same time on the same day.
        "variant_id": s.variant_id,
        "variant_name": s.variant.name if s.variant else None,
        "showroom_id": s.showroom_id,
        "showroom_name": s.showroom.name if s.showroom else None,
        "service_area": s.service_area,
        # The lock's own fields. Surfaced HERE and only here: rule 14's corollary
        # forbids the AGENT ever learning a lock's timing, because a model told
        # "this expires at 19:42" will eventually pressure a customer with it.
        # An operator looking at inventory is not the agent, and "which session
        # is holding this and since when" is the whole reason a release-lock
        # button can be used correctly.
        "locked_by_session": s.locked_by_session,
        "locked_at": _iso(s.locked_at),
        "licence_ack": bool(s.licence_ack),
    }


def list_slots(
    db: Session,
    *,
    status: str | None = None,
    vehicle_id: object = None,
    showroom_id: object = None,
    location_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    """The rolling slot window. The largest table here — paging is load-bearing.

    14 days x 6 showrooms of evening-weighted slots runs to thousands of rows,
    so this is the one list that would visibly hurt if it were unbounded.
    """
    lim, off = _clamp_page(limit, offset)
    filters = []

    status_enum = _coerce_enum(status, SlotStatus)
    if status_enum is not None:
        filters.append(Slot.status == status_enum)
    loc_enum = _coerce_enum(location_type, LocationType)
    if loc_enum is not None:
        filters.append(Slot.location_type == loc_enum)
    if vehicle_id not in (None, ""):
        try:
            filters.append(Slot.vehicle_id == int(vehicle_id))
        except (TypeError, ValueError):
            pass
    if showroom_id not in (None, ""):
        try:
            filters.append(Slot.showroom_id == int(showroom_id))
        except (TypeError, ValueError):
            pass
    for raw, op in ((date_from, "from"), (date_to, "to")):
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        filters.append(Slot.start >= parsed if op == "from" else Slot.start <= parsed)

    stmt = select(Slot).options(
        selectinload(Slot.vehicle), selectinload(Slot.showroom)
    )
    if filters:
        stmt = stmt.where(*filters)
    rows = db.scalars(stmt.order_by(Slot.start).limit(lim).offset(off)).all()
    return _envelope(
        [_slot_row(s) for s in rows], _count(db, Slot.id, filters), lim, off
    )


# --------------------------------------------------------------------------- #
# Bookings & customers  (PII — see the module docstring)
# --------------------------------------------------------------------------- #
def _booking_row(b: Booking, *, survey: object = None) -> dict:
    cust = b.customer
    slot = b.slot
    # The branch this booking is at, reached through the slot — a Booking has no
    # showroom of its own, because WHERE a test drive happens is a property of
    # the slot that was locked, not a second field that could disagree with it.
    #
    # Field names match services/booking._booking_dict (showroom_id /
    # showroom_name / showroom_map_link / service_area) so the console and the
    # customer-facing endpoint describe the same booking in the same words.
    # Unlike that dict, which emits EITHER the showroom keys OR service_area, a
    # table has fixed columns — so every key is always present and simply null
    # on the branch that does not apply. A doorstep booking has no showroom and
    # a showroom booking has no service area; that is data, not a gap.
    room = slot.showroom if slot else None
    return {
        "booking_id": b.id,
        "ref": b.ref,
        "status": _enum(b.status),
        # The second axis (2026-07-23): `status` says whether the booking is
        # alive, `attendance` says whether the customer turned up. Both are
        # always present and neither derives from the other. `noshow_notified_at`
        # is here so an operator can see that the "we missed you" message has
        # already gone — the row would otherwise look identical before and after.
        "attendance": _enum(b.attendance),
        "noshow_notified_at": _iso(b.noshow_notified_at),
        "customer_id": b.customer_id,
        "customer_name_en": cust.name_en if cust else None,
        "customer_name_ar": cust.name_ar if cust else None,
        "customer_mobile": cust.mobile if cust else None,
        "driver_name": b.driver_name,
        "driver_relationship": b.driver_relationship,
        "vehicle_id": b.vehicle_id,
        "vehicle_name": b.vehicle.name if b.vehicle else None,
        # WHICH TRIM, so the console shows what a consultant will actually be
        # handing over. Null on a single-trim model (the seed's convention) and
        # on the historic rows the backfill could not settle — both mean "the
        # model name is the whole answer", so a reader shows nothing extra.
        "variant_id": b.variant_id,
        "variant_name": b.variant.name if b.variant else None,
        "slot_id": b.slot_id,
        "start": _iso(slot.start) if slot else None,
        "end": _iso(slot.end) if slot else None,
        "location": _enum(b.location),
        "showroom_id": slot.showroom_id if slot else None,
        "showroom_name": room.name if room else None,
        "showroom_city": room.city if room else None,
        "showroom_emirate": room.emirate if room else None,
        "showroom_address": room.address if room else None,
        # The genuinely bilingual one: a street address cannot be composed at
        # speaking time the way a price or a seat count can, so it is stored in
        # both scripts and both are shown (LOC-01).
        "showroom_address_ar": room.address_ar if room else None,
        "showroom_phone": room.phone if room else None,
        "showroom_map_link": room.map_link if room else None,
        # Doorstep's counterpart to a showroom: the fleet service area.
        "service_area": slot.service_area if slot else None,
        "doorstep_address": b.doorstep_address,
        "companions": b.companions,
        "child_seat": bool(b.child_seat),
        "consultant_pref": b.consultant_pref,
        "reason_code": b.reason_code,
        # The post-drive feedback survey (2026-07-26), or null when the drive was
        # never marked completed. EMBEDDED in the row rather than served from its
        # own per-ref endpoint: the collapsed row shows an average-score badge, so
        # the list needs every survey anyway, and a separate endpoint would mean
        # one request per visible booking to render a badge. Bulk-loaded by the
        # caller — see list_bookings — so this is not an N+1.
        "feedback": survey,
        # Present so an operator can tell "never surveyed" from "surveyed and
        # ignored": the survey row itself is deleted by a rule 12 erasure, and
        # without this the booking would then look as though we never asked.
        "feedback_requested_at": _iso(b.feedback_requested_at),
        "created_at": _iso(b.created_at),
        "updated_at": _iso(b.updated_at),
    }


def list_bookings(
    db: Session,
    *,
    status: str | None = None,
    vehicle_id: object = None,
    showroom_id: object = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    """Bookings, newest first, with the console's filters.

    showroom_id and the date range filter on the SLOT (where/when the drive is),
    so those cases JOIN Slot — and the count query has to carry the same join or
    it would count the unfiltered set. vehicle_id and status live on Booking
    itself and need no join.
    """
    lim, off = _clamp_page(limit, offset)
    filters = []
    needs_slot = False

    # "CONFIRMED" MEANS CONFIRMED **OR** RESCHEDULED (2026-07-28), because the
    # console now SHOWS both as "Confirmed" — a moved booking is exactly as alive
    # as one never moved, and the operator has no decision that turns on which.
    # The filter has to agree with the badge: without this, a card reading
    # "Confirmed" vanished when you filtered for Confirmed, which is the kind of
    # disagreement that makes someone distrust the whole screen.
    #
    # The STORED status is untouched and stays `rescheduled`. It is load-bearing
    # (the partial unique index on slot_id, reschedule/cancel, which reminders
    # exist) and the dashboard funnel still counts reschedules as their own
    # stage — that is a real event and a different question from "is it live?".
    # Filtering explicitly on `rescheduled` still returns only those, so nothing
    # is lost for a caller that wants them.
    status_enum = _coerce_enum(status, BookingStatus)
    if status_enum is not None:
        if status_enum is BookingStatus.confirmed:
            filters.append(
                Booking.status.in_([BookingStatus.confirmed, BookingStatus.rescheduled])
            )
        else:
            filters.append(Booking.status == status_enum)

    if vehicle_id not in (None, ""):
        try:
            filters.append(Booking.vehicle_id == int(vehicle_id))
        except (TypeError, ValueError):
            pass

    if showroom_id not in (None, ""):
        try:
            filters.append(Slot.showroom_id == int(showroom_id))
            needs_slot = True
        except (TypeError, ValueError):
            pass

    for raw, op in ((date_from, "from"), (date_to, "to")):
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        filters.append(Slot.start >= parsed if op == "from" else Slot.start <= parsed)
        needs_slot = True

    stmt = select(Booking)
    count_stmt = select(func.count(Booking.id))
    if needs_slot:
        # Booking.slot_id is non-null, so a plain (inner) join never drops a row.
        stmt = stmt.join(Slot, Slot.id == Booking.slot_id)
        count_stmt = count_stmt.join(Slot, Slot.id == Booking.slot_id)

    stmt = stmt.options(
        selectinload(Booking.customer),
        selectinload(Booking.vehicle),
        # Nested: the showroom hangs off the SLOT, so loading the slot alone
        # would still fire one query per row to reach its branch.
        selectinload(Booking.slot).selectinload(Slot.showroom),
    )
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    rows = db.scalars(
        stmt.order_by(Booking.created_at.desc(), Booking.id.desc())
        .limit(lim)
        .offset(off)
    ).all()
    total = int(db.scalar(count_stmt) or 0)

    # One query for every survey on this page, rather than one per row. Imported
    # locally: services/feedback imports services/notifications, and console is
    # imported by the API layer at startup — a module-scope import here would pull
    # the whole notification stack into the console's import graph for one lookup.
    from app.services import feedback as feedback_service

    surveys = feedback_service.for_refs(db, [b.ref for b in rows])
    return _envelope(
        [
            _booking_row(
                b, survey=feedback_service.survey_dict(surveys.get(b.ref))
            )
            for b in rows
        ],
        total,
        lim,
        off,
    )


def _customer_row(c: Customer, *, bookings: int) -> dict:
    return {
        "customer_id": c.id,
        "title": c.title,
        "title_ar": c.title_ar,
        "name_en": c.name_en,
        "name_ar": c.name_ar,
        "mobile": c.mobile,
        "email": c.email,
        "emirate": c.emirate,
        "nationality": c.nationality,
        "language_pref": _enum(c.language_pref),
        "memory_consent": bool(c.memory_consent),
        "memory_consent_at": _iso(c.memory_consent_at),
        "whatsapp_consent": bool(c.whatsapp_consent),
        "whatsapp_consent_at": _iso(c.whatsapp_consent_at),
        "winback_flag": bool(c.winback_flag),
        # WHETHER a profile exists, never its contents. Recall is consent-gated
        # on read as well as write (services/memory.py), and a console that
        # printed `notes` would be a second reader that forgot the gate. The
        # boolean is what an operator needs to answer "why did Aisha remember
        # nothing about this person?" — the answer is consent, and it is right
        # there next to it.
        "has_memory_profile": c.memory_profile is not None,
        "bookings": bookings,
        "created_at": _iso(c.created_at),
    }


def list_customers(
    db: Session, *, limit: object = None, offset: object = None
) -> dict:
    lim, off = _clamp_page(limit, offset)
    stmt = (
        select(Customer)
        .options(selectinload(Customer.memory_profile))
        .order_by(Customer.id)
        .limit(lim)
        .offset(off)
    )
    rows = db.scalars(stmt).all()

    # Booking counts for this page only, in one grouped query rather than one
    # per customer (or a relationship load that pulls every booking row).
    ids = [c.id for c in rows]
    counts: dict[int, int] = {}
    if ids:
        counts = {
            cid: n
            for cid, n in db.execute(
                select(Booking.customer_id, func.count(Booking.id))
                .where(Booking.customer_id.in_(ids))
                .group_by(Booking.customer_id)
            ).all()
        }

    return _envelope(
        [_customer_row(c, bookings=counts.get(c.id, 0)) for c in rows],
        _count(db, Customer.id, []),
        lim,
        off,
    )


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
def list_conversations(
    db: Session, *, limit: object = None, offset: object = None
) -> dict:
    """One row per session_id, newest activity first.

    Aggregated from conversation_turns because there is no Conversation table —
    session_id is a bare string shared by the checkpointer, the turns, the audit
    rows and the slot lock. See the module docstring for why closed-status is
    resolved per PAGE rather than per row.
    """
    lim, off = _clamp_page(limit, offset)

    grouped: Select = (
        select(
            ConversationTurn.session_id,
            func.count(ConversationTurn.id).label("turns"),
            func.min(ConversationTurn.ts).label("first_ts"),
            func.max(ConversationTurn.ts).label("last_ts"),
        )
        .group_by(ConversationTurn.session_id)
        .order_by(func.max(ConversationTurn.ts).desc())
        .limit(lim)
        .offset(off)
    )
    rows = db.execute(grouped).all()

    total = int(
        db.scalar(
            select(func.count(func.distinct(ConversationTurn.session_id)))
        )
        or 0
    )

    sessions = [r.session_id for r in rows]
    closed: set[str] = set()
    langs: dict[str, set[str]] = {}
    voice_counts: dict[str, int] = {}
    if sessions:
        # Close markers for this page, one query. Scanned in Python for the same
        # reason conversation.is_closed does: querying INTO the JSON payload
        # would fork on SQLite-vs-Postgres dialect, and DATABASE_URL swaps
        # between them.
        for ev in db.scalars(
            select(AuditEvent).where(
                AuditEvent.session.in_(sessions),
                AuditEvent.type == AuditType.intent,
            )
        ).all():
            if (ev.payload or {}).get("action") == conversation.CLOSE_ACTION:
                closed.add(ev.session)

        for sid, lang, n in db.execute(
            select(
                ConversationTurn.session_id,
                ConversationTurn.lang,
                func.count(ConversationTurn.id),
            )
            .where(ConversationTurn.session_id.in_(sessions))
            .group_by(ConversationTurn.session_id, ConversationTurn.lang)
        ).all():
            langs.setdefault(sid, set()).add(_enum(lang))

        for sid, n in db.execute(
            select(ConversationTurn.session_id, func.count(ConversationTurn.id))
            .where(
                ConversationTurn.session_id.in_(sessions),
                ConversationTurn.audio_id.is_not(None),
            )
            .group_by(ConversationTurn.session_id)
        ).all():
            voice_counts[sid] = int(n)

    return _envelope(
        [
            {
                "session_id": r.session_id,
                "turns": int(r.turns or 0),
                "first_ts": _iso(r.first_ts),
                "last_ts": _iso(r.last_ts),
                "langs": sorted(langs.get(r.session_id, set())),
                "voice_turns": voice_counts.get(r.session_id, 0),
                "status": "closed" if r.session_id in closed else "active",
            }
            for r in rows
        ],
        total,
        lim,
        off,
    )


# --------------------------------------------------------------------------- #
# Audit  (contract + browser)
# --------------------------------------------------------------------------- #
def _audit_row(e: AuditEvent) -> dict:
    return {
        "id": e.id,
        "ts": _iso(e.ts),
        "session": e.session,
        "actor": _enum(e.actor),
        "type": _enum(e.type),
        "payload": e.payload or {},
    }


def audit_for_session(db: Session, *, session_id: str) -> list[dict]:
    """The CONTRACTED per-conversation trail. Returns a BARE LIST, per contract.

    Unpaginated on purpose: the contract defines it as the audit drawer for ONE
    conversation, and a conversation's trail is tens of rows. Adding an envelope
    here would change a signed shape for no benefit.
    """
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.session == session_id)
        .order_by(AuditEvent.ts, AuditEvent.id)
    ).all()
    return [_audit_row(e) for e in rows]


def audit_log(
    db: Session,
    *,
    session: str | None = None,
    actor: str | None = None,
    type: str | None = None,
    limit: object = None,
    offset: object = None,
) -> dict:
    """Cross-session audit browser — the console's own view, newest first.

    Separate from `audit_for_session` rather than a flag on it, so the signed
    contract shape stays untouched by anything the browser wants later.
    """
    lim, off = _clamp_page(limit, offset)
    filters = []
    if session:
        filters.append(AuditEvent.session == session)
    actor_enum = _coerce_enum(actor, Actor)
    if actor_enum is not None:
        filters.append(AuditEvent.actor == actor_enum)
    type_enum = _coerce_enum(type, AuditType)
    if type_enum is not None:
        filters.append(AuditEvent.type == type_enum)

    stmt = select(AuditEvent)
    if filters:
        stmt = stmt.where(*filters)
    rows = db.scalars(
        stmt.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .limit(lim)
        .offset(off)
    ).all()
    return _envelope(
        [_audit_row(e) for e in rows], _count(db, AuditEvent.id, filters), lim, off
    )


# --------------------------------------------------------------------------- #
# Notifications  (contract)
# --------------------------------------------------------------------------- #
def notifications(
    db: Session, *, status: str | None = None, kind: str | None = None
) -> list[dict]:
    """The WhatsApp-simulator feed. BARE LIST, per the contract.

    `body` is published as a string and is legitimately "" until the template
    phase renders it — an honest empty, not a placeholder. The console must show
    it as empty rather than substituting English prose, which would be display
    text invented at read time (LOC-01).
    """
    filters = []
    status_enum = _coerce_enum(status, NotificationStatus)
    if status_enum is not None:
        filters.append(Notification.status == status_enum)
    kind_enum = _coerce_enum(kind, NotificationKind)
    if kind_enum is not None:
        filters.append(Notification.kind == kind_enum)

    stmt = select(Notification)
    if filters:
        stmt = stmt.where(*filters)
    rows = db.scalars(stmt.order_by(Notification.send_at)).all()
    return [
        {
            "id": n.id,
            "ref": n.ref,
            "kind": _enum(n.kind),
            "lang": _enum(n.lang),
            "status": _enum(n.status),
            "send_at": _iso(n.send_at),
            "body": n.body or "",
            "payload": n.payload or {},
        }
        for n in rows
    ]


# --------------------------------------------------------------------------- #
# Metrics  (contract)
# --------------------------------------------------------------------------- #
def metrics(db: Session) -> dict:
    """Live dashboard counters. REAL COUNTS ONLY (rule 16).

    Every number here is a query against actual session activity — there is no
    seeded or illustrative figure, and a fresh database correctly reports zeros
    rather than a plausible-looking demo shape. A dashboard that looks busy on
    an empty DB is precisely the fake data rule 16 forbids.
    """
    conversations = int(
        db.scalar(select(func.count(func.distinct(ConversationTurn.session_id)))) or 0
    )

    turns_by_lang = {lang.value: 0 for lang in Language}
    for lang, n in db.execute(
        select(ConversationTurn.lang, func.count(ConversationTurn.id)).group_by(
            ConversationTurn.lang
        )
    ).all():
        key = _enum(lang)
        if key is not None:
            turns_by_lang[key] = int(n)

    bookings = {status.value: 0 for status in BookingStatus}
    for status, n in db.execute(
        select(Booking.status, func.count(Booking.id)).group_by(Booking.status)
    ).all():
        key = _enum(status)
        if key is not None:
            bookings[key] = int(n)

    notifications_sent = int(
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.status == NotificationStatus.sent
            )
        )
        or 0
    )

    return {
        "conversations": conversations,
        "turns_by_lang": turns_by_lang,
        "bookings": bookings,
        "notifications_sent": notifications_sent,
        "agent_registry": [dict(entry) for entry in AGENT_REGISTRY],
    }


# --------------------------------------------------------------------------- #
# Dashboard (2026-07-23)
# --------------------------------------------------------------------------- #
# A SECOND METRICS FUNCTION, NOT A WIDER `metrics()`. The shape `metrics()`
# returns is signed in CLAUDE.md's API contract and its consumer is written
# against it; growing it with a window parameter and six new blocks would edit a
# contracted response as a side effect of building a screen. So `metrics()` is
# frozen and this is its own endpoint, exactly as `audit_for_session` (contract)
# and `audit_log` (browser) are two functions over one table.
#
# EVERY FIGURE IS A QUERY (rule 16). Nothing here is seeded, illustrative or
# carried over from the mockup this was built from: a fresh database reports
# zeros, and a metric with no data behind it is ABSENT from the payload rather
# than defaulted to a plausible number — see `handoffs` below, which is the one
# place that distinction has teeth today.

# Rolling windows, in days. `None` is "all time" — no lower bound at all, and
# the only value with no previous period to compare against.
DASHBOARD_WINDOWS: dict[str, int | None] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}
DEFAULT_WINDOW = "7d"

# Statuses that count as a booking the agent WON and that still stands. Cancelled
# is deliberately outside this set: the tile answers "how many drives are on the
# books", so a cancelled booking leaves the count rather than sitting inside it,
# and appears as its own funnel stage.
_LIVE_BOOKING_STATUSES = (BookingStatus.confirmed, BookingStatus.rescheduled)

# How many rows each ranked list carries. Zero-count rows never appear — a car
# nobody booked is absent, not a bar of length zero, because the panel answers
# "what is winning".
_LEADERBOARD_ROWS = 5


def _window_bounds(window: str) -> tuple[str, datetime | None, datetime | None]:
    """Resolve a window key to (key, current_start, previous_start).

    Both bounds are computed ONCE, here, and threaded through every query — so
    the tiles, the funnel and the leaderboards on one screen can never disagree
    about where the period began because two of them called `utcnow()` a
    microsecond apart.

    `previous_start` is the same length again, immediately before, and is None
    for all-time (nothing precedes everything). The current window is therefore
    [current_start, now) and the previous one [previous_start, current_start).
    """
    key = window if window in DASHBOARD_WINDOWS else DEFAULT_WINDOW
    days = DASHBOARD_WINDOWS[key]
    if days is None:
        return key, None, None
    now = datetime.utcnow()
    start = now - timedelta(days=days)
    return key, start, start - timedelta(days=days)


def _enquiries(db: Session, start: datetime | None, end: datetime | None) -> int:
    """Distinct sessions with at least one CUSTOMER turn in the window.

    One conversation is one enquiry — the same unit the Conversations tab counts
    and the same unit the funnel's first stage uses, so "enquiries -> booked"
    reads as a conversion rate rather than as two unrelated numbers. Counting
    customer TURNS instead would grow with chattiness rather than with demand.

    Agent turns are excluded from the test deliberately: a session where Aisha
    spoke and nobody answered is not an enquiry anybody made.
    """
    q = select(func.count(func.distinct(ConversationTurn.session_id))).where(
        ConversationTurn.speaker == Speaker.customer
    )
    if start is not None:
        q = q.where(ConversationTurn.ts >= start)
    if end is not None:
        q = q.where(ConversationTurn.ts < end)
    return int(db.scalar(q) or 0)


def _bookings_in(
    db: Session,
    start: datetime | None,
    end: datetime | None,
    statuses: tuple[BookingStatus, ...] | None = None,
) -> int:
    """Count bookings CREATED in the window.

    created_at, not the slot time: the tile claims demand the agent captured in
    the period, and a drive booked today for next month was still won today.
    (The funnel's outcome stages read attendance, which is about the drive — but
    they are scoped by the same created_at window so every stage describes the
    same set of bookings.)
    """
    q = select(func.count(Booking.id))
    if statuses is not None:
        q = q.where(Booking.status.in_(statuses))
    if start is not None:
        q = q.where(Booking.created_at >= start)
    if end is not None:
        q = q.where(Booking.created_at < end)
    return int(db.scalar(q) or 0)


def _attendance_in(
    db: Session,
    start: datetime | None,
    end: datetime | None,
    value: BookingAttendance,
) -> int:
    """Bookings created in the window whose drive has this outcome.

    Cancelled bookings are excluded: attendance does not apply to a drive that
    was called off (the service refuses to set it), so counting their `pending`
    rows would put drives nobody was ever expected at into the denominator.
    """
    q = select(func.count(Booking.id)).where(
        Booking.attendance == value,
        Booking.status.in_(_LIVE_BOOKING_STATUSES),
    )
    if start is not None:
        q = q.where(Booking.created_at >= start)
    if end is not None:
        q = q.where(Booking.created_at < end)
    return int(db.scalar(q) or 0)


def _escalations(
    db: Session, start: datetime | None, end: datetime | None
) -> tuple[int, list[dict]]:
    """Human-callback escalations in the window (rule 13, FR-ES).

    Returns (escalated_sessions, reasons) where:
    - escalated_sessions is the number of DISTINCT conversations that were handed
      to a human — the same session unit `_enquiries` counts, so "handoffs as a
      percent of enquiries" is an honest ratio rather than two unrelated numbers.
      A conversation escalated twice is still one enquiry handed over.
    - reasons is a breakdown [{reason, count}, ...] ordered by count desc, where
      count is the number of distinct SESSIONS whose escalation(s) included that
      reason. A session handed over for two different reasons appears under both,
      so the reason counts may sum to more than escalated_sessions — each is a
      true "this many conversations were escalated for this reason".

    Read off the append-only audit trail (AuditType.escalation), which is where
    services/escalation logs each handoff. Grouping is done in Python so the JSON
    payload's `reason` is read the same way on SQLite (dev) and Postgres.
    """
    q = select(AuditEvent).where(AuditEvent.type == AuditType.escalation)
    if start is not None:
        q = q.where(AuditEvent.ts >= start)
    if end is not None:
        q = q.where(AuditEvent.ts < end)

    session_reasons: dict[str, set[str]] = {}
    for event in db.scalars(q):
        reason = (event.payload or {}).get("reason") or "other"
        session_reasons.setdefault(event.session, set()).add(reason)

    reason_counts: dict[str, int] = {}
    for reasons in session_reasons.values():
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    reasons_list = [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            reason_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]
    return len(session_reasons), reasons_list


def _pct_change(current: int, previous: int) -> float | None:
    """Period-over-period change, or None when there is nothing to compare.

    None on BOTH a missing previous window (all-time) and an empty one: "up
    infinity percent" from a base of zero is not a fact, and rendering it as
    +100% would understate a first booking as badly as +∞ overstates it. The UI
    says "no prior activity" rather than printing a number for either.
    """
    if previous <= 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


def _top_vehicles(db: Session, start: datetime | None) -> list[dict]:
    """Most-booked models in the window, best first.

    Live bookings only and ranked by count, so the list answers "what is
    winning". A model with no bookings does not appear at all.
    """
    q = (
        select(VehicleModel.id, VehicleModel.name, func.count(Booking.id).label("n"))
        .join(Booking, Booking.vehicle_id == VehicleModel.id)
        .where(Booking.status.in_(_LIVE_BOOKING_STATUSES))
        .group_by(VehicleModel.id, VehicleModel.name)
        .order_by(func.count(Booking.id).desc(), VehicleModel.name)
        .limit(_LEADERBOARD_ROWS)
    )
    if start is not None:
        q = q.where(Booking.created_at >= start)
    return [
        {"vehicle_id": int(vid), "name": name, "bookings": int(n)}
        for vid, name, n in db.execute(q).all()
    ]


def _top_places(db: Session, start: datetime | None) -> list[dict]:
    """Most-booked BRANCHES AND DOORSTEP AREAS, best first.

    Doorstep visits rank as their own rows rather than being dropped. They are a
    headline feature (FR-SL-07) and excluding them would quietly make the panel
    describe a subset of the bookings the tile above it counted — the sort of
    silent narrowing that reads as a bug in the total.

    Both live on the SLOT, not the booking: where a drive happens is a property
    of the slot that was locked, which is why this joins through it.
    """
    rows: list[dict] = []

    branch = (
        select(
            Showroom.id,
            Showroom.name,
            Showroom.city,
            Showroom.emirate,
            func.count(Booking.id).label("n"),
        )
        .join(Slot, Slot.id == Booking.slot_id)
        .join(Showroom, Showroom.id == Slot.showroom_id)
        .where(Booking.status.in_(_LIVE_BOOKING_STATUSES))
        .group_by(Showroom.id, Showroom.name, Showroom.city, Showroom.emirate)
    )
    doorstep = (
        select(Slot.service_area, func.count(Booking.id).label("n"))
        .join(Booking, Booking.slot_id == Slot.id)
        .where(
            Booking.status.in_(_LIVE_BOOKING_STATUSES),
            Slot.location_type == LocationType.doorstep,
        )
        .group_by(Slot.service_area)
    )
    if start is not None:
        branch = branch.where(Booking.created_at >= start)
        doorstep = doorstep.where(Booking.created_at >= start)

    for sid, name, city, emirate, n in db.execute(branch).all():
        rows.append(
            {
                "showroom_id": int(sid),
                "name": name,
                "city": city,
                "emirate": emirate,
                "location_type": LocationType.showroom.value,
                "bookings": int(n),
            }
        )
    for area, n in db.execute(doorstep).all():
        rows.append(
            {
                "showroom_id": None,
                # The service area, raw — the console labels it, because a
                # stored "Doorstep (Dubai)" would be display text in a data
                # field (the rule this schema is built on).
                "name": area,
                "city": None,
                "emirate": None,
                "location_type": LocationType.doorstep.value,
                "bookings": int(n),
            }
        )

    rows.sort(key=lambda r: (-r["bookings"], r["name"] or ""))
    return rows[:_LEADERBOARD_ROWS]


def _map_pins(db: Session, start: datetime | None) -> list[dict]:
    """Every branch that can be placed on a map, with its bookings in the window.

    A NULL coordinate DROPS THE BRANCH rather than defaulting to (0, 0), which
    is a spot in the Atlantic — a pin in the wrong place is a false claim about
    where Elite is (FR-CX-07), and a missing pin is merely incomplete.

    Doorstep bookings have no branch and therefore no pin; they still rank in
    the list beside the map, which is why the two can legitimately differ.
    """
    counts = (
        select(Slot.showroom_id, func.count(Booking.id))
        .join(Booking, Booking.slot_id == Slot.id)
        .where(
            Booking.status.in_(_LIVE_BOOKING_STATUSES),
            Slot.showroom_id.is_not(None),
        )
        .group_by(Slot.showroom_id)
    )
    if start is not None:
        counts = counts.where(Booking.created_at >= start)
    by_showroom = {int(sid): int(n) for sid, n in db.execute(counts).all()}

    pins: list[dict] = []
    for s in db.scalars(
        select(Showroom)
        .where(Showroom.latitude.is_not(None), Showroom.longitude.is_not(None))
        .order_by(Showroom.id)
    ).all():
        pins.append(
            {
                "showroom_id": s.id,
                "name": s.name,
                "lat": float(s.latitude),
                "lng": float(s.longitude),
                "bookings": by_showroom.get(s.id, 0),
            }
        )
    return pins


def dashboard(db: Session, window: str = DEFAULT_WINDOW) -> dict:
    """The console dashboard, scoped to a rolling window.

    `handoffs` is now MEASURED (rule 13, FR-ES). Since request_human_callback
    logs an `AuditType.escalation` row per handoff, this block counts real data:
    `count` is the number of enquiries handed to a human in the window,
    `resolved_without_human` is the rest, and `reasons` is the live breakdown.
    (`AuditType.handoff` is a different thing — the WhatsApp deep-link bridge —
    and is deliberately not counted here.) When no escalation has been logged the
    block is a true zero rather than a hardcoded one: 0 handoffs, 100% resolved
    without a human, and an empty `reasons` list, all derived, none invented.
    """
    key, start, prev_start = _window_bounds(window)

    enquiries = _enquiries(db, start, None)
    booked = _bookings_in(db, start, None, _LIVE_BOOKING_STATUSES)
    # The previous window is [prev_start, start) — bounded on BOTH sides, or the
    # comparison would be "the last 30 days versus all of history".
    booked_previous = (
        _bookings_in(db, prev_start, start, _LIVE_BOOKING_STATUSES)
        if prev_start is not None
        else 0
    )

    escalated, escalation_reasons = _escalations(db, start, None)

    completed = _attendance_in(db, start, None, BookingAttendance.completed)
    noshow = _attendance_in(db, start, None, BookingAttendance.noshow)
    cancelled = _bookings_in(db, start, None, (BookingStatus.cancelled,))
    rescheduled = _bookings_in(db, start, None, (BookingStatus.rescheduled,))

    # Of drives with an OUTCOME, not of all bookings: a booking nobody has
    # marked yet is not evidence either way, and letting unmarked rows dilute
    # the rate would make it drift with an operator's paperwork rather than
    # with customer behaviour.
    decided = completed + noshow
    noshow_rate = round((noshow / decided) * 100, 1) if decided else None

    noshow_rate_previous = None
    if prev_start is not None:
        prev_completed = _attendance_in(
            db, prev_start, start, BookingAttendance.completed
        )
        prev_noshow = _attendance_in(db, prev_start, start, BookingAttendance.noshow)
        prev_decided = prev_completed + prev_noshow
        if prev_decided:
            noshow_rate_previous = round((prev_noshow / prev_decided) * 100, 1)

    return {
        "window": key,
        "test_drives": {
            "booked": booked,
            "previous": booked_previous,
            "change_pct": _pct_change(booked, booked_previous),
        },
        "enquiries": {
            "handled": enquiries,
            # Bookings per enquiry. None rather than 0 on an empty window: "0%
            # of nothing converted" is a claim about a period in which nobody
            # asked anything.
            "converted": booked,
            "conversion_pct": (
                round((booked / enquiries) * 100, 1) if enquiries else None
            ),
        },
        "handoffs": {
            "count": escalated,
            # Handoffs as a percent of enquiries. Clamped at the session level so
            # it can never exceed 100 (escalated is distinct sessions, a subset of
            # enquiries). None on an empty window: a percent of no enquiries is
            # not a fact.
            "pct": round((escalated / enquiries) * 100, 1) if enquiries else 0.0,
            # The enquiries NOT handed to a human. max(...,0) is defensive: an
            # escalation logged for a session with no customer turn (so not an
            # enquiry) must not push this negative.
            "resolved_without_human": max(enquiries - escalated, 0),
            "resolved_pct": (
                round((max(enquiries - escalated, 0) / enquiries) * 100, 1)
                if enquiries
                else None
            ),
            "reasons": escalation_reasons,
        },
        "funnel": {
            "enquiries": enquiries,
            "booked": booked,
            "completed": completed,
            "rescheduled": rescheduled,
            "cancelled": cancelled,
            "noshow": noshow,
            "noshow_rate_pct": noshow_rate,
            "noshow_rate_previous_pct": noshow_rate_previous,
        },
        "top_vehicles": _top_vehicles(db, start),
        "top_places": _top_places(db, start),
        "map_pins": _map_pins(db, start),
    }


__all__ = [
    "AGENT_REGISTRY",
    "DASHBOARD_WINDOWS",
    "DEFAULT_WINDOW",
    "DEFAULT_PAGE",
    "dashboard",
    "MAX_PAGE",
    "audit_for_session",
    "audit_log",
    "list_bookings",
    "list_conversations",
    "list_customers",
    "list_showrooms",
    "list_slots",
    "list_vehicles",
    "metrics",
    "notifications",
]
