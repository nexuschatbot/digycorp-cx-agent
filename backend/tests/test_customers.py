"""Customer identity + +971 mobile validation tests (LOC-09, LOC-07, PDPL)."""

from __future__ import annotations

import pytest

from app.services import customers


# --------------------------------------------------------------------------- #
# Normalisation — the formats real customers actually type (LOC-09)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "+971501234567",  # canonical
        "0501234567",  # local, no country code
        "971501234567",  # country code, no plus
        "00971501234567",  # international dialling prefix
        "+971 50 123 4567",  # spaces
        "+971-50-123-4567",  # dashes
        "(050) 123 4567",  # parens + local
        "501234567",  # bare national number
        "+971 050 123 4567",  # country code AND redundant trunk 0
        "  +971501234567  ",  # stray whitespace
    ],
)
def test_every_real_format_normalises_to_one_number(raw):
    """All of these are the SAME number. Refusing any of them would be us
    failing to read, then blaming the customer.
    """
    assert customers.normalise_mobile(raw) == {"mobile": "+971501234567"}


@pytest.mark.parametrize("prefix", ["50", "52", "54", "55", "56", "58"])
def test_all_uae_mobile_prefixes_accepted(prefix):
    out = customers.normalise_mobile(f"0{prefix}1234567")
    assert out["mobile"] == f"+971{prefix}1234567"


# --------------------------------------------------------------------------- #
# Arabic-Indic digits (LOC-01, S-20) — QR attendees type on their own phones
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "٠٥٠١٢٣٤٥٦٧",  # Arabic-Indic, local format
        "+٩٧١٥٠١٢٣٤٥٦٧",  # Arabic-Indic, with country code
        "٠٥٠ ١٢٣ ٤٥٦٧",  # Arabic-Indic with separators
        "۰۵۰۱۲۳۴۵۶۷",  # EXTENDED Arabic-Indic (Persian/Urdu keyboard)
    ],
)
def test_arabic_indic_digits_are_the_same_number(raw):
    """An Arabic keyboard writes ٠٥٠١٢٣٤٥٦٧ for 0501234567. Rejecting it would
    look, to the customer, exactly like the agent cannot read Arabic.
    """
    assert customers.normalise_mobile(raw) == {"mobile": "+971501234567"}


def test_mixed_ascii_and_arabic_indic_digits(raw_mixed="+971 ٥٠ 123 ٤٥٦٧"):
    """Phones and paste buffers mix scripts more often than you would like."""
    assert customers.normalise_mobile(raw_mixed) == {"mobile": "+971501234567"}


def test_arabic_indic_landline_is_still_a_landline(db):
    """Translation happens BEFORE validation, so every rule sees one digit set —
    a rule must not become bypassable by typing it in another script.
    """
    assert customers.normalise_mobile("٠٤٢٢٢١٢٣٤")["reason"] == "landline"


def test_arabic_indic_too_short_is_still_too_short():
    assert customers.normalise_mobile("+٩٧١٥٠١٢٣٤٥٦")["reason"] == "too_short"


def test_arabic_indic_number_finds_the_same_customer(db):
    """The identity guarantee has to survive the script the number was typed in,
    or an Arabic-speaking returning customer becomes a duplicate.
    """
    a = customers.find_or_create(db, mobile="0501234567")
    b = customers.find_or_create(db, mobile="٠٥٠١٢٣٤٥٦٧")
    assert b["created"] is False
    assert b["customer"]["customer_id"] == a["customer"]["customer_id"]


# --------------------------------------------------------------------------- #
# Rejection — as DATA, with a reason the agent can act on
# --------------------------------------------------------------------------- #
def test_too_short_is_rejected_not_guessed():
    """"+97150123456" is 8 digits. There is no single correct reading, so
    guessing at it could book a stranger's phone.
    """
    out = customers.normalise_mobile("+97150123456")
    assert out["error"] == "invalid_uae_mobile"
    assert out["reason"] == "too_short"


def test_too_long_is_rejected():
    assert customers.normalise_mobile("+9715012345678")["reason"] == "too_long"


@pytest.mark.parametrize("landline", ["042221234", "+97142221234", "022221234", "04 222 1234"])
def test_landline_is_not_a_mobile(landline):
    """We confirm by WhatsApp. Accepting a landline would produce a booking
    whose confirmation silently never arrives.
    """
    out = customers.normalise_mobile(landline)
    assert out["error"] == "invalid_uae_mobile"
    assert out["reason"] == "landline"


def test_unknown_mobile_prefix_rejected():
    """51 is not a UAE mobile prefix."""
    assert customers.normalise_mobile("0511234567")["reason"] == "unknown_prefix"


