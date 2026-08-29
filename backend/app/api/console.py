"""Admin console — thin read routes over services/console.py (AD-I).

Three of these are the endpoints the CLAUDE.md API contract has specified since
the console was scoped, and which nothing implemented until now:

    GET /api/console/audit?session_id=...   per-conversation trail (BARE LIST)
    GET /api/console/metrics                live counters, real data only
    GET /api/console/notifications          WhatsApp simulator feed (BARE LIST)

The rest are the data browser added alongside them (2026-07-21), and they return
a paginated envelope `{rows, total, limit, offset}` rather than a bare list. That
asymmetry is deliberate and is explained in services/console.py: the contracted
shapes are signed and are not ours to reshape as a side effect.

READ-ONLY EXCEPT FOR ONE ROUTE. Every GET here writes nothing and commits
nothing — which is why the `get_db` convention (services flush, the caller owns
the transaction) did not bite for most of this router's life. The exception,
added 2026-07-23, is POST /bookings/{ref}/attendance at the bottom: it commits,
and like any future console write it goes through the existing service (write
guards + audit choke point intact) rather than touching the DB itself.

⚠️ UNGATED, BY EXPLICIT DECISION (2026-07-21), AND FLAGGED FOR THE D1 PDPL
CHECKLIST. There is no auth anywhere in this backend and this router adds none,
so on the demo network every QR attendee can reach these paths. Two of them —
/customers and /bookings — return names and +971 mobiles for EVERY customer at
once. That is strictly heavier than the ref-alone booking lookup already on the
checklist, which at least gates PII on the viewing session precisely so that a
guessable reference cannot put a stranger's phone number on screen in front of
an audience. Do not read the absence of a gate here as a judgement that none is
needed. The router is deliberately a single object so that one
`dependencies=[Depends(...)]` closes all of it at once.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Actor
from app.services import booking as booking_service
from app.services import console

router = APIRouter(prefix="/api/console")


# --------------------------------------------------------------------------- #
# Contracted endpoints — shapes are signed; do not reshape
# --------------------------------------------------------------------------- #
@router.get("/audit")
def get_audit(
    session_id: str = Query(..., description="The conversation to trace"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """One conversation's audit trail, oldest first (FR-AD-02, rule 15).

    An unknown session returns [] rather than 404, matching the conversation GET
    rather than the vehicle GET: a session with no events is an ordinary state
    (nothing has happened yet), not a client bug.
    """
    return console.audit_for_session(db, session_id=session_id)


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)) -> dict:
    """Live dashboard counters — every number a real query (rule 16)."""
    return console.metrics(db)


@router.get("/dashboard")
def get_dashboard(
    window: str = Query(
        console.DEFAULT_WINDOW,
        description="Rolling window: 7d | 30d | 90d | all",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """The operator dashboard, scoped to a rolling window (2026-07-23).

    A SEPARATE ENDPOINT FROM /metrics, WHICH IS UNTOUCHED. That response shape is
    signed in the CLAUDE.md API contract; widening it with a window parameter and
    six new blocks would edit a contracted response as a side effect of building
    a screen, so this is its own route and `metrics` still answers exactly what
    it always did.

    AN UNKNOWN `window` FALLS BACK to the default rather than 422-ing, matching
    `_coerce_enum`'s reasoning for the browser filters: a typo in a URL should
    degrade to a normal dashboard, never to an error screen mid-demo. The window
    actually applied comes back in the payload, so the UI reflects what it got
    rather than what it asked for.
    """
    return console.dashboard(db, window=window)


@router.get("/notifications")
def get_notifications(
    status: str | None = Query(None),
    kind: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The WhatsApp simulator feed, in send order.

    The optional filters are additive to the contracted shape — the response is
    still the same bare list, just narrowed.
    """
    return console.notifications(db, status=status, kind=kind)


