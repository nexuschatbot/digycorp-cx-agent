"""Device recognition tests (C10).

The four properties this feature stands on, each tested for the failure it
prevents rather than for the happy path:

  1. LOOKUP RETURNS A NAME AND NOTHING ELSE — because whatever it returns is
     about to be spoken to whoever is holding the device.
  2. CONSENT IS RE-CHECKED ON EVERY READ, not trusted from write time.
  3. A LINK IS NEVER WRITTEN WITHOUT CONSENT.
  4. FORGETTING ENDS RECOGNITION, on both the full-erasure and the
     withdraw-consent-only paths.

The bind guard itself (you cannot answer a question that has not been asked)
lives in tests/test_recognition_guard.py, because it is a property of the graph
rather than of this service.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Customer, DeviceLink
from app.services import consent, customers, memory, recognition

SESSION = "sess-recognition-1"
TOKEN = "device-token-aaaa"
OTHER_TOKEN = "device-token-bbbb"


def _customer(db, *, mobile="+971501234567", name="Ahmed Al Marzooqi", memory_ok=True):
    cid = customers.find_or_create(db, mobile=mobile, name=name)["customer"]["customer_id"]
    if memory_ok:
        consent.record(db, session_id=SESSION, customer_id=cid, memory=True)
    return cid


# --------------------------------------------------------------------------- #
# 3. Consent gates the WRITE
# --------------------------------------------------------------------------- #
def test_no_link_without_memory_consent(db):
    """The gate that made C10 look broken in the first place, from the other side.

    A customer who has not agreed to be remembered must leave no device link —
    recognition is remembering somebody between visits, which is the thing
    memory consent grants.
    """
    cid = _customer(db, memory_ok=False)
    assert recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION) is False
    assert db.scalars(select(DeviceLink)).all() == []


def test_link_written_once_consent_is_granted(db):
    cid = _customer(db)
    assert recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION) is True
    rows = db.scalars(select(DeviceLink)).all()
    assert len(rows) == 1
    assert rows[0].customer_id == cid


def test_the_raw_token_is_never_stored(db):
    """A dump of device_links must not hand anybody a working cookie."""
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)
    row = db.scalars(select(DeviceLink)).one()
    assert row.token_hash != TOKEN
    assert TOKEN not in row.token_hash
    assert row.token_hash == recognition.hash_token(TOKEN)
    assert len(row.token_hash) == 64  # sha256 hex


def test_relinking_the_same_device_does_not_duplicate(db):
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id="sess-later")
    rows = db.scalars(select(DeviceLink)).all()
    assert len(rows) == 1
    assert rows[0].last_session == "sess-later"


# --------------------------------------------------------------------------- #
# 1. Lookup returns a NAME, never a dossier
# --------------------------------------------------------------------------- #
def test_lookup_returns_a_name_and_no_contact_details(db):
    """THE MOST IMPORTANT ASSERTION IN C10.

    Everything this returns is about to be said out loud to whoever is holding
    the device, before anybody has confirmed anything. A mobile number or a
    booking here would make the greeting itself the leak.
    """
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)

    hit = recognition.lookup(db, token=TOKEN)
    assert hit is not None
    assert hit["name_en"] == "Ahmed Al Marzooqi"
    assert hit["customer_id"] == cid

    forbidden = {"mobile", "email", "bookings", "booking_ref", "memory", "profile"}
    assert forbidden.isdisjoint(hit), f"lookup leaked {forbidden & set(hit)}"
    # And nothing that merely LOOKS like a number should have crept in either.
    assert "+971" not in repr(hit)


def test_unknown_device_is_not_an_error(db):
    assert recognition.lookup(db, token="never-seen-this") is None
    assert recognition.lookup(db, token=None) is None
    assert recognition.lookup(db, token="") is None


def test_a_customer_with_no_name_is_not_recognised(db):
    """"Is that still you?" is not a question anybody can answer without a name."""
    cid = _customer(db, name=None, mobile="+971502223333")
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)
    assert recognition.lookup(db, token=TOKEN) is None


# --------------------------------------------------------------------------- #
# 2. Consent is re-checked on every READ
# --------------------------------------------------------------------------- #
def test_withdrawing_consent_stops_recognition_even_if_the_row_survives(db):
    """The belt to the delete's braces.

    memory.forget removes the link, so this forces the case where it did not:
    the row is put back by hand and the read must STILL refuse. A delete that
    silently failed would otherwise keep greeting somebody who told us to stop.
    """
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)

    consent.record(db, session_id=SESSION, customer_id=cid, memory=False)
    db.flush()

    # Re-insert whatever the withdrawal removed, to isolate the READ gate.
    if not db.scalars(select(DeviceLink)).all():
        db.add(DeviceLink(token_hash=recognition.hash_token(TOKEN), customer_id=cid))
        db.flush()
    assert db.scalars(select(DeviceLink)).all(), "row needed for this test to mean anything"

    assert recognition.lookup(db, token=TOKEN) is None


# --------------------------------------------------------------------------- #
# 4. Forgetting ends recognition
# --------------------------------------------------------------------------- #
def test_withdrawing_memory_consent_unlinks_the_device(db):
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)
    consent.record(db, session_id=SESSION, customer_id=cid, memory=False)
    db.flush()
    assert db.scalars(select(DeviceLink).where(DeviceLink.customer_id == cid)).all() == []


def test_confirmed_forget_me_unlinks_and_reports_it(db):
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)

    out = memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)
    assert out["forgotten"] is True
    db.flush()

    assert recognition.lookup(db, token=TOKEN) is None
    assert db.scalars(select(DeviceLink)).all() == []
    # The purge receipt must SAY the device was unlinked — an erasure whose
    # scope is invisible is one nobody can check.
    removed = out.get("removed") or {}
    assert removed.get("device_links", out.get("devices_unlinked")) == 1


def test_erasure_does_not_take_another_customers_device(db):
    """Two people share a tablet; one asks to be forgotten. The other is untouched."""
    a = _customer(db, mobile="+971501111111", name="Ahmed")
    b = _customer(db, mobile="+971502222222", name="Layla")
    recognition.link(db, token=TOKEN, customer_id=a, session_id=SESSION)
    recognition.link(db, token=TOKEN, customer_id=b, session_id=SESSION)
    assert len(db.scalars(select(DeviceLink)).all()) == 2

    memory.forget(db, session_id=SESSION, customer_id=a, confirmed=True)
    db.flush()

    remaining = db.scalars(select(DeviceLink)).all()
    assert [r.customer_id for r in remaining] == [b]
    assert recognition.lookup(db, token=TOKEN)["customer_id"] == b


def test_unlink_is_scoped_to_the_customer(db):
    a = _customer(db, mobile="+971501111111", name="Ahmed")
    recognition.link(db, token=TOKEN, customer_id=a, session_id=SESSION)
    recognition.link(db, token=OTHER_TOKEN, customer_id=a, session_id=SESSION)
    assert recognition.unlink_customer(db, customer_id=a) == 2
    assert db.scalars(select(DeviceLink)).all() == []


def test_a_deleted_customer_is_not_recognised(db):
    """Belt-and-braces against a link outliving the person it names."""
    cid = _customer(db)
    recognition.link(db, token=TOKEN, customer_id=cid, session_id=SESSION)
    db.execute(select(Customer))  # keep the session warm
    memory.forget(db, session_id=SESSION, customer_id=cid, confirmed=True)
    db.flush()
    assert recognition.lookup(db, token=TOKEN) is None
