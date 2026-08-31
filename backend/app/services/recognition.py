"""Device recognition — "welcome back" without asking twice (C10, 2026-08-14).

WHAT THIS IS. A long-lived, HttpOnly cookie on the browser, hashed into
`device_links` against the customer it was last used by. On a return visit the
agent is told a NAME and nothing else, asks whether it is still them, and binds
the session only on a yes.

WHAT THIS IS NOT, and the distinction is the whole design: it is not a login.
A cookie proves possession of a browser, not identity — a shared iPad, a phone
lent to a spouse, a tablet on the showroom floor. So the four rules below are
structural, not prompt guidance:

1. LOOKUP RETURNS A NAME, NEVER A DOSSIER. No mobile, no bookings, no memory
   profile. Everything `lookup` returns is safe to say out loud to whoever is
   holding the device, because the agent is about to do exactly that. The moment
   it returned a booking list, the greeting itself would be the leak.
2. RECOGNITION NEVER BINDS ON ITS OWN. `lookup` is read-only and touches no
   graph state. Binding happens in `confirm`, reached only after the customer
   answers the question — see tools/recognition_tools.py.
3. CONSENT IS RE-CHECKED ON EVERY READ. `memory_consent` is the legal basis for
   holding the link at all (PDPL), so a withdrawal stops recognition on the next
   visit even if the row survived. The row is deleted too — this is the belt to
   that braces, because a delete that failed silently would otherwise keep
   greeting somebody who told us to forget them.
4. MANAGING A BOOKING STILL NEEDS identify_by_reference_or_mobile. Recognition
   changes what Aisha KNOWS, never what she will ACT on for an existing booking.
   That path is untouched by this module, deliberately: if a device could
   reschedule somebody else's Saturday, recognition would have become a way in.

ON THE HASH. The cookie value is the secret; what is stored is its SHA-256, for
the reason a password digest is stored rather than the password. A dump of
`device_links` yields no usable cookie. Plain SHA-256 with no salt or stretching
is correct HERE and would not be for a password: the input is 256 bits of
`secrets.token_urlsafe` entropy, so there is no dictionary to run against it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session

from app.models import Customer, DeviceLink

log = logging.getLogger("app.services.recognition")

# The cookie the browser carries. HttpOnly so no page script can read it — it is
# a bearer credential and JavaScript has no business holding one. SameSite=Lax
# (not None) so it does not ride cross-site requests; the chat is same-origin, so
# Lax costs us nothing and closes CSRF-shaped uses of it.
COOKIE_NAME = "eg_device"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # one year


def mint_token() -> str:
    """A fresh device token. 32 bytes of entropy, URL-safe for a cookie value."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex of a device token — what `device_links.token_hash` stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Write — remember that this device belongs to this customer
# --------------------------------------------------------------------------- #
def link(
    db: Session,
    *,
    token: str | None,
    customer_id: int,
    session_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Record (or refresh) this device -> customer link. Returns whether it stuck.

    CONSENT-GATED AT THE WRITE TOO. No memory consent, no row: recognition is
    remembering somebody between visits, which is the thing `memory_consent`
    grants. Writing the row now and gating only the read would leave us holding
    a link we had no basis to hold, which is the gap PDPL exists to close.

    NEVER RAISES. This is a convenience feature riding on somebody's booking; a
    failure here must cost them nothing. The worst outcome is that their next
    visit starts as a stranger — which is exactly where they were before C10.
    """
    if not token or customer_id is None:
        return False
    try:
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.memory_consent:
            return False

        now = now or datetime.now()
        token_hash = hash_token(token)
        row = (
            db.query(DeviceLink)
            .filter(
                DeviceLink.token_hash == token_hash,
                DeviceLink.customer_id == customer_id,
            )
            .one_or_none()
        )
        if row is None:
            db.add(
                DeviceLink(
                    token_hash=token_hash,
                    customer_id=customer_id,
                    created_at=now,
                    last_seen_at=now,
                    last_session=session_id,
                )
            )
        else:
            row.last_seen_at = now
            row.last_session = session_id
        db.flush()
        return True
    except Exception:  # noqa: BLE001 — see NEVER RAISES above
        log.exception("device link failed (customer=%s) — continuing", customer_id)
        return False


# --------------------------------------------------------------------------- #
# Read — who was this device last used by?
# --------------------------------------------------------------------------- #
def lookup(db: Session, *, token: str | None) -> dict | None:
    """The customer this device was last used by, or None.

    RETURNS A NAME AND AN ID, AND NOTHING ELSE (rule 1 above). The id is for the
    server — `confirm` uses it and it never reaches the model. Both name scripts
    come back, unpicked, for the same reason customers._customer_dict returns
    both: the agent replies in the language of the CURRENT turn, so choosing here
    would be wrong the moment the customer code-switches.

    Most recent link wins on a shared device. Never raises; a failure is "we do
    not recognise this browser", which is a normal answer, not an error.
    """
    if not token:
        return None
    try:
        row = (
            db.query(DeviceLink)
            .filter(DeviceLink.token_hash == hash_token(token))
            .order_by(DeviceLink.last_seen_at.desc())
            .first()
        )
        if row is None:
            return None
        customer = db.get(Customer, row.customer_id)
        # Rule 3: consent re-checked at read time, not trusted from write time.
        if customer is None or not customer.memory_consent:
            return None
        if not (customer.name_en or customer.name_ar):
            # A customer we hold no name for gives the agent nothing to ask about
            # ("is that still you?" is not a question anybody can answer), so
            # there is nothing to recognise. Not an error — just no greeting.
            return None
        return {
            "customer_id": customer.id,
            "name_en": customer.name_en,
            "name_ar": customer.name_ar,
            "title": customer.title,
            "title_ar": customer.title_ar,
            "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }
    except Exception:  # noqa: BLE001
        log.exception("device lookup failed — treating as unrecognised")
        return None


# --------------------------------------------------------------------------- #
# Forget — the link goes when the memory does
# --------------------------------------------------------------------------- #
def unlink_customer(db: Session, *, customer_id: int) -> int:
    """Drop every device link for this customer. Returns rows removed.

    Called from memory.forget (a consent withdrawal that does not purge) and from
    erasure.purge (the full "forget me"). Two callers rather than one because the
    two requests are genuinely different — withdrawing memory consent is not
    asking to be deleted — but recognition ends on both, since both remove the
    basis for it.

    Never raises: on the purge path this runs inside a delete cascade that must
    finish, and on the withdrawal path a failure here must not cost the customer
    the withdrawal itself. Rule 3's read-time consent check is what makes that
    safe — an undeleted row still stops recognising.
    """
    try:
        result = db.execute(
            sa_delete(DeviceLink).where(DeviceLink.customer_id == customer_id)
        )
        return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001
        log.exception("device unlink failed (customer=%s)", customer_id)
        return 0
