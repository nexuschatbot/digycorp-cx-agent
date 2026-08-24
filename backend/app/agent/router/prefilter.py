"""Layer 1: rules that decide without a model call (architecture doc §4.1).

Most turns never need the classifier. A message carrying a booking reference, or
saying "cancel my booking" in so many words, is not an ambiguous intent — it is a
fact, and paying 350ms plus a token bill to have a model confirm it would be
waste on the voice path where the latency budget is 400ms to first audio.

THE REFERENCE PATTERN IS NOT RE-INVENTED HERE. The architecture doc proposes
`(?i)td[\\s-]*\\d{4}[\\s-]*[a-z0-9]+`, which is wrong twice: customers also write
TD.2026.HTXE and TD_2026_HTXE (services/booking._normalise_ref forgives `.` and
`_` too), and the suffix is exactly four characters, not "one or more" — an
unbounded tail matches half a sentence. So this reuses booking's own normaliser:
the router and the lookup can then never disagree about what a reference is, and
a change to the format lands in one place.
"""

from __future__ import annotations

import re

from app.services.booking import _normalise_ref

# A reference is TD + 4-digit year + 4 chars, with `-`, `.`, `_`, or spaces
# between them, anywhere in the sentence. Candidates are extracted loosely and
# then CONFIRMED by booking's own normaliser, so this pattern only has to be
# generous, never correct.
_REF_CANDIDATE = re.compile(
    r"\bTD[\s\-._]*\d{4}[\s\-._]*[0-9A-Z]{4}\b",
    re.IGNORECASE,
)

# Words that name the BookingManagement journey outright, English and Gulf
# Arabic. Deliberately narrow: these must be about an existing booking, so
# "cancel" is here but "change" is not — "can I change to the 7-seater instead"
# is a TestDrive turn.
_MANAGE_TERMS = (
    "reschedule",
    "postpone",
    "cancel my booking",
    "cancel the booking",
    "cancel my test drive",
    "move my booking",
    "move my test drive",
    "change my booking",
    "change my appointment",
    "إلغاء",        # cancel
    "ألغي",         # cancel (verb)
    "تأجيل",        # postpone
    "أجل موعدي",    # postpone my appointment
    "تغيير موعدي",  # change my appointment
)


def booking_reference(message: str) -> str | None:
    """The normalised booking reference in `message`, if it carries one."""
    if not isinstance(message, str):
        return None
    for candidate in _REF_CANDIDATE.findall(message):
        ref = _normalise_ref(candidate)
        # _normalise_ref returns the input tidied-but-unaltered when it does not
        # fit the shape, so a real hit is the one that comes back punctuated.
        if ref and re.fullmatch(r"TD-\d{4}-[0-9A-Z]{4}", ref):
            return ref
    return None


# A management VERB pointed at a booking, including when the booking is only
# referred to. "cancel it" and "move it to Sunday" are unambiguous once the verb
# and the object are both present, and leaving them to the classifier measured
# badly: 0.75 and 0.60 confidence, one withheld by the switch gate and one
# silently staying put while the customer asked to move a booking.
#
# The object is what keeps this narrow. "cancel my search" and "can I change to
# the 7-seater instead" carry no booking object, so neither matches.
_MANAGE_PATTERN = re.compile(
    r"\b(cancel|reschedule|postpone|move|change)\b[^.?!]{0,24}?"
    r"\b(it|them|booking|appointment|test\s*drive|drive|slot|reservation)\b",
    re.IGNORECASE,
)


def names_manage_intent(message: str) -> bool:
    """True when the message says, in words, that it is about an existing booking."""
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    if any(term.lower() in lowered for term in _MANAGE_TERMS):
        return True
    return _MANAGE_PATTERN.search(message) is not None


# A BARE greeting — nothing else in the message. Architecture doc §4.1 step 1
# names this case and it is the most common first turn there is, so paying a
# classifier for it is the worst trade in the cascade: measured live, "Hi" cost
# 1688ms to come back `fallback` at 0.5 confidence, i.e. the slowest, most
# expensive path to "I do not know".
_GREETINGS = frozenset({
    "hi", "hii", "hey", "hello", "helo", "yo", "hiya",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "salam", "salaam", "asalam alaikum", "assalamu alaikum", "as salamu alaykum",
    "السلام عليكم", "سلام", "مرحبا", "أهلا", "اهلا", "هاي", "صباح الخير",
    "مساء الخير", "هلا",
})

DEFAULT_AGENT = "testdrive"


def is_bare_greeting(message: str) -> bool:
    """True only when the message is a greeting AND NOTHING ELSE.

    "Hi, I want to cancel my booking" is not a greeting for routing purposes —
    it is a cancellation wearing a hello, and treating it as a greeting would
    strand the customer with the wrong agent. So this matches the whole message,
    never a prefix.
    """
    if not isinstance(message, str):
        return False
    text = message.strip().strip("!.,؟?ـ").lower()
    text = " ".join(text.split())
    return text in _GREETINGS


def decide(message: str, current_agent: str | None = None) -> str | None:
    """The agent this message obviously belongs to, or None to keep going.

    Order matters: a management signal beats a greeting, so "hi, cancel my
    booking" routes on the cancellation.
    """
    if booking_reference(message) or names_manage_intent(message):
        return "bookingmanagement"
    if is_bare_greeting(message):
        # Doc §4.1: "a bare greeting -> current agent or default."
        return current_agent or DEFAULT_AGENT
    return None
