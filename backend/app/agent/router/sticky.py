"""Layer 2: a follow-up belongs to whoever asked the question (doc §4.1).

"yes", "ok", "3pm", "تمام", "the second one" carry no intent of their own — they
are the answer to something the ACTIVE agent just asked. Sending them to a
classifier is worse than pointless: the classifier sees a message with no signal
and has to guess, and a guess here switches agents mid-question, which is the
exact failure the sticky rule exists to prevent.

So the rule is arithmetic, not judgement: if an agent is active and the message
carries no journey signal of its own, stay. No model call, no latency, no cost.

WHY THIS IS CHECKED AFTER THE PRE-FILTER. A short message CAN carry a hard
signal — "cancel" is one word. The pre-filter runs first so those still route.
"""

from __future__ import annotations

import re

# Answers to a closed question. Never an intent.
_BARE_ANSWERS = frozenset({
    "yes", "yeah", "yep", "yes please", "ok", "okay", "sure", "fine", "alright",
    "no", "nope", "no thanks", "not now",
    "please", "thanks", "thank you", "great", "perfect", "good",
    "نعم", "أيوه", "ايوه", "تمام", "طيب", "أكيد", "اكيد", "ماشي",
    "لا", "لأ", "شكرا", "شكراً", "ممتاز",
})

# A time, a date fragment, a bare number, an ordinal — an answer to "when?" or
# "which one?".
_ANSWER_SHAPES = (
    re.compile(r"^\d{1,2}\s*(:\d{2})?\s*(am|pm)?$", re.I),      # 3, 3pm, 15:30
    re.compile(r"^\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?$"),           # 12/9
    re.compile(r"^(first|second|third|the first|the second|the third)$", re.I),
    re.compile(r"^(morning|afternoon|evening|صباح|مساء)$", re.I),
)

_MAX_WORDS = 4

# A message whose subject is a REFERRING EXPRESSION cannot be about anything but
# what came before — "it", "that one", "them" have no antecedent of their own.
# Length is a crude proxy for this and gets it wrong: measured live, "Tell me
# more about it" is five words, missed the cap by one, and paid 1357ms to
# conclude TestDrive was still TestDrive.
_REFERRING = (
    r"\bit\b", r"\bthem\b", r"\bthose\b", r"\bthese\b",
    r"\bthat one\b", r"\bthis one\b", r"\bthe same\b",
    r"\bعنه\b", r"\bعنها\b", r"\bنفسه\b", r"\bذلك\b",
)
_REFERRING_RE = re.compile("|".join(_REFERRING), re.IGNORECASE)

# ...WITH ONE EXCEPTION, AND IT IS THE WHOLE REASON THIS IS NOT JUST A PRONOUN
# CHECK. "cancel it" and "move it to Sunday" both refer backwards AND change
# journey. Staying put on those would keep a customer in TestDrive while they
# ask to cancel — a referring expression tells you the message is a CONTINUATION,
# not that the journey is unchanged. So an unambiguous management verb vetoes
# stickiness and the message goes to the classifier, which is exactly the kind of
# call it should be paid for.
_JOURNEY_VERBS = re.compile(
    r"\b(cancel|reschedule|postpone|move|refund)\b|إلغاء|ألغي|تأجيل|أجل",
    re.IGNORECASE,
)


def is_followup(message: str) -> bool:
    """True when the message only makes sense as a reply to the active agent."""
    if not isinstance(message, str):
        return False
    text = message.strip().strip(".!،؟?").lower()
    if not text:
        return True
    if text in _BARE_ANSWERS:
        return True
    if any(shape.match(text) for shape in _ANSWER_SHAPES):
        return True
    # An unambiguous journey verb is never a mere follow-up, however short or
    # however much it refers backwards. Let the classifier earn its keep.
    if _JOURNEY_VERBS.search(message):
        return False
    # Refers backwards -> it is a continuation, whatever its length.
    if _REFERRING_RE.search(message):
        return True
    # Very short and carrying no verb of its own: a day name, a branch name, a
    # model name. Whoever asked the question knows what it answers.
    return len(text.split()) <= _MAX_WORDS and "?" not in message


def decide(message: str, current_agent: str | None) -> str | None:
    """Stay with `current_agent`, or None to keep going down the cascade."""
    if not current_agent:
        return None
    return current_agent if is_followup(message) else None
