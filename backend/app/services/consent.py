"""Consent service (FR-CF-05, PDPL).

Three properties this module exists to guarantee:

1. MESSAGING AND MEMORY ARE SEPARATE DECISIONS. A customer may happily accept a
   WhatsApp confirmation while refusing to be remembered — those are different
   questions with different answers, and neither may imply the other. They are
   separate columns, set independently, and passing one here never touches the
   other.
2. CONSENT CAN BE WITHDRAWN. `False` is a real, recordable decision, not a
   no-op to be skipped because "there's nothing to grant". A system that can
   only record yes has not implemented consent.
3. EVERY CHANGE IS AUDITED, IN BOTH DIRECTIONS. The audit trail is the PDPL
   evidence: it is what shows we asked before messaging, and that we stopped
   when told to. A withdrawal that leaves no trace is indistinguishable from
   one that never happened.
4. WITHDRAWING MEMORY CONSENT ERASES THE PROFILE. Withdrawal removes the legal
   basis for holding it, so continuing to hold it is not defensible — consent
   we can no longer rely on plus data we still keep is the gap PDPL exists to
   close. Done HERE, in the service, so the direct/REST path and any future
   caller inherit it and no screen can forget. Unlike forget_me there is no
   confirmation step: a withdrawal is ALREADY a recorded decision the customer
   made, and asking twice would be arguing with it.

On the `_at` columns: they mean "when consent was GRANTED", so a withdrawal
NULLs them rather than repurposing them into a "last changed" timestamp. Nothing
is lost — the append-only audit event carries the withdrawal's timestamp, which
is the record that actually matters, and it cannot be overwritten by the next
change the way a mutable column can.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Actor, AuditType, Customer
from app.services import audit

# The consent axes, mapped to their (flag column, granted-at column). Adding an
# axis means adding it here and nowhere else.
AXES: dict[str, tuple[str, str]] = {
    "messaging": ("whatsapp_consent", "whatsapp_consent_at"),
    "memory": ("memory_consent", "memory_consent_at"),
}


def _coerce_bool(value: object) -> bool | None:
    """Strict-ish bool coercion. Returns None for "not supplied".

    Deliberately does NOT treat arbitrary truthy values as consent: consent is
    the one field where guessing the customer's answer is unacceptable. A value
    we cannot read is an omission, not a yes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "1"}:
            return True
        if v in {"false", "no", "0"}:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def record(
    db: Session,
    *,
    session_id: str,
    customer_id: int,
    messaging: object = None,
    memory: object = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
    _erase_profile: bool = True,
) -> dict:
    """Record consent decisions for a customer. Never raises.

    Pass only the axes the customer actually decided on; an omitted axis is left
    exactly as it was. Passing False WITHDRAWS consent and is recorded as such.

    Withdrawing `memory` also HARD-DELETES the stored profile (property 4 above),
    producing a second audit row of type `erasure` alongside the `consent` one,
    both stamped with the same correlation_id so the console groups them.

    `correlation_id` (optional) ties each consent audit row back to the
    `tool_call` event for the same record_consent invocation — see audit.record.
    The direct/CLI path leaves it None; only the agent path threads one.

    `_erase_profile` is INTERNAL and not part of the tool contract. memory.forget
    passes False when it calls us, because it has already deleted the profile
    itself and only wants the flag put down — that is what stops the two modules
    recursing into each other. No other caller should set it.
    """
    now = now or datetime.now()

    customer = db.get(Customer, customer_id) if customer_id is not None else None
    if customer is None:
        return {"error": "customer_not_found", "customer_id": customer_id}

    decisions = {"messaging": _coerce_bool(messaging), "memory": _coerce_bool(memory)}
    if all(v is None for v in decisions.values()):
        return {
            "error": "no_consent_specified",
            "detail": "Pass messaging and/or memory as true or false.",
            "customer_id": customer.id,
        }

    changed: list[dict] = []
    for axis, granted in decisions.items():
        if granted is None:
            continue  # not asked about — leave alone, never infer from the other
        flag_col, at_col = AXES[axis]
        previous = bool(getattr(customer, flag_col))
        setattr(customer, flag_col, granted)
        setattr(customer, at_col, now if granted else None)

        # Audit even when the value did not change: "they confirmed it again" is
        # a real event, and a re-confirmation we failed to log looks identical to
        # never having asked. `changed_from`/`changed_to` carry the direction so
        # a withdrawal is unmistakable in the trail.
        audit.record(
            db,
            session=session_id,
            actor=Actor.customer,
            type=AuditType.consent,
            payload={
                "axis": axis,
                "granted": granted,
                "changed_from": previous,
                "changed_to": granted,
                "withdrawal": previous and not granted,
                "customer_id": customer.id,
                "at": now.isoformat(),
            },
            correlation_id=correlation_id,
        )
        changed.append({"axis": axis, "granted": granted, "was": previous})

        # Withdrawing memory removes the basis for holding the profile, so the
        # profile goes with it (property 4). Only on a real True -> False
        # transition: re-recording an existing False is not a new withdrawal, and
        # firing an erasure row every time would make the trail lie about how
        # often the customer asked.
        if axis == "memory" and _erase_profile and previous and not granted:
            # Deferred import — memory.forget calls back into this function on a
            # customer-initiated erasure, so a module-level import would be
            # circular. See memory.forget's withdraw_consent flag for the other
            # half of the break.
            from app.services import memory as memory_service

            memory_service.forget(
                db,
                session_id=session_id,
                customer_id=customer.id,
                confirmed=True,
                withdraw_consent=False,  # we are the ones withdrawing it
                reason="consent_withdrawn",
                now=now,
                correlation_id=correlation_id,
            )

    db.flush()
    return {
        "customer_id": customer.id,
        "consent_messaging": bool(customer.whatsapp_consent),
        "consent_memory": bool(customer.memory_consent),
        "recorded": changed,
    }
