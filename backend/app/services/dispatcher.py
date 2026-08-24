"""Notification dispatcher (AD-F) — reads Notification.send_at and sends due rows.

THE SINGLE SEND PATH for both messages the user asked for:
  - the CONFIRMATION (send_at = now → goes out on the next tick, ~instant), and
  - the T-2h REMINDER (send_at = slot.start - 2h → goes out at that wall-clock).
Both are ordinary `scheduled` rows written by services/notifications; this module
is the "later phase" that module's header always pointed at ("the accelerated
demo clock will read send_at").

CONSENT IS CHECKED HERE, AT SEND TIME — not when the row was scheduled. A booking
schedules its rows before the customer's WhatsApp consent is necessarily final,
and consent can also be withdrawn between scheduling and the 2-hour reminder, so
the gate belongs at the moment of sending. No consent → the row is retired
(`cancelled`) and audited, never sent.

RENDER AT DISPATCH. The row is stored language-neutral (`body=""`); the
customer-facing text is composed now via notifications.render_body and persisted
onto the row so the console feed shows exactly what went out.

BOUNDED RETRY, NO NEW STATUS. A transient failure (closed 24h window, network)
leaves the row `scheduled` with an incremented `payload.attempts`, so the next
tick retries; after NOTIFY_MAX_ATTEMPTS it is retired. This avoids adding a
`failed` value to NotificationStatus (which is part of the API contract in both
repos) — the failure reason lives in the payload and the audit trail instead.

Runs from the in-process APScheduler started in app.main. Never raises past the
tick wrapper: one bad row must not stop the others, and one bad tick must not
stop the scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import SessionLocal
from app.models import (
    Actor,
    AuditType,
    Booking,
    Customer,
    Notification,
    NotificationKind,
    NotificationStatus,
)
from app.services import audit, feedback, notifications, whatsapp

log = logging.getLogger("app.services.dispatcher")


def _customer_for_ref(db: Session, ref: str) -> Customer | None:
    """The customer a notification is for, via its booking ref. One booking per
    ref, so this is unambiguous; None if the booking or customer is gone."""
    booking = db.scalar(select(Booking).where(Booking.ref == ref))
    return booking.customer if booking is not None else None


def _audit_notification(
    db: Session,
    row: Notification,
    status: NotificationStatus,
    *,
    correlation_id: str,
    extra: dict | None = None,
) -> None:
    audit.record(
        db,
        session=f"notify:{row.ref}",
        actor=Actor.system,
        type=AuditType.notification,
        payload={
            "ref": row.ref,
            "kind": row.kind.value,
            "lang": row.lang.value,
            "status": status.value,
            "at": datetime.now().isoformat(),
            **(extra or {}),
        },
        correlation_id=correlation_id,
    )


def dispatch_due(db: Session, *, now: datetime | None = None, limit: int = 50) -> dict:
    """Send every `scheduled` notification whose send_at has arrived. Commits."""
    now = now or datetime.now()

    due = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.scheduled,
                Notification.send_at <= now,
            )
            .order_by(Notification.send_at)
            .limit(limit)
        ).all()
    )

    sent = skipped = retried = retired = 0

    def _retire(row: Notification, reason: str, correlation_id: str) -> None:
        """Cancel a row that cannot be sent, and close the survey behind it.

        The survey half matters more than it looks: a feedback survey left at
        `pending` blocks promotion, so a single undeliverable opening would
        silently stop that customer ever being surveyed again — with nothing on
        screen to explain it. Every retirement path routes through here so none
        can forget.
        """
        row.status = NotificationStatus.cancelled
        row.payload = {**(row.payload or {}), "not_sent_reason": reason}
        _audit_notification(
            db, row, NotificationStatus.cancelled,
            correlation_id=correlation_id, extra={"reason": reason},
        )
        if row.kind == NotificationKind.feedback:
            feedback.abort_opening(db, ref=row.ref, reason=reason, now=now)

    for row in due:
        correlation_id = uuid4().hex

        # ---- Kind gate (send only the kinds enabled right now) --------------- #
        # t24 is scheduled but not sent yet (config.NOTIFY_SEND_KINDS). Retire it
        # rather than leaving it `scheduled`, or it would come due on every tick.
        if row.kind.value not in config.NOTIFY_SEND_KINDS:
            _retire(row, "kind_not_enabled", correlation_id)
            skipped += 1
            continue

        # A brochure has no booking to route through, so the recipient is
        # named on the row itself (C3). Booking kinds are unchanged: `ref`
        # first, and customer_id is simply None on them.
        customer = (
            db.get(Customer, row.customer_id)
            if row.customer_id is not None
            else _customer_for_ref(db, row.ref)
        )

        # ---- Consent gate (only message a customer who said yes) ------------- #
        if customer is None or not customer.whatsapp_consent:
            _retire(row, "no_consent", correlation_id)
            skipped += 1
            continue

        # ---- Render + send --------------------------------------------------- #
        # The feedback survey's OPENING is the one kind rendered outside
        # notifications.render_body. That function is pure over the row's
        # payload, and the opening needs the customer's NAME — which would
        # otherwise have to be written into the payload, putting personal data
        # into a table that does not need it (the same minimisation reasoning
        # that keeps doorstep_address out of notification payloads). We already
        # hold the Customer here, so the survey composes from live objects.
        if row.kind == NotificationKind.feedback:
            survey = feedback.for_ref(db, row.ref)
            if survey is None:
                # The survey was erased (rule 12) or the row outlived it. Retire
                # rather than send: an opening with no survey behind it would ask
                # a question no reply could ever be filed against.
                _retire(row, "survey_missing", correlation_id)
                skipped += 1
                continue
            body = feedback.render_opening_for_dispatch(survey, now=now)
        else:
            body = notifications.render_body(row)
        row.body = body  # persist what was (or would have been) sent
        result = whatsapp.send_text(customer.mobile, body)

        if result.get("ok") or result.get("skipped"):
            # A real delivery OR simulator mode (WhatsApp disabled): the row is
            # `sent` either way — simulator keeps the console/metrics behaviour
            # identical to before real sending existed.
            simulated = bool(result.get("skipped"))
            row.status = NotificationStatus.sent
            row.payload = {
                **(row.payload or {}),
                "sent_at": now.isoformat(),
                **({"simulated": True} if simulated else {}),
                **({"wa_message_id": result.get("message_id")} if result.get("message_id") else {}),
            }
            _audit_notification(
                db, row, NotificationStatus.sent, correlation_id=correlation_id,
                extra={
                    **({"simulated": True} if simulated else {"channel": "whatsapp"}),
                    **({"wa_message_id": result.get("message_id")} if result.get("message_id") else {}),
                },
            )
            # The survey only becomes "listening" once its opening has actually
            # gone out. Marked here, in the one place that knows the send
            # succeeded — including in simulator mode, so the whole flow is
            # rehearsable with no WhatsApp credentials at all.
            if row.kind == NotificationKind.feedback:
                feedback.mark_opening_sent(db, ref=row.ref, now=now)
            sent += 1
            continue

        # ---- Failure: bounded retry ------------------------------------------ #
        attempts = int((row.payload or {}).get("attempts", 0)) + 1
        row.payload = {
            **(row.payload or {}),
            "attempts": attempts,
            "last_error": result.get("error") or result.get("reason"),
        }
        if attempts >= config.NOTIFY_MAX_ATTEMPTS:
            _retire(row, "send_failed", correlation_id)
            row.payload = {**row.payload, "attempts": attempts}
            retired += 1
        else:
            # Left `scheduled` on purpose — the next tick retries it.
            retried += 1

    # Retire feedback surveys nobody has answered for FEEDBACK_EXPIRY_HOURS.
    # Ridden along on this tick rather than given a scheduler job of its own:
    # AD-F keeps the demo to one in-process timer, and an expiry that runs every
    # 15 seconds instead of every hour costs one indexed query against a table
    # with a handful of rows.
    expired_surveys = feedback.expire_stale(db, now=now)

    db.commit()
    return {
        "due": len(due),
        "sent": sent,
        "skipped": skipped,
        "retried": retried,
        "retired": retired,
        "surveys_expired": expired_surveys,
    }


def run_dispatch_tick() -> None:
    """One APScheduler tick: own DB session, fully guarded. Never raises."""
    db = SessionLocal()
    try:
        result = dispatch_due(db)
        if (
            result["sent"] or result["skipped"] or result["retired"]
            or result["surveys_expired"]
        ):
            log.info("notify tick: %s", result)
    except Exception as exc:  # noqa: BLE001 — a bad tick must not stop the scheduler
        log.warning("notify tick failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


__all__ = ["dispatch_due", "run_dispatch_tick"]
