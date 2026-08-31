"""offer_options — turn a question with a known set of answers into tappable chips.

WHY THE AGENT DECLARES THEM RATHER THAN THE UI GUESSING. The obvious cheap build
is to pattern-match Aisha's text in the browser ("does this end in a question
mark and contain 'or'?") and draw Yes/No. We have already paid for that mistake
once: the "use my current location" control keyed off a phrase in the agent's
prose and appeared on "please share your booking reference". Prose is not a
contract. It is also bilingual — every pattern would need an Arabic twin, and
RTL ordering on top.

The model, on the other hand, KNOWS what it just asked. Declaring the options is
therefore correct by construction: the chips always match the question, they are
already in the customer's language because Aisha wrote them there, and a question
with no fixed set of answers simply does not call this and gets no chips.

ONE FIELD, NOT TWO. An option is a single string that is BOTH the label and the
message sent when it is tapped. A separate "label" and "reply" would let the two
drift — a chip reading "Yes" that sends "no thanks" is a bug the customer cannot
see coming, and there is no honest way for the UI to warn about it.

CALLED ALONE, AND IT ENDS THE TURN. Same shape as close_conversation and for the
same reason: the question has already streamed as text in this very message, so
handing the model another generation afterwards buys a sentence nobody needs
stapled under the chips. Batched with other tools it is refused as data and the
siblings run normally — a model mid-sequence is not ready to offer a choice.

THE CHIPS ARE NOT THE ANSWER. Tapping one sends ordinary text, which arrives as
an ordinary message and is read the way typing it would be. Nothing downstream
knows or cares that a chip was involved, so a customer who ignores the chips and
types "the second one" is in no way worse off. This is deliberately UNLIKE the
slot chips, which pin a trusted slot_id — there is no id here to pin, and
inventing one would make the chips authoritative over the customer's own words.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

# Two is the minimum that is a CHOICE; four is where a row of chips stops being
# scannable and becomes a menu. A model that wants five options is really asking
# an open question and should just ask it.
MIN_OPTIONS = 2
MAX_OPTIONS = 4
# Long enough for "Bring the car to me instead", short enough that a chip stays a
# chip. Anything longer is a sentence and belongs in the question.
MAX_LABEL_CHARS = 40

OFFER_OPTIONS_SCHEMA = {
    "name": "offer_options",
    "description": (
        "Show the customer tappable buttons for a question whose answers you "
        "already know — so they can answer with one tap instead of typing.\n\n"
        "USE IT EVERY TIME THE ANSWER IS ONE OF A FIXED SET. Yes or no, this car "
        "or that one, showroom or doorstep, morning or evening. If you can list "
        "the sensible answers, list them here.\n\n"
        "WRITE THE QUESTION AS TEXT FIRST, IN THE SAME MESSAGE, then call this "
        "with the answers. The buttons sit under what you said; they never "
        "replace it. Never call this without asking the question in words — a "
        "row of buttons with no question is not something anyone can answer.\n\n"
        "Write the options the way the CUSTOMER would say them, in the language "
        "you are speaking, and keep them short: 'Yes', 'No', 'The petrol T2', "
        "'Bring it to me'. Whatever you write is exactly what gets sent when "
        "they tap it, so each one must read as a complete answer to your "
        "question on its own. Two to four of them, and never an option you are "
        "not genuinely willing to act on.\n\n"
        "DO NOT USE IT for questions with no fixed answer — a name, a mobile "
        "number, an address, a landmark, what day suits them, or anything you "
        "are asking them to describe. Those are open questions and buttons would "
        "only get in the way.\n\n"
        "Do not use it for times or showrooms either: those already draw their "
        "own tappable cards from the tools that fetch them, and a second set of "
        "buttons saying the same thing would be shown twice.\n\n"
        "CALL IT ON ITS OWN, as the last thing you do in a message. Never "
        "alongside another tool.\n\n"
        "The customer can always ignore the buttons and type instead, so never "
        "tell them to tap one, never say 'choose from the options below', and "
        "never treat a typed answer as anything unusual."
    ),
    "input_schema": {
        "type": "object",
        "required": ["options"],
        "properties": {
            "options": {
                "type": "array",
                "minItems": MIN_OPTIONS,
                "maxItems": MAX_OPTIONS,
                "items": {"type": "string"},
                "description": (
                    "Two to four short answers, in the customer's language. Each "
                    "is sent verbatim when tapped, so each must stand alone as a "
                    "reply to the question you just asked."
                ),
            },
        },
    },
}


def _clean(options: object) -> list[str]:
    """The options, trimmed to what is renderable. Order is preserved.

    Deliberately forgiving where forgiveness is free (whitespace, a stray
    non-string, one duplicate) and strict where it is not (count, length). A
    malformed list is a UI problem, never a reason to fail the customer's turn —
    the worst outcome here is a question with no chips under it, which is exactly
    where we were before this tool existed.
    """
    if not isinstance(options, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in options:
        if not isinstance(raw, str):
            continue
        label = " ".join(raw.split())
        if not label or len(label) > MAX_LABEL_CHARS:
            continue
        key = label.casefold()
        if key in seen:  # two chips reading the same is a dead chip
            continue
        seen.add(key)
        out.append(label)
    return out[:MAX_OPTIONS]


def offer_options(
    db: Session,
    session_id: str,
    options: object = None,
    **_ignored: object,
) -> dict:
    """Echo the validated options back so the UI can draw them. Writes nothing.

    A pure presentation tool: it touches no table, holds no state and commits
    nothing. It exists to put a structured list on the tool_event stream, which
    is the one channel the browser already reads for everything it draws.
    """
    cleaned = _clean(options)
    if len(cleaned) < MIN_OPTIONS:
        return {
            "error": "not_enough_options",
            "detail": (
                f"Give between {MIN_OPTIONS} and {MAX_OPTIONS} short, distinct "
                f"answers, each at most {MAX_LABEL_CHARS} characters. Your "
                "question still stands — just carry on and let them type."
            ),
        }
    return {"offered": True, "options": cleaned}
