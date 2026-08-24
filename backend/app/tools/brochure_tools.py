"""send_brochure (C3) — put a model summary on the customer's WhatsApp.

RIDES THE NOTIFICATION RAILS, and that is the whole point of doing it this way.
Writing a row rather than calling WhatsApp directly inherits four things already
built and already trusted: the CONSENT GATE at send time (dispatcher checks
whatsapp_consent, not this tool), the audit trail, the bounded retry, and
visibility in the console's Notifications feed — which is where the simulator
send is actually SEEN. A direct send would work and show nobody anything.

`customer_id` is INJECTED, never modelled: this puts a message on somebody's
phone, so whose phone it is must be resolved server-side from the session
binding. A model-supplied id here would let a sentence in the chat window choose
a recipient.

NO REF, DELIBERATELY. A brochure is asked for while browsing, long before any
booking exists — which is exactly the moment it is most useful. That is why
`Notification.ref` became nullable and `customer_id` was added: the recipient of
a brochure is a PERSON, not a booking.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Language,
    Notification,
    NotificationKind,
    NotificationStatus,
)
from app.services import brochure as brochure_service

log = logging.getLogger("app.tools.brochure")

SEND_BROCHURE_SCHEMA = {
    "name": "send_brochure",
    "description": (
        "Send the customer a summary of one model on WhatsApp — what it costs, "
        "how many it seats, whether it is petrol or hybrid, and what stands "
        "out.\n\n"
        "Use it when they ask for a brochure, a spec sheet, details to read "
        "later, or something to show someone else ('can you send me that?', "
        "'my wife wants to see it'). You may also offer it once, warmly, when "
        "somebody is clearly weighing a car up and would obviously rather read "
        "than scroll — never more than once, and never instead of answering "
        "what they asked.\n\n"
        "THEY MUST BE IDENTIFIED FIRST, because this goes to a phone number. If "
        "nobody is identified yet you get customer_not_identified back — ask for "
        "their name and mobile and call find_or_create_customer, exactly as you "
        "would for a booking.\n\n"
        "IT ALSO NEEDS THEIR PERMISSION TO BE MESSAGED. If they have not agreed "
        "to WhatsApp you get consent_required — so ask, record it with "
        "record_consent, then send. A refusal is completely normal: say so "
        "warmly, offer to go through the details in the chat instead, and do not "
        "ask again.\n\n"
        "Tell them plainly that it is on its way and what it covers. Do not "
        "promise photographs, finance figures or trim tables — it is a summary "
        "of the things we hold, and saying otherwise sets up a disappointment "
        "you will not be there for.\n\n"
        "One model per call. For two cars, call it twice."
    ),
    "input_schema": {
        "type": "object",
        "required": ["vehicle_id"],
        "properties": {
            "vehicle_id": {
                "type": "integer",
                "description": (
                    "The model to send, from a tool result you have already "
                    "received — never a number you inferred from the name."
                ),
            },
        },
    },
}


def send_brochure(
    db: Session,
    session_id: str,
    customer_id: int | None = None,  # INJECTED by the tool node, never modelled
    vehicle_id: object = None,
    correlation_id: str | None = None,  # INJECTED by the tool node
    **_ignored: object,
) -> dict:
    """Queue a brochure for immediate dispatch. Returns the outcome as DATA.

    Writes a `scheduled` row due NOW rather than sending inline: the dispatcher
    owns sending, so the consent gate, the retry and the audit row all happen in
    the one place that already does them for every other message we send. It
    also means a brochure behaves identically whether WhatsApp is live or
    simulated, which is what makes the demo honest.
    """
    if customer_id is None:
        return {
            "error": "customer_not_identified",
            "detail": (
                "A brochure goes to a phone number, so ask for their name and "
                "mobile and call find_or_create_customer first."
            ),
        }

    customer = db.get(Customer, customer_id)
    if customer is None:
        return {"error": "customer_not_found", "customer_id": customer_id}

    # Checked HERE as well as in the dispatcher, and the duplication is the
    # point: the dispatcher's check silently retires the row, which is right for
    # a scheduled reminder and wrong for something the customer just asked for.
    # Refusing up front lets Aisha ask for permission in the same breath instead
    # of promising a message that will never arrive.
    if not customer.whatsapp_consent:
        return {
            "error": "consent_required",
            "detail": (
                "They have not agreed to WhatsApp messages yet. Ask whether we "
                "may send it there, record their answer with record_consent, "
                "then try again."
            ),
            "customer_id": customer.id,
        }

    summary = brochure_service.summary_for(db, _coerce_int(vehicle_id))
    if summary is None:
        return {
            "error": "vehicle_not_found",
            "detail": "Look the model up first and pass the vehicle_id it returns.",
            "vehicle_id": vehicle_id,
        }

    row = Notification(
        ref=None,  # a brochure belongs to a person, not a booking
        customer_id=customer.id,
        kind=NotificationKind.brochure,
        lang=customer.language_pref or Language.en,
        body="",  # rendered by the dispatcher, like every other kind
        payload={
            "vehicle_id": summary["vehicle_id"],
            "vehicle_name": summary["name"],
            "model_code": summary["model_code"],
            "price": summary["price"],
            "seats": summary["seats"],
            "fuel": summary["fuel"],
            "document": f"/api/brochure/{summary['vehicle_id']}",
        },
        status=NotificationStatus.scheduled,
        send_at=datetime.now(),  # due immediately; the tick picks it up
    )
    db.add(row)
    db.flush()

    log.info(
        "brochure queued: vehicle=%s customer=%s session=%s",
        summary["vehicle_id"], customer.id, session_id,
    )
    return {
        "queued": True,
        "vehicle_name": summary["name"],
        "to_mobile": customer.mobile,
        "covers": ["price", "seats", "powertrain", "highlights"],
        "detail": (
            "On its way to their WhatsApp. It is a one-page summary of price, "
            "seats, powertrain and highlights — not photographs or finance."
        ),
    }


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