@pytest.mark.parametrize("junk", ["", "   ", None, 12345, "not a phone", "+971 50 ABC 4567"])
def test_junk_never_raises(junk):
    """A live turn must never die on bad input."""
    out = customers.normalise_mobile(junk)
    assert out["error"] == "invalid_uae_mobile"
    assert "reason" in out and "hint" in out


def test_rejection_carries_machine_readable_reason_and_a_hint():
    out = customers.normalise_mobile("042221234")
    assert isinstance(out["reason"], str)
    assert isinstance(out["hint"], str) and out["hint"]


# --------------------------------------------------------------------------- #
# Relaxed mobile rules (STRICT_UAE_MOBILE=false) — 2026-08-04
# --------------------------------------------------------------------------- #
# The demo stack runs relaxed so a customer on a foreign handset can be
# identified without being interrogated about their own phone number. conftest
# forces the flag ON for the whole suite (the code default every test above is
# written against), so relaxed behaviour has to opt back in here.
@pytest.fixture
def relaxed(monkeypatch):
    from app import config as _config

    monkeypatch.setattr(_config, "STRICT_UAE_MOBILE", False)


def test_a_bare_foreign_number_is_read_as_uae_rather_than_refused(relaxed):
    """The defect this exists to stop: '03409297758' is a Pakistani mobile whose
    first digit collides with Al Ain's 03 area code, so the strict rules called it
    a UAE landline and the agent asked which country it was from — a question no
    customer should have to answer to book a test drive.
    """
    assert customers.normalise_mobile("03409297758") == {"mobile": "+9713409297758"}


@pytest.mark.parametrize("raw", ["03409297758", "0340 929 7758", "٠٣٤٠٩٢٩٧٧٥٨"])
def test_the_same_bare_number_however_it_is_written(relaxed, raw):
    """Separators and an Arabic keyboard are read, not rejected — the relaxed path
    sits AFTER that normalisation, so it inherits it rather than restating it.
    """
    assert customers.normalise_mobile(raw) == {"mobile": "+9713409297758"}


@pytest.mark.parametrize(
    "raw", ["+971501234567", "0501234567", "501234567", "971501234567"]
)
def test_a_uae_mobile_is_untouched_by_the_relaxation(relaxed, raw):
    """Relaxing the rules must not move the numbers that were already right."""
    assert customers.normalise_mobile(raw) == {"mobile": "+971501234567"}


def test_a_number_with_its_own_country_code_keeps_it(relaxed):
    """+92 stays +92 — the bare-number path must not overwrite a country the
    customer actually stated.
    """
    assert customers.normalise_mobile("+923409297758") == {"mobile": "+923409297758"}


def test_a_uae_landline_is_still_refused_when_relaxed(relaxed):
    """8 digits behind an area code is a real landline, and the length is what
    tells it apart from a foreign mobile starting with the same digit.
    """
    assert customers.normalise_mobile("042221234")["reason"] == "landline"


@pytest.mark.parametrize("junk", ["123", "1" * 13, "not a phone", ""])
def test_relaxed_still_refuses_what_is_not_a_number(relaxed, junk):
    """Permissive is not credulous: the E.164 ceiling and the digits check hold,
    so a live turn still fails honestly rather than booking nonsense.
    """
    out = customers.normalise_mobile(junk)
    assert out["error"] == "invalid_uae_mobile"
    assert out["reason"] in {"too_short", "too_long", "not_a_number", "missing"}


def test_bare_and_prefixed_spellings_find_ONE_customer(db, relaxed):
    """The reason canonicalisation matters at all. Someone who books saying
    '03409297758' and comes back saying '+9713409297758' must reach the same
    customer, or their booking is invisible to the reschedule they just asked for.
    """
    first = customers.find_or_create(db, mobile="03409297758", name="Bilal")
    again = customers.find_or_create(db, mobile="+971 340 929 7758", name="Bilal")
    assert first["customer"]["customer_id"] == again["customer"]["customer_id"]


# --------------------------------------------------------------------------- #
# find_or_create — idempotency (the entry point to every booking)
# --------------------------------------------------------------------------- #
def test_find_or_create_creates_then_finds(db):
    first = customers.find_or_create(db, mobile="+971501234567", name="Sara")
    assert first["created"] is True
    second = customers.find_or_create(db, mobile="+971501234567")
    assert second["created"] is False
    assert second["customer"]["customer_id"] == first["customer"]["customer_id"]


def test_same_number_different_format_is_the_same_customer(db):
    """Idempotency is on the NUMBER, not the string. "0501234567" and
    "+971 50 123 4567" must not become two customers.
    """
    a = customers.find_or_create(db, mobile="0501234567")
    b = customers.find_or_create(db, mobile="+971 50 123 4567")
    c = customers.find_or_create(db, mobile="971501234567")
    assert a["customer"]["customer_id"] == b["customer"]["customer_id"] == c["customer"]["customer_id"]
    assert b["created"] is False and c["created"] is False


