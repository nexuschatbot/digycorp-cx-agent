"""STANDING GUARD: rule 4, one question per message, and its PDPL consequence.

THE DEFECT THIS EXISTS FOR (2026-07-28). Aisha asked two separately-answerable
questions in one message, the customer replied "yes", and the agent applied that
one word to BOTH. The rule was already in the prompt and had been since the
beginning; it lost anyway, because the prompt contradicted it in five places and
its ONLY fully-quoted question exemplar was itself a compound:

    "Is there anything else I can help you with, or shall I close the chat for
     now?"

An example outweighs an instruction. That is why this file guards the EXEMPLARS
as hard as it guards the rule text, and why the regex below is the assertion that
actually matters: a future prompt pass can restate rule 4 perfectly and still
reintroduce the bug by writing one compound question into a section it was tidying.

WHY THIS IS SUBSTRING ASSERTIONS AND NOT BEHAVIOUR. The suite is offline by
construction (CLAUDE.md): agent tests inject a ScriptedModel, so nothing here can
show that the model asks once. This pins the INSTRUCTIONS. The behavioural
evidence is the manual rehearsal recorded in the plan, answering every prompt with
a bare "yes" and reading the tool trace.

THE PDPL HALF IS THE SHARP END. record_consent's own schema used to say to ask for
messaging consent "as part of the recap" — an explicit instruction to fuse the
recap-confirmation question with the consent question. A "yes" confirming the
booking details was then filed as WhatsApp consent, which writes a consent record
standing as proof of something that never happened, and it is indistinguishable
afterwards from a real one. test_consent_schema_no_longer_fuses_consent_into_the_recap
pins the exact removed phrase, and test_an_ambiguous_yes_fails_safe_to_no_consent
pins the tie-break that makes the position defensible rather than hopeful.
"""

from __future__ import annotations

import json
import re

from app.agent.prompt import SYSTEM_PROMPT, system_prompt
from app.tools import TOOL_SCHEMAS
from app.tools.customer_tools import RECORD_CONSENT_SCHEMA
from app.tools.slot_tools import (
    ACKNOWLEDGE_LICENCE_REMINDER_SCHEMA,
    GET_AVAILABLE_SLOTS_SCHEMA,
)

# Reused rather than redefined: the same Latin+Arabic question-mark proxy the
# discovery/personalisation guard already uses.
from tests.test_discovery_and_personalisation import count_questions

_P = system_prompt()
_B = SYSTEM_PROMPT.lower()

_CONSENT_DESC = RECORD_CONSENT_SCHEMA["description"]
_MESSAGING_DESC = RECORD_CONSENT_SCHEMA["input_schema"]["properties"]["messaging"][
    "description"
]
_LOCATION_DESC = GET_AVAILABLE_SLOTS_SCHEMA["input_schema"]["properties"][
    "location_type"
]["description"]
_LICENCE_DESC = ACKNOWLEDGE_LICENCE_REMINDER_SCHEMA["description"]


# --------------------------------------------------------------------------- #
# Rule 4 itself
# --------------------------------------------------------------------------- #
def test_rule_4_states_one_question_per_message() -> None:
    assert "one question per message" in _B


def test_rule_4_gives_the_separability_test() -> None:
    """The test that tells a banned double from a legitimate either/or.

    Without it the rule is a slogan: "one question" reads as "one question mark"
    and bans "Al Quoz or Deira?", which is one question with two answers and is
    exactly what a good salesperson says.
    """
    assert "answer one half and leave the other half" in _B


def test_the_choice_carve_out_survives() -> None:
    """A later 'tighten the prompt' pass must not delete the carve-out.

    Deleting it is the plausible over-correction: the prompt would then forbid
    offering two branches or two dayparts in one breath, which is not this bug
    and would make every slot conversation longer for nothing.
    """
    assert "one question with two answers to choose between" in _B


def test_rule_4_forbids_choosing_which_question_a_bare_yes_answered() -> None:
    assert "you do not get to decide which one it answered" in _B
    assert "silence about the other thing is not agreement" in _B


def test_rule_4_names_bare_answers_in_both_scripts() -> None:
    """Arabic bare answers must be listed too, or the rule only holds in English.

    S-19 switches language mid-conversation, so the ambiguous "yes" arrives as
    "نعم" just as often as it arrives as "yes".
    """
    for token in ('"yes"', '"no"', '"sure"', '"ok"', '"please do"'):
        assert token in _B, token
    for token in ("نعم", "لا", "أكيد", "تمام"):
        assert token in _P, token


