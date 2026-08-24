"""Customer identity service (LOC-09, PDPL).

The entry point to every booking: a customer is identified by their +971 mobile
and nothing else. All DB access for customers lives here (AD-I).

Two rules run through this module:

1. NEVER OVERWRITE STORED DATA FROM A CONVERSATION. A name arriving in chat is
   an assertion, not a fact — anyone typing "I'm Mohammed" against Ravi's number
   must not rewrite Ravi's stored profile. Chat can only FILL a field that is
   currently empty (see _fill_if_empty).
2. Errors are data, never exceptions. An unparseable mobile returns a dict the
   agent can act on ("could you read that back to me?") — a raised exception
   would kill the turn.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import Customer
from app.services import memory

# --------------------------------------------------------------------------- #
# +971 mobile validation (LOC-09)
# --------------------------------------------------------------------------- #
# The UAE mobile prefixes in service. A number outside this set is not a mobile,
# however well-formed it looks — we cannot WhatsApp a landline, so accepting one
# would produce a booking whose confirmation silently never arrives.
MOBILE_PREFIXES: frozenset[str] = frozenset({"50", "52", "54", "55", "56", "58"})

# UAE landline area codes, recognised ONLY to explain the rejection precisely.
# Never accepted.
LANDLINE_PREFIXES: frozenset[str] = frozenset({"02", "03", "04", "06", "07", "09"})

# Digits after the country code: 9 (prefix 2 + subscriber 7).
NATIONAL_DIGITS = 9

# Digits after the trunk 0 in a UAE landline: 8 (area 1 + subscriber 7). Used
# ONLY by the relaxed path below, to tell a real landline ("04 222 1234") from a
# foreign mobile that merely happens to start with a UAE area-code digit — every
# Pakistani mobile is 03xx, which collides with Al Ain's 03 exactly.
LANDLINE_DIGITS = 8

# Separators a human sprinkles through a phone number. Stripped, never rejected:
# "+971 50 123 4567" is the SAME number as "+971501234567", and refusing it
# would be us failing to read, then blaming the customer.
_SEPARATORS = re.compile(r"[\s\-(). ‎‏]")

# Arabic-Indic digits -> ASCII. NOT optional for a bilingual demo: QR attendees
# type on their own phones, and an Arabic keyboard produces ٠٥٠١٢٣٤٥٦٧ for the same
# number an English one writes 0501234567. Rejecting that would look, to the
# customer, exactly like the agent cannot read Arabic (LOC-01, S-20).
#
# U+0660-U+0669 is Arabic-Indic (Gulf/Arabic keyboards) — the range that matters.
# U+06F0-U+06F9 is EXTENDED Arabic-Indic (Persian/Urdu), out of scope for the UAE
# but free to accept: it is unambiguously the same digit, and a Farsi- or
# Urdu-configured keyboard is not implausible in Dubai. Translated BEFORE any
# validation, so every rule below sees one canonical digit set.
_ARABIC_INDIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩" "۰۱۲۳۴۵۶۷۸۹",
    "0123456789" "0123456789",
)

# Arabic script range — used to route a name to name_ar vs name_en. Same rule as
# the frontend's per-bubble RTL detection (CLAUDE.md), so one script test governs
# the whole stack.
_ARABIC = re.compile(r"[؀-ۿ]")


def _invalid(mobile: object, reason: str, hint: str) -> dict:
    """Shape every rejection identically: a machine-readable `reason` the agent
    can branch on, plus a `hint` explaining what to do. The agent must PHRASE the
    hint in the customer's language, never read it out — it is guidance to the
    model, not customer-facing copy (LOC-01).
    """
    return {
        "error": "invalid_uae_mobile",
        "reason": reason,
        "received": mobile if isinstance(mobile, str) else None,
        "hint": hint,
    }


def normalise_mobile(mobile: object) -> dict:
    """Normalise a UAE mobile to canonical +9715XXXXXXXX form.

    Returns {"mobile": "+971501234567"} or an `invalid_uae_mobile` dict.

    Normalises what is UNAMBIGUOUSLY normalisable and rejects the rest. The line
    is drawn at "could this be a different number?" — a missing +971 or a leading
    0 has exactly one reading (a local mobile prefix never starts with 97, and no
    UAE number legitimately begins 0 after the country code), so those are read
    rather than refused. A number with the wrong digit count has no single
    correct reading, so guessing at it could book a stranger's phone.

    WITH STRICT_UAE_MOBILE=false THAT LINE MOVES, deliberately: a number carrying
    a country code is taken as E.164, and a BARE one is read as UAE and accepted
    whatever its shape. Both relaxations exist to stop the agent interrogating a
    number instead of booking a drive — see the two blocks below for the trade
    each one makes.
    """
    if not isinstance(mobile, str) or not mobile.strip():
        return _invalid(mobile, "missing", "No mobile number was given. Ask for it.")

    # Arabic-Indic digits first: ٠٥٠١٢٣٤٥٦٧ is the same number as 0501234567, and
    # every rule below should only ever have to reason about one digit set.
    s = _SEPARATORS.sub("", mobile.strip().translate(_ARABIC_INDIC_DIGITS))

    # INTERNATIONAL RELAX (demo, STRICT_UAE_MOBILE=false). A number the customer
    # wrote WITH a country code (+.. or 00..) is accepted as E.164 so a non-UAE
    # test number (e.g. +92 Pakistan) can be identified and messaged in the
    # sandbox. Read the flag at call time so a .env change (or a test) takes
    # effect without re-import. A bare local number (no country code) is NOT
    # caught here — it falls through to the UAE rules below, so +971 behaviour is
    # unchanged whether the flag is on or off.
    if not config.STRICT_UAE_MOBILE:
        if s.startswith("+"):
            intl = s[1:]
        elif s.startswith("00"):
            intl = s[2:]
        else:
            intl = None
        if intl is not None:
            if not intl.isdigit():
                return _invalid(
                    mobile,
                    "not_a_number",
                    "That is not a phone number. Ask the customer to repeat it.",
                )
            # E.164 allows up to 15 digits including the country code; require a
            # plausible minimum so a stray "+1" is still rejected.
            if not (8 <= len(intl) <= 15):
                return _invalid(
                    mobile,
                    "too_short" if len(intl) < 8 else "too_long",
                    "That does not look like a full international number "
                    "(country code + number). Ask the customer to read it back.",
                )
            return {"mobile": f"+{intl}"}

    # Country code, in any of the forms people actually type it.
    if s.startswith("00971"):
        national = s[5:]
    elif s.startswith("+971"):
        national = s[4:]
    elif s.startswith("971"):
        national = s[3:]
    else:
        national = s

    # A leading 0 is the national trunk prefix ("050..."), redundant once the
    # country code is explicit. Strip it in both spellings, incl. "+971 050...".
    if national.startswith("0"):
        national = national[1:]

    if not national.isdigit():
        return _invalid(
            mobile,
            "not_a_number",
            "That is not a phone number. Ask the customer to repeat it.",
        )

    prefix = national[:2]

    # Check the landline BEFORE the length rule: "04 222 1234" is wrong because
    # it is a landline, not because of how many digits it has, and the agent
    # should say the useful thing ("is there a mobile we can WhatsApp?"). No
    # overlap with MOBILE_PREFIXES is possible — every mobile prefix starts 5.
    #
    # THE LENGTH CLAUSE APPLIES ONLY WHEN RELAXED, and strict behaviour is
    # therefore byte-identical (the flag short-circuits the `or`). Relaxed, the
    # area-code digit alone is not enough to call something a landline: "03409297758"
    # is a Pakistani mobile whose first digit is Al Ain's area code, and refusing it
    # as a landline is the exact misreading this whole change exists to remove.
    if f"0{national[:1]}" in LANDLINE_PREFIXES and (
        config.STRICT_UAE_MOBILE or len(national) == LANDLINE_DIGITS
    ):
        return _invalid(
            mobile,
            "landline",
            "That is a UAE landline, not a mobile. We can only send the "
            "confirmation by WhatsApp, so ask for a mobile number.",
        )

    # BARE LOCAL NUMBER, RELAXED (demo, STRICT_UAE_MOBILE=false). A number typed
    # with no country code is READ AS UAE and accepted as it stands — the mobile
    # prefix and the 9-digit rule are both dropped. The point is what the agent
    # must NOT do: asking "which country is that?" is a question the customer
    # cannot be expected to answer about their own number, and it stalled every
    # booking made from a non-UAE handset. A number we cannot place is still a
    # number the customer gave us, so we take it rather than interrogate it.
    #
    # The cost is accepted deliberately (developer decision 2026-08-04): +971
    # in front of a foreign number is not a reachable UAE mobile, so a real
    # WhatsApp to it will not arrive. On the demo stack WhatsApp is a simulator,
    # and identification — not delivery — is what a bare number is needed for.
    # Setting STRICT_UAE_MOBILE=true restores the old rules in one env line.
    #
    # Only the E.164 ceiling is still enforced, and it is the SAME bound the
    # international branch above applies, so there is one length rule here, not
    # two dialects of one.
    if not config.STRICT_UAE_MOBILE:
        digits = len(national) + 3  # "971"
        if not (8 <= digits <= 15):
            return _invalid(
                mobile,
                "too_short" if digits < 8 else "too_long",
                f"That is {len(national)} digits, which is not a plausible phone "
                "number. Ask the customer to read it back — do not guess the "
                "missing digits.",
            )
        return {"mobile": f"+971{national}"}

    if len(national) != NATIONAL_DIGITS:
        return _invalid(
            mobile,
            "too_short" if len(national) < NATIONAL_DIGITS else "too_long",
            f"A UAE mobile has {NATIONAL_DIGITS} digits after +971 (e.g. "
            f"+971 50 123 4567); this has {len(national)}. Ask the customer to "
            "read the number back — do not guess the missing digits.",
        )

    if prefix not in MOBILE_PREFIXES:
        return _invalid(
            mobile,
            "unknown_prefix",
            f"'{prefix}' is not a UAE mobile prefix "
            f"({', '.join(sorted(MOBILE_PREFIXES))}). Ask the customer to check "
            "the number.",
        )

    return {"mobile": f"+971{national}"}


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _customer_dict(c: Customer) -> dict:
    """Serialise a customer for a tool result.

    Consent is exposed under the tool contract's names (consent_messaging /
    consent_memory), not the column names (whatsapp_consent / memory_consent):
    the agent reasons about "may I message them", and the transport being
    WhatsApp is our implementation detail.

    Deliberately does NOT carry the memory profile. That is not a phasing note
    any more — it is the boundary. Memory arrives as a SIBLING key on the
    envelope (`{"customer": ..., "created": ..., "memory": ...}`), never inside
    this dict, because it has a consent gate this dict does not: everything here
    is returned to any caller who identified the customer, and the profile is
    returned only with recorded consent (FR-MEM). Folding it in would put two
    different gates behind one key.

    NO SINGLE "name" FIELD, deliberately. Both scripts are returned and the agent
    picks per turn. A pre-picked name would have to choose from language_pref —
    a STORED preference — while the agent replies in the language of the CURRENT
    turn (FR-CX-09), so it would be wrong every time a customer code-switches
    (S-19). It is also the display-text bug in disguise: a rendering decision
    made in the data layer.
    """
    return {
        "customer_id": c.id,
        "name_en": c.name_en,
        "name_ar": c.name_ar,
        "title": c.title,
        "title_ar": c.title_ar,
        "mobile": c.mobile,
        "language_pref": c.language_pref.value if c.language_pref else None,
        "consent_messaging": bool(c.whatsapp_consent),
        "consent_memory": bool(c.memory_consent),
    }


def _envelope(db: Session, customer: Customer, *, created: bool) -> dict:
    """The find_or_create return shape, with memory attached when there is any.

    The `memory` key is OMITTED rather than set to None when there is nothing to
    recall. An absent key is unambiguous; `"memory": null` on every new customer
    is tokens the model may still find a way to narrate ("I don't have anything
    on file for you") — which is precisely the volunteering rule 12 forbids.
    """
    envelope = {"customer": _customer_dict(customer), "created": created}
    profile = memory.recall(db, customer_id=customer.id)
    if profile is not None:
        envelope["memory"] = profile
    return envelope


def envelope_for(db: Session, customer: Customer) -> dict:
    """The find_or_create envelope for a customer we resolved some OTHER way.

    Public because the booking-management journey identifies by TD reference or
    mobile rather than by creating anyone (services/booking.identify, 2026-08-05),
    and it must hand the agent the SAME shape find_or_create returns — same
    `customer` dict, same consent-gated `memory` sibling key, same omission when
    there is nothing to recall. Two identification paths returning two shapes
    would be two things for the prompt to learn, and the memory gate would then
    live in two places.

    `created` is always false here by construction: this path resolves an
    EXISTING customer and never mints one.
    """
    return _envelope(db, customer, created=False)


def _normalise_name(value: str) -> str:
    """A name reduced to its comparable core: outer/inner whitespace collapsed and
    case folded. "  Ravi   SHARMA " and "ravi sharma" compare equal; "Ravi" does
    not equal "Ravi Sharma" — the match is FULL-name (developer decision
    2026-07-23), so a first name alone is deliberately not enough."""
    return " ".join(value.split()).casefold()


def _has_stored_name(customer: Customer) -> bool:
    """True iff we hold a name to check an assertion against. A record created by
    number alone has nothing to contradict, so name verification is vacuous for it
    (a genuinely nameless customer also holds no bookings to protect)."""
    return bool(
        (customer.name_en and customer.name_en.strip())
        or (customer.name_ar and customer.name_ar.strip())
    )


def _name_matches(customer: Customer, supplied: str) -> bool:
    """Does the supplied name match EITHER stored script? A customer may give
    their name in Arabic on one visit and Latin on another, so name_en and name_ar
    are both acceptable targets — matching one is enough."""
    target = _normalise_name(supplied)
    for stored in (customer.name_en, customer.name_ar):
        if stored and _normalise_name(stored) == target:
            return True
    return False


def _verify_name(customer: Customer, supplied: object) -> dict | None:
    """The name+number second factor (developer decision 2026-07-23, D1 PDPL).

    Knowing a number is not proof of identity — anyone can guess or mistype one.
    Before a session is bound to an EXISTING, NAMED customer, the caller must also
    supply the matching FULL name; otherwise a wrong number lands on a stranger's
    profile and, downstream, their bookings. Returns None when identity is
    confirmed (or there is nothing to confirm against), else an error dict and NO
    binding happens.

    TWO distinct failures, because the agent must react differently:
      - name_required      -> we hold a name, none was given: ASK for the full name.
      - identity_unconfirmed -> a name was given and it does NOT match: the honest
        answer to the customer is "no booking found" — it must NOT reveal whether
        the number exists (developer decision: mismatch reads as not-found).
    """
    if not _has_stored_name(customer):
        return None
    if not (isinstance(supplied, str) and supplied.strip()):
        return {
            "error": "name_required",
            "detail": (
                "Ask for the customer's FULL name to confirm their identity before "
                "continuing — a number alone is not enough."
            ),
        }
    if not _name_matches(customer, supplied):
        return {
            "error": "identity_unconfirmed",
            "detail": (
                "The name does not match our records for this number. Tell the "
                "customer honestly that no booking was found under those details; "
                "do NOT reveal whether the number itself is on file, and offer to "
                "re-check the name and number or arrange a human follow-up."
            ),
        }
    return None


def _fill_if_empty(customer: Customer, *, name: str | None, title: str | None) -> list[str]:
    """Fill blank profile fields from the conversation. Returns fields filled.

    ONLY fills what is empty. A stored value is data we already hold; a chat
    value is an assertion by whoever is typing. Overwriting the first with the
    second means anyone who knows a mobile number can rewrite that customer's
    profile by claiming a different name.

    Script decides the column: Arabic input lands in name_ar, everything else in
    name_en, so the name is stored in the script the customer actually wrote it
    in and never machine-translated (LOC-01).
    """
    filled: list[str] = []
    if isinstance(name, str) and name.strip():
        value = name.strip()
        field = "name_ar" if _ARABIC.search(value) else "name_en"
        if not getattr(customer, field):
            setattr(customer, field, value)
            filled.append(field)
    if isinstance(title, str) and title.strip():
        value = title.strip()
        field = "title_ar" if _ARABIC.search(value) else "title"
        if not getattr(customer, field):
            setattr(customer, field, value)
            filled.append(field)
    return filled


# --------------------------------------------------------------------------- #
# find_or_create — the entry point to every booking
# --------------------------------------------------------------------------- #
def _return_existing(
    db: Session,
    existing: Customer,
    *,
    name: str | None,
    title: str | None,
    verify_name: bool,
) -> dict:
    """Resolve an already-existing customer: verify identity if asked, then fill
    any blank fields and return the envelope. Shared by the found-early path and
    the create-race loser so the name gate applies identically to both."""
    if verify_name:
        gate = _verify_name(existing, name)
        if gate is not None:
            return gate  # NO binding — the caller stays unidentified
    if _fill_if_empty(existing, name=name, title=title):
        db.flush()
    return _envelope(db, existing, created=False)


def find_or_create(
    db: Session,
    *,
    mobile: str,
    name: str | None = None,
    title: str | None = None,
    verify_name: bool = False,
    require_name_for_create: bool = False,
) -> dict:
    """Identify a customer by mobile, creating them if new. Never raises.

    `verify_name` (developer decision 2026-07-23) turns on the name+number second
    factor: when set, an EXISTING customer with a stored name is only returned
    (and the session only bound) if `name` matches it — a wrong number can no
    longer reach a stranger's profile. OFF by default, so trusted callers (the
    CLI seed, the WhatsApp handoff which is already server-identified) are
    unchanged; the agent tool passes it True. A NEW customer and a nameless
    existing one have nothing to match, so verification is a no-op for them.

    IDEMPOTENT by the mobile's uniqueness: called twice with the same number —
    in any format — it returns the same customer, never a duplicate. `mobile` is
    UNIQUE in the schema, so the race between two concurrent creates is settled
    by the database, not by our check-then-insert (which is exactly the pattern
    that loses that race). We resolve the violation as DATA: whoever lost simply
    re-reads the row the winner wrote.

    THE ONLY PLACE MEMORY IS RECALLED (FR-MEM, rule 12). A consented, non-empty
    profile arrives as a `memory` sibling key; the key is OMITTED entirely when
    there is nothing to recall, so its presence alone means "we remember this
    person" and no caller has to test for null. Recall rides here rather than on
    a tool of its own precisely so the agent cannot go looking for something to
    show off with — see services/memory.py.
    """
    normalised = normalise_mobile(mobile)
    if "error" in normalised:
        return normalised
    canonical = normalised["mobile"]

    existing = db.scalar(select(Customer).where(Customer.mobile == canonical))
    if existing is not None:
        return _return_existing(db, existing, name=name, title=title, verify_name=verify_name)

    # A NEW customer needs a name when the caller says so (the agent tool
    # does; trusted callers — seed CLI, WhatsApp handoff — do not). Without
    # this, a model that skipped the name question created a NAMELESS
    # customer and booked a drive for "—" (live 2026-07-31, on the voice
    # call). Error-as-data: the agent recovers by asking. Lookups above are
    # untouched — an existing customer is legitimately reachable by mobile
    # alone (FR-RS-01), and verify_name already guards the named ones.
    if require_name_for_create and not (isinstance(name, str) and name.strip()):
        return {
            "error": "name_required",
            "detail": (
                "This number is new to us, and a booking needs a name. Ask "
                "for the customer's full name, then call this again with "
                "both the name and the number."
            ),
        }

    customer = Customer(mobile=canonical)
    _fill_if_empty(customer, name=name, title=title)
    try:
        # SAVEPOINT, not a bare add(): if the INSERT violates the unique index we
        # must undo ONLY this insert. Letting an IntegrityError reach the
        # caller's transaction would poison it, taking the booking down with it.
        with db.begin_nested():
            db.add(customer)
            db.flush()
    except IntegrityError:
        # Someone else created this number between our SELECT and our INSERT.
        # That is not an error — it is the answer to the question we asked.
        existing = db.scalar(select(Customer).where(Customer.mobile == canonical))
        if existing is None:  # pragma: no cover — violation from something else
            raise
        return _return_existing(db, existing, name=name, title=title, verify_name=verify_name)

    return _envelope(db, customer, created=True)