def test_invalid_mobile_creates_nothing(db):
    from sqlalchemy import func, select

    from app.models import Customer

    before = db.scalar(select(func.count()).select_from(Customer))
    assert customers.find_or_create(db, mobile="042221234")["error"] == "invalid_uae_mobile"
    assert db.scalar(select(func.count()).select_from(Customer)) == before


def test_new_customer_defaults_to_no_consent(db):
    """Consent is never assumed. A customer we just met has consented to
    nothing (PDPL).
    """
    out = customers.find_or_create(db, mobile="+971501234567")["customer"]
    assert out["consent_messaging"] is False
    assert out["consent_memory"] is False


# --------------------------------------------------------------------------- #
# Never overwrite stored data from a conversation
# --------------------------------------------------------------------------- #
def test_chat_cannot_overwrite_a_stored_name(db):
    """Anyone typing "I'm Mohammed" against Ravi's number must not rewrite
    Ravi's profile. A chat name is an assertion, not a fact.
    """
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(db, mobile="+971501234567", name="Mohammed")
    assert out["customer"]["name_en"] == "Ravi Sharma"


def test_chat_cannot_overwrite_a_stored_title(db):
    customers.find_or_create(db, mobile="+971501234567", title="Dr")
    out = customers.find_or_create(db, mobile="+971501234567", title="Mr")
    assert out["customer"]["title"] == "Dr"


def test_chat_fills_a_field_that_is_empty(db):
    """Filling a blank is not overwriting — it is the only way a name ever
    arrives for a new customer.
    """
    customers.find_or_create(db, mobile="+971501234567")
    out = customers.find_or_create(db, mobile="+971501234567", name="Sara")
    assert out["customer"]["name_en"] == "Sara"


# --------------------------------------------------------------------------- #
# Name+number second factor (developer decision 2026-07-23, D1 PDPL).
# verify_name is OFF by default (trusted callers unchanged) and ON for the agent
# tool: a returning, NAMED customer must present the matching FULL name.
# --------------------------------------------------------------------------- #
def test_verify_name_off_by_default_binds_on_number_alone(db):
    """Default behaviour is unchanged — trusted/CLI callers identify by number."""
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(db, mobile="+971501234567")  # no verify_name
    assert out["created"] is False
    assert "customer" in out and "error" not in out


def test_verify_name_matches_returning_customer(db):
    """Right number + right full name (any case) -> identified."""
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(
        db, mobile="+971501234567", name="ravi   SHARMA", verify_name=True
    )
    assert "error" not in out
    assert out["customer"]["name_en"] == "Ravi Sharma"


def test_verify_name_mismatch_is_identity_unconfirmed(db):
    """Right number, WRONG name -> not identified, no binding. The agent phrases
    this as an honest 'no booking found' (mismatch must not confirm the number)."""
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(
        db, mobile="+971501234567", name="Mohammed", verify_name=True
    )
    assert out["error"] == "identity_unconfirmed"
    assert "customer" not in out


def test_verify_name_first_name_alone_fails(db):
    """FULL name required (developer decision) — a first name is not enough."""
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(db, mobile="+971501234567", name="Ravi", verify_name=True)
    assert out["error"] == "identity_unconfirmed"
    assert "customer" not in out


def test_verify_name_required_when_missing_for_a_known_customer(db):
    """We hold a name but none was supplied -> name_required (ask for it)."""
    customers.find_or_create(db, mobile="+971501234567", name="Ravi Sharma")
    out = customers.find_or_create(db, mobile="+971501234567", verify_name=True)
    assert out["error"] == "name_required"
    assert "customer" not in out


def test_verify_name_is_vacuous_for_a_new_or_nameless_customer(db):
    """A number we have never seen (or hold no name for) has nothing to match, so
    verification never blocks a first-time booking."""
    fresh = customers.find_or_create(db, mobile="+971501234567", verify_name=True)
    assert fresh["created"] is True and "error" not in fresh
    # Existing but nameless: still binds, and the name now fills the blank.
    named = customers.find_or_create(db, mobile="+971501234567", name="Sara", verify_name=True)
    assert "error" not in named
    assert named["customer"]["name_en"] == "Sara"