def test_rule_4_carries_the_recovery() -> None:
    """The half most likely to be dropped by a shortening pass.

    Forbidding the guess without saying what to do instead leaves the model with
    a rule it can only obey by stalling.
    """
    assert "is that a yes to the whatsapp updates" in _B
    assert "do not settle it silently" in _B


def test_rule_4_sits_with_the_flow_rules_not_the_format_section() -> None:
    """Placement, for the same reason test_conversation_recovery.py asserts one.

    This is a rule about what a turn CONTAINS, not about how text is marked up.
    Filed under OUTPUT FORMAT it reads as typography, which is how the existing
    "a question is a whole turn" paragraph came to be about ask-then-ANSWER only
    and silent on ask-then-ASK.
    """
    assert _P.index("ONE QUESTION PER MESSAGE") < _P.index("OUTPUT FORMAT")


def test_rule_4_prose_writes_no_dash_punctuation() -> None:
    """The prompt teaches output shape by its own shape (rule 17).

    New prose that models a forbidden character teaches the model to emit it.
    """
    section = _P[_P.index("HOW YOU TALK (rule 4)") : _P.index("TWICE IS THE LIMIT")]
    assert "—" not in section
    assert "–" not in section


# --------------------------------------------------------------------------- #
# The exemplar guard — the actual root cause
# --------------------------------------------------------------------------- #

# The ONE compound question the prompt is allowed to contain, because it is cited
# as the failure rather than offered as a model. Same reasoning as rule 17's
# literal markup tokens: citing a forbidden form is instruction, using it is
# example. See the DO-NOT-CLEAN comment at the top of app/agent/prompt.py.
_ALLOWED_COMPOUND_EXEMPLARS = {
    "shall I go ahead and book it, and are you happy for us to WhatsApp you?",
}

_QUOTED_QUESTION = re.compile(r'"([^"\n]{3,140}[?؟])"')


def test_no_compound_question_exemplar_in_the_prompt() -> None:
    """No quoted question in the prompt may model two asks in one message.

    KNOWN LIMIT, stated so it is not mistaken for proof: this catches the two
    shapes that actually shipped, a comma-or ("...with you, or shall I...") and
    an and-join ("...book it, and are you happy..."), plus any quote carrying two
    question marks. A compound built as a bare "X or Y?" across two clauses would
    slip through, and no cheap pattern separates that from the legitimate
    "Al Quoz or Deira?". This is a tripwire on the regression we had, not a
    decision procedure for English.
    """
    for question in _QUOTED_QUESTION.findall(_P):
        if question in _ALLOWED_COMPOUND_EXEMPLARS:
            continue
        assert ", or " not in question, f"compound question exemplar: {question!r}"
        assert " and " not in question, f"compound question exemplar: {question!r}"
        assert count_questions(question) == 1, (
            f"exemplar asks more than once: {question!r}"
        )


def test_the_deliberate_negative_exemplar_is_still_labelled_as_the_failure() -> None:
    """The allowlisted compound must stay a warning, never drift into advice.

    It sits inside WHY THIS MATTERS FAR MORE THAN IT LOOKS. If a later edit moved
    it somewhere neutral it would become the thing it was written to prevent, and
    the allowlist above would silently bless it.
    """
    exemplar = next(iter(_ALLOWED_COMPOUND_EXEMPLARS))
    assert exemplar in _P
    assert _P.index("WHY THIS MATTERS FAR MORE THAN IT LOOKS") < _P.index(exemplar)
    assert _P.index(exemplar) < _P.index("A SHORT ANSWER ANSWERS ONE QUESTION")


# --------------------------------------------------------------------------- #
# Rule 9 and consent — the PDPL half
# --------------------------------------------------------------------------- #
def test_recap_and_consent_never_share_a_message() -> None:
    assert (
        "THE RECAP AND THE CONSENT ARE TWO DIFFERENT QUESTIONS AND THEY NEVER "
        "SHARE A MESSAGE." in _P
    )


def test_the_recap_message_carries_nothing_else_answerable() -> None:
    assert "CARRIES NOTHING ELSE THEY COULD ANSWER" in _P