# --------------------------------------------------------------------------- #
# Data browser — paginated envelopes
# --------------------------------------------------------------------------- #
@router.get("/vehicles")
def get_vehicles(
    brand: str | None = Query(None),
    fuel: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    return console.list_vehicles(
        db, brand=brand, fuel=fuel, limit=limit, offset=offset
    )


@router.get("/showrooms")
def get_showrooms(
    emirate: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    return console.list_showrooms(db, emirate=emirate, limit=limit, offset=offset)


@router.get("/slots")
def get_slots(
    status: str | None = Query(None),
    vehicle_id: int | None = Query(None),
    showroom_id: int | None = Query(None),
    location_type: str | None = Query(None),
    date_from: str | None = Query(None, description="ISO date or datetime"),
    date_to: str | None = Query(None, description="ISO date or datetime"),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    return console.list_slots(
        db,
        status=status,
        vehicle_id=vehicle_id,
        showroom_id=showroom_id,
        location_type=location_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/bookings")
def get_bookings(
    status: str | None = Query(None),
    vehicle_id: str | None = Query(None),
    showroom_id: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """Bookings incl. customer name and mobile — see the PII warning above.

    Filters: status, vehicle_id, showroom_id (the drive's branch), and a
    date_from/date_to range over the drive's slot time.
    """
    return console.list_bookings(
        db,
        status=status,
        vehicle_id=vehicle_id,
        showroom_id=showroom_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/customers")
def get_customers(
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """Customers incl. +971 mobiles — see the PII warning above."""
    return console.list_customers(db, limit=limit, offset=offset)


@router.get("/conversations")
def get_conversations(
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """One row per session, newest activity first."""
    return console.list_conversations(db, limit=limit, offset=offset)


@router.get("/handoffs")
def get_handoffs(
    reason: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """The human-callback queue, OLDEST FIRST (T8.1).

    Deliberately the opposite order to every other browser here. Newest-first
    suits an audit log, which is read to find out what just happened. This is a
    WORK QUEUE: the person waiting longest is the one being let down most, and
    sorting them to the bottom is how a queue starves its oldest entry.
    """
    return console.list_handoffs(db, reason=reason, limit=limit, offset=offset)


@router.get("/routing")
def get_routing(
    session: str | None = Query(None),
    routed_to: str | None = Query(None),
    only_multi_agent: bool = Query(False),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """Per-turn routing detail, newest first (T8.2).

    Every field is a READ of something turn_trace has been recording all along
    and nothing displayed.
    """
    return console.list_routing(
        db, session=session, routed_to=routed_to,
        only_multi_agent=only_multi_agent, limit=limit, offset=offset,
    )


@router.get("/routing/summary")
def get_routing_summary(db: Session = Depends(get_db)) -> dict:
    """How the cascade behaves in aggregate.

    `fast_lane_share` is the number this exists for: the calibration predicted
    the embedding shortcut settles about a third of briefs, on 32 SYNTHETIC
    utterances. This says what it settles on real ones — and if it is far lower,
    the honest move is to delete the fast lane and always ask the judge.
    """
    return console.routing_summary(db)


@router.get("/audit-log")
def get_audit_log(
    session: str | None = Query(None),
    actor: str | None = Query(None),
    type: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """Cross-session audit browser, newest first.

    Distinct from GET /audit (which is the contracted per-conversation drawer
    and returns a bare list) so that the signed shape stays untouched.
    """
    return console.audit_log(
        db, session=session, actor=actor, type=type, limit=limit, offset=offset
    )


# --------------------------------------------------------------------------- #
# The console's ONLY write (2026-07-23)
# --------------------------------------------------------------------------- #
class AttendanceBody(BaseModel):
    """pending | completed | noshow. Validated in the SERVICE, not here — an
    unknown value comes back as `{"error": "invalid_attendance"}` rather than a
    422, so the console recovers on the same envelope as every other failure."""

    attendance: str


@router.post("/bookings/{ref}/attendance")
def set_booking_attendance(
    ref: str, body: AttendanceBody, db: Session = Depends(get_db)
) -> dict:
    """Mark a booking pending / completed / noshow — the DIRECT path, no LLM.

    THE FIRST WRITE IN THIS ROUTER, and it changes the PII warning above from
    "anyone can read this" to "anyone can change this". It is still ungated, for
    the same reason and with the same D1 checklist entry — but note that a
    no-show now SENDS A WHATSAPP MESSAGE to a real customer, so this is the one
    path here where an unauthenticated caller can cause outbound contact. The
    single `dependencies=[Depends(...)]` that closes the router closes this too.

    A thin wrapper like every other route (AD-I): every rule — cancelled
    bookings refused, idempotency, once-only notification, the audit row — lives
    in booking.set_attendance and is inherited by any future caller.

    Returns the service's envelope verbatim, failures included, so the UI
    branches on `error` exactly as the agent path does.
    """
    correlation_id = uuid4().hex
    result = booking_service.set_attendance(
        db,
        # No conversation is involved — an operator is acting on a booking, not
        # inside a chat. Same synthetic-session convention as the dispatcher's
        # `notify:{ref}`, so the trail still groups by the thing acted upon.
        session_id=f"console:{ref}",
        ref=ref,
        attendance=body.attendance,
        actor=Actor.admin,
        correlation_id=correlation_id,
    )
    # The service flushes but never commits (audit.record's contract): the audit
    # row and the notification must live or die with the change they describe.
    if "error" not in result:
        db.commit()
    return result
