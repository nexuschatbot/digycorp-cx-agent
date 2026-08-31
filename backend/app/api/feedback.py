"""Agent-conversation feedback — POST /api/feedback (DIRECT path, no LLM).

The star rating + optional comment a customer leaves when the CHAT is closing —
feedback on Aisha (the agent), distinct from the post-drive survey. Stored in the
`agent_feedback` table; the Salesforce sidecar mirrors each row to
`Test_Drive_Agent_Feedback__c`. Client name / mobile are resolved from the
booking reference (if the chat made a booking) and snapshotted on the row, so the
record is self-contained.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AgentFeedback, AuditEvent, Booking, Customer

router = APIRouter(prefix="/api", tags=["feedback"])

# The app stores local times as naive Asia/Dubai (UTC+4), matching how bookings
# store slot times; the SF sidecar stamps +0400. Compute the same here so
# Feedback_At shows the correct local time regardless of the container/DB tz.
_DUBAI = timezone(timedelta(hours=4))


class FeedbackIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    stars: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    booking_ref: str | None = Field(default=None, max_length=32)


@router.post("/feedback")
def submit_feedback(body: FeedbackIn, db: Session = Depends(get_db)) -> dict:
    client_name: str | None = None
    mobile: str | None = None
    customer_id: int | None = None
    session_id = body.session_id.strip()

    # 1) Best source: the booking this chat made (also ties the feedback to it).
    ref = (body.booking_ref or "").strip() or None
    if ref:
        booking = db.query(Booking).filter(Booking.ref == ref).first()
        if booking is not None:
            customer_id = booking.customer_id
            cust = booking.customer
            if cust is not None:
                client_name = cust.name_en or cust.name_ar
                mobile = cust.mobile

    # 2) Fallback: the customer IDENTIFIED in this chat (gave name + mobile via
    #    find_or_create_customer) even if they didn't book. The session→customer
    #    binding lives in graph state, so recover it from the audit trail: the
    #    tool_call audit row carries the mobile the customer provided. Match the
    #    Customer by mobile suffix (stored form is +971-prefixed).
    if mobile is None:
        # payload is a generic JSON column (no JSONB ->> operator), so pull the
        # session's recent audit rows and find the latest find_or_create_customer
        # in Python.
        rows = (
            db.query(AuditEvent)
            .filter(AuditEvent.session == session_id)
            .order_by(AuditEvent.ts.desc())
            .limit(50)
            .all()
        )
        for ev in rows:
            p = ev.payload if isinstance(ev.payload, dict) else {}
            if p.get("tool") != "find_or_create_customer":
                continue
            inp = p.get("input") or {}
            raw_mobile = (inp.get("mobile") or "").strip()
            raw_name = (inp.get("name") or "").strip() or None
            if raw_mobile:
                cust = (
                    db.query(Customer)
                    .filter(Customer.mobile.like(f"%{raw_mobile}"))
                    .first()
                )
                if cust is not None:
                    customer_id = cust.id
                    client_name = cust.name_en or cust.name_ar or raw_name
                    mobile = cust.mobile
                else:
                    client_name = raw_name
                    mobile = raw_mobile
            break

    row = AgentFeedback(
        session_id=session_id,
        customer_id=customer_id,
        client_name=client_name,
        mobile=mobile,
        booking_ref=ref,
        stars=body.stars,
        comment=(body.comment or "").strip() or None,
        created_at=datetime.now(_DUBAI).replace(tzinfo=None),
    )
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}