def test_agent_path_cannot_create_a_nameless_customer(db):
    """The nameless-booking bug (live 2026-07-31, voice call): the model
    skipped the name question and find_or_create_customer happily created a
    customer named "—" and booked them a drive. The AGENT TOOL now requires
    a name to CREATE; looking up an existing customer by mobile alone stays
    legitimate (FR-RS-01), and trusted callers (seed CLI, handoff) keep the
    permissive service default."""
    from sqlalchemy import select

    from app.models import Customer
    from app.tools.customer_tools import find_or_create_customer

    # New number, no name: refused, and no row exists.
    out = find_or_create_customer(db, "sess-1", mobile="+971501234567")
    assert out["error"] == "name_required"
    assert db.scalar(select(Customer).where(Customer.mobile == "+971501234567")) is None

    # Whitespace is not a name.
    out = find_or_create_customer(db, "sess-1", mobile="+971501234567", name="  ")
    assert out["error"] == "name_required"

    # With a name, the same call creates.
    out = find_or_create_customer(db, "sess-1", mobile="+971501234567", name="Sara Ahmed")
    assert "error" not in out and out["created"] is True

    # Existing customer, mobile + matching name — unchanged.
    again = find_or_create_customer(
        db, "sess-1", mobile="+971501234567", name="Sara Ahmed"
    )
    assert "error" not in again and again["created"] is False

    # The permissive default still serves trusted callers.
    trusted = customers.find_or_create(db, mobile="+971509998888")
    assert "error" not in trusted and trusted["created"] is True


def test_verify_name_matches_arabic_script(db):
    """A customer whose name we hold in Arabic can confirm it in Arabic."""
    customers.find_or_create(db, mobile="+971501234567", name="راشد المنصوري")
    out = customers.find_or_create(
        db, mobile="+971501234567", name="راشد المنصوري", verify_name=True
    )
    assert "error" not in out


# --------------------------------------------------------------------------- #
# Script routing (LOC-01) — a name is stored as it was written
# --------------------------------------------------------------------------- #
def test_arabic_name_lands_in_name_ar(db):
    out = customers.find_or_create(db, mobile="+971501234567", name="فاطمة المنصوري")
    assert out["customer"]["name_ar"] == "فاطمة المنصوري"
    assert out["customer"]["name_en"] is None


def test_latin_name_lands_in_name_en(db):
    out = customers.find_or_create(db, mobile="+971501234567", name="Daniel Thompson")
    assert out["customer"]["name_en"] == "Daniel Thompson"
    assert out["customer"]["name_ar"] is None


def test_arabic_and_latin_names_coexist(db):
    """Different scripts are different columns, so one never overwrites the
    other — and neither is a translation of the other.
    """
    customers.find_or_create(db, mobile="+971501234567", name="Fatima Al Mansouri")
    out = customers.find_or_create(db, mobile="+971501234567", name="فاطمة المنصوري")
    assert out["customer"]["name_en"] == "Fatima Al Mansouri"
    assert out["customer"]["name_ar"] == "فاطمة المنصوري"


# --------------------------------------------------------------------------- #
# Memory rides on this envelope as a SIBLING key, never inside customer (FR-MEM)
# --------------------------------------------------------------------------- #
def test_no_memory_key_without_consent(db):
    """The default is forgetting: an unconsented customer gets no `memory` key
    at all, not a null one the model might find a way to narrate.

    The second assertion is the load-bearing half and must survive any change to
    the first: `customer` and `memory` have DIFFERENT consent gates, so folding
    the profile into the customer dict would put two gates behind one key. The
    consented case is covered in tests/test_memory.py.
    """
    out = customers.find_or_create(db, mobile="+971501234567")
    assert set(out) == {"customer", "created"}
    assert "memory" not in out["customer"]


# --------------------------------------------------------------------------- #
# No rendering decisions in the data layer
# --------------------------------------------------------------------------- #
def test_no_prepicked_name_field(db):
    """Both scripts are handed over; the agent picks per turn. A name chosen
    here would have to come from language_pref — a STORED preference — while the
    agent replies in the language of the CURRENT turn, so it would be wrong every
    time the customer code-switches (FR-CX-09, S-19).
    """
    customers.find_or_create(db, mobile="+971501234567", name="Fatima Al Mansouri")
    out = customers.find_or_create(db, mobile="+971501234567", name="فاطمة المنصوري")["customer"]

    assert "name" not in out, "a pre-picked name is a rendering decision in the data layer"
    assert out["name_en"] == "Fatima Al Mansouri"
    assert out["name_ar"] == "فاطمة المنصوري"


def test_both_title_scripts_are_returned(db):
    """Same rule as the name: an Arabic title is written to title_ar, so it must
    be readable, or it is stored and then invisible.
    """
    customers.find_or_create(db, mobile="+971501234567", title="Mrs")
    out = customers.find_or_create(db, mobile="+971501234567", title="السيدة")["customer"]
    assert out["title"] == "Mrs"
    assert out["title_ar"] == "السيدة"