def test_an_ambiguous_yes_fails_safe_to_no_consent() -> None:
    """The tie-break, and the reason this is defensible rather than hopeful.

    Told only "do not guess", a model facing an ambiguous yes still has to do
    SOMETHING. This makes the safe branch the determined one: an unattributable
    yes means the details, never the messaging, so the failure mode is asking
    again rather than recording a permission nobody gave.
    """
    assert "then it was about the details" in _P
    assert "record only what they answer to that" in _P


def test_the_licence_reminder_is_a_statement_not_a_question() -> None:
    """What makes it legal for the reminder to share a turn with the consent ask.

    Without this the split reads as three separate turns and the booking tail
    grows a round trip it does not need.
    """
    assert "something you TELL them and not something you ask" in _P


def test_consent_schema_no_longer_fuses_consent_into_the_recap() -> None:
    """Regression pin on the exact phrase that CAUSED the defect.

    The schema's existing "TWO SEPARATE decisions" paragraph is about messaging
    vs memory, a different axis entirely, which is why it never bit here.
    """
    assert "as part of the recap" not in _CONSENT_DESC
    assert "never folded into the recap" in _CONSENT_DESC
    assert "AFTER the customer has confirmed" in _CONSENT_DESC


def test_the_messaging_field_refuses_a_yes_that_answered_something_else() -> None:
    """Pinned on the PROPERTY, which is the last text read before the write."""
    assert "is NOT a messaging decision" in _MESSAGING_DESC


def test_no_tool_schema_anywhere_folds_anything_into_the_recap() -> None:
    """Swept across EVERY schema, not just the two we knew about.

    This is not belt-and-braces, it caught a live contradiction the targeted
    assertions missed: acknowledge_licence_reminder ALSO said to give the licence
    reminder "as part of the recap", which is exactly the turn rule 9 now requires
    to carry nothing but the recap question. A tool-local instruction beating a
    general prompt rule is a failure this codebase has already had (see the
    bookings-anchor note in app/agent/prompt.py), so the sweep is the guard, not
    the memory of which files were edited.
    """
    blob = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
    assert "part of the recap" not in blob
    assert "part of the recap" not in SYSTEM_PROMPT


def test_the_licence_schema_places_the_reminder_after_the_recap() -> None:
    assert "AFTER the customer has confirmed the recap" in _LICENCE_DESC
    assert "never inside the recap itself" in _LICENCE_DESC
    assert "carries nothing else the customer could answer" in _LICENCE_DESC


# --------------------------------------------------------------------------- #
# The other contradiction sites
# --------------------------------------------------------------------------- #
def test_in_the_same_breath_only_ever_forbids() -> None:
    """The phrase appeared twice, forbidding in one place and permitting in another.

    Rule 13 used it to authorise asking name and mobile together, twenty lines
    from the paragraph that uses it to ban combining. One phrase doing two
    opposite jobs is not a rule the model can apply.
    """
    assert _P.count("in the same breath") == 1
    assert _P.index("in the same breath") < _P.index("BRINGING IN A PERSON")


def test_a_callback_asks_for_name_and_mobile_as_one_request() -> None:
    """The carve-out is TAUGHT, not merely granted.

    Neither a name nor a number can be answered with a bare yes, so asking for
    both is one request. Stating the reason is what stops it being read as
    licence to pair up two yes/no questions elsewhere.
    """
    assert "neither of them can be answered" in _P
    assert "one request and not two" in _P


def test_personalisation_asks_once_and_offers_rather_than_asking_twice() -> None:
    assert "as an OFFER rather than a second question" in _P
    assert "There is exactly one question mark in that message." in _P


def test_the_discovery_cap_is_per_message_as_well_as_per_conversation() -> None:
    """Complements the total-budget pin in test_discovery_and_personalisation.py.

    "At most two" alone reads as a budget that may be spent in one breath, which
    is how the cap and rule 4 came to disagree.
    """
    assert "one per message and never both at once" in _B


def test_the_closing_offers_to_close_without_asking_a_second_question() -> None:
    assert "The offer to close is a sentence you write" in _P
    assert "never nudge someone towards leaving" in _P


def test_the_doorstep_choice_is_one_question_on_its_own() -> None:
    """Rule 7's only instruction lives in the schema, so the guard lives here.

    With no turn-shape guidance it fused with the date question: "showroom or
    shall we bring it to you, and what day suits?".
    """
    assert "as ONE question on its own" in _LOCATION_DESC
    assert "before you ask about days or times" in _LOCATION_DESC
