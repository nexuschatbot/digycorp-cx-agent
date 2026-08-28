"""The guardrail rewrite (Phase 6).

THE ONLY DELIBERATE RE-AUTHORING IN THE WHOLE MIGRATION. Everything else was
ported verbatim; these four changes are decisions, and each is pinned here so a
later edit that quietly reverses one fails rather than ships.

Offline: reads the assembled prompt. No model, no database.
"""

from __future__ import annotations

import pytest

from app.agents import loader


@pytest.fixture(scope="module")
def body() -> str:
    loader.reset()
    return loader.single_agent_body()


@pytest.fixture(scope="module")
def finance_body() -> str:
    return "".join(loader.sections("finance").values())


# --------------------------------------------------------------------------- #
# T6.1 — the scope changed: indicative figures are hers, commitments are not
# --------------------------------------------------------------------------- #
def test_financing_is_no_longer_forbidden_outright(body):
    """It used to read "price haggling, discounts, financing, insurance and
    trade-ins are all deals you do not make" — which, now that services/finance
    computes real figures, would have made her refuse a question she can
    answer."""
    assert "financing, insurance and trade-ins are all deals you do not make" not in body
    assert "Financing and insurance are no longer on that list" in body


def test_indicative_figures_are_explicitly_permitted(body):
    assert "You may give indicative figures" in body
    assert "on-road price or a monthly instalment" in body


def test_what_still_goes_to_a_human_is_named_precisely(body):
    """The line MOVED, it did not disappear."""
    for phrase in ("Haggling over a price", "issuing a final quotation",
                   "completing a purchase"):
        assert phrase in body, phrase


def test_she_still_guarantees_nothing(body):
    """The permission is to ESTIMATE, never to promise."""
    assert "What you never do is guarantee" in body
    # Case-insensitive: the first item is sentence-initial ("Not a discount").
    lowered = body.lower()
    for never in ("not a discount", "not a trade-in value", "not an approval",
                  "not a delivery date"):
        assert never in lowered, never


# --------------------------------------------------------------------------- #
# T6.2 — the handoff is a queue, not a phone that rings
# --------------------------------------------------------------------------- #
def test_the_handoff_may_not_promise_a_time(body):
    """Elite's own Purchase Confirmation template says "a representative will
    contact you within 1-2 hours". services/escalation.py says plainly there is
    no supervisor UI and no real callback is placed. Adopting that wording
    verbatim would have had Aisha promising a call nobody is making."""
    assert "Never name a time" in body
    assert "not a phone that rings" in body
    for forbidden in ('"within the hour"', '"shortly"', '"today"'):
        assert forbidden in body, f"{forbidden} should be named as forbidden"


def test_the_honest_version_is_supplied_not_just_the_ban(body):
    """A rule that only forbids leaves the model to invent a substitute. This
    one hands it the sentence."""
    assert "they will be in touch" in body
    assert "Someone will call you in the next hour" in body


def test_the_helpline_still_travels_with_every_handoff(body):
    """It is what makes the honest version enough: not waiting helplessly, but
    holding a way to reach us first."""
    assert "«HELPLINE»" in body


# --------------------------------------------------------------------------- #
# T6.3 — every figure carries its caveat; offers carry no date
# --------------------------------------------------------------------------- #
def test_the_caveat_travels_in_the_same_sentence_as_the_figure(body):
    """Not in a later one and not only when asked. A figure without its caveat
    is a promise she did not mean to make."""
    assert "carries its caveat in the same sentence" in body
    assert "not a quotation from a bank" in body


def test_offers_are_never_dated(body):
    """Every promotion Elite supplied is marked "Sample" with no end date. An
    invented deadline about money is the worst kind to get wrong."""
    assert "never dated" in body
    assert "never say when one starts or ends" in body.lower()


def test_a_discount_is_never_folded_into_a_quoted_price(body):
    """A number with a discount already in it is one the customer cannot
    reproduce and the invoice will not match — the same rule promotions.md and
    services/finance.py already enforce on the data side."""
    assert "never quietly subtract it from a price" in body


def test_the_finance_agent_carries_the_same_rules(finance_body):
    """The sub-agent returns the figures; it must not be looser than the prose
    that speaks them."""
    lowered = finance_body.lower()
    assert "indicative" in lowered
    assert "never state a validity date" in lowered
    assert "never present a promotion as a discount you have applied" in lowered


# --------------------------------------------------------------------------- #
# The T0.1 baseline defect
# --------------------------------------------------------------------------- #
def test_third_person_notes_to_self_are_forbidden(body):
    """FOUND BY CAPTURING TWELVE REAL CONVERSATIONS (T0.1), not by reading code.

    graph.py seals pre-tool text with a text_break, so it is DELIVERED, never
    retracted. Aisha was writing reasoning about the customer in the third
    person and it reached them verbatim:

        "They've chosen the petrol T2 (already confirmed above)."
        "The customer already said 'WhatsApp reminders are fine' two turns ago"

    The rule quotes the observed leak so the model can recognise the shape.
    """
    assert "Everything you type is delivered" in body
    assert "There is no scratchpad" in body
    assert "never write ABOUT the customer, only TO them" in body
    assert "already confirmed above" in body, "the observed leak is quoted"


def test_the_existing_tool_narration_rule_survived_the_edit(body):
    """The new paragraph sits BEFORE it and must not have replaced it — they
    forbid different things."""
    assert "Do NOT narrate your tool use" in body
    assert '"let me check"' in body


# --------------------------------------------------------------------------- #
# What the rewrite must not have broken
# --------------------------------------------------------------------------- #
def test_the_rewrite_introduced_no_em_dashes_into_aishas_rules(body):
    """Rule 17 forbids her typing them. New prose that used them would be the
    prompt contradicting itself, which is how a model learns a rule is soft."""
    for para in (
        "You may give indicative figures",
        "Financing and insurance are no longer",
        "Be careful what you promise",
        "Every money figure you say",
        "Offers are named, never dated",
        "Everything you type is delivered",
    ):
        start = body.index(para)
        end = body.find("\n\n", start)
        chunk = body[start:end if end != -1 else len(body)]
        assert "—" not in chunk and "–" not in chunk, para


def test_the_load_bearing_guardrails_all_survived(body):
    """The activation gate checks these on every boot; asserting them here too
    means a failure names WHICH rule went, not just that one did."""
    lowered = body.lower()
    for rule in ("record_consent", "licence", "one question", "markdown"):
        assert rule in lowered, rule


def test_the_rewrite_did_not_reintroduce_a_phrase_that_does_two_jobs(body):
    """CAUGHT BY AN EXISTING TEST, ON ME.

    tests/test_one_question_per_turn.py pins that "in the same breath" appears
    exactly ONCE and only where it FORBIDS combining two questions. Its docstring
    records why: the phrase had previously appeared twice, "forbidding in one
    place and permitting in another", and one phrase doing two opposite jobs is
    not a rule a model can apply.

    The Phase 6 rewrite used it permissively twice — once to authorise saying a
    caveat alongside a figure, once to authorise giving the helpline alongside a
    handoff. Both were reworded. This test is here so the next person editing
    this prose learns it from the file they are editing, rather than from a
    failure three test files away.
    """
    assert body.count("in the same breath") == 1
    assert body.index("in the same breath") < body.index("BRINGING IN A PERSON")
