"""Three live failures from the 2026-07-27 chat review, all prompt-layer (7, 10, 12).

What links them: in each case the agent had everything it needed and still gave a
worse answer than the data supported.

  #7  "give me 7 seater options" was answered by sifting the three cars already on
      screen -> "the X70 FL is the one we have in a 7-seater configuration". The
      catalogue has TWO (X70 FL 7-7 and X70 Plus 5-7), and catalogue._matches
      filters min_seats on seats_max precisely so the X70 Plus satisfies "I need
      7". A real search returns both. This was a false claim about the catalogue,
      i.e. a rule 2 grounding failure produced without any tool being wrong.

  #10 Two consecutive misunderstandings, no human offered. The rule existed but
      was buried in BRINGING IN A PERSON, several screens below the flow rules,
      and it never fired.

  #12 "I can only hold one slot at a time" — the lock() auto-release invariant
      narrated to the customer. The prompt already forbade explaining limits; what
      it never said was that two drives in one conversation are NORMAL, which is
      probably why the model reached for an excuse.

OFFLINE ONLY (CLAUDE.md): prompt string assertions plus the real catalogue service
against the in-memory DB. No model, no vendor call.
"""

from __future__ import annotations

from app.agent.prompt import system_prompt
from app.services import catalogue

_P = system_prompt()


# --------------------------------------------------------------------------- #
# 7 — a narrowing request is a new search
# --------------------------------------------------------------------------- #
def test_prompt_forbids_filtering_the_cars_already_on_screen():
    assert "A NARROWING REQUEST IS A NEW SEARCH, NOT A FILTER OF THE LAST ONE." in _P
    assert "Do NOT sift the cars already on screen" in _P
    # The reason has to be in there too: the three were one-per-category, so the
    # match usually is NOT among them. Without the why this reads as a style note.
    assert "one per category" in _P


def test_a_real_seven_seater_search_returns_both_cars(db):
    """The claim "the X70 FL is the one we have" is false against the catalogue.

    Pinned as a service fact, not just a prompt string: if the seed or the filter
    ever changed so that only one seven-seater matched, the prompt rule above
    would be right for the wrong reason and this test should be what tells us.
    """
    out = catalogue.search(db, min_seats=7)
    names = {m["name"] for m in out["matches"]}
    assert "Jetour X70 FL" in names
    assert "Jetour X70 FL Plus" in names, (
        "min_seats filters on seats_max, so the X70 FL (5-7) satisfies 'I need 7' "
        "alongside the X70 FL Plus (7-7) — the agent claiming a single "
        "seven-seater is a grounding error"
    )


# --------------------------------------------------------------------------- #
# 10 — two misunderstandings, then a person
# --------------------------------------------------------------------------- #
def test_two_failures_trigger_a_human_from_the_flow_rules():
    assert "TWICE IS THE LIMIT, THEN YOU FETCH A PERSON" in _P
    assert "request_human_callback" in _P
    # It must forbid a THIRD attempt explicitly — "offer a person" alone leaves
    # trying again as a legal reading.
    assert "Do not attempt a third reading" in _P

    # Placement is the fix: the rule already existed in BRINGING IN A PERSON and
    # never fired. It has to sit in the conversation-flow rules, well above the
    # escalation section, or it is buried again.
    assert _P.index("TWICE IS THE LIMIT") < _P.index("BRINGING IN A PERSON")


# --------------------------------------------------------------------------- #
# 12 — two drives in one conversation are normal, and silent
# --------------------------------------------------------------------------- #
def test_prompt_normalises_a_second_booking_and_forbids_narrating_the_limit():
    assert "TWO TEST DRIVES IN ONE CONVERSATION ARE PERFECTLY NORMAL" in _P
    assert "ONE AT \\\nA TIME" in _P or "ONE AT A TIME" in _P.replace("\n", " ")
    # The specific sentence that reached a customer, forbidden by name.
    assert "you can only hold one slot" in _P
    assert "will not let you" in _P


def test_the_second_booking_carries_context_forward():
    """Rule 4 in the multi-booking case: the failure was re-asking for a time the
    customer had already chosen once the first booking completed."""
    assert "carry across everything they have already told you rather than asking again" in _P


# --------------------------------------------------------------------------- #
# A bare booking intent, and the invented "popular" framing (2026-07-27, both
# found by comparing two fresh sessions given the SAME opening message).
#
# The same "I'd like to book a test drive." produced two different BEHAVIOURS:
# one session called search_vehicles and put three cars on screen, the other
# asked which model they had in mind. Rule 5 covered a stated NEED, an open
# "what do you have?", and a NAMED model — but not a bare intent naming neither,
# so the model chose, and chose differently each time. Product decision: ASK.
#
# The searching branch also opened with "here are a few of our POPULAR models".
# We hold no sales figures, no ratings, no popularity data of any kind — the word
# appears nowhere in the prompt, the catalogue service, the tool schemas or the
# seed. It is invented, about the attribute a buyer weighs most heavily, and it
# is the ranking the paragraph above already forbids.
# --------------------------------------------------------------------------- #
def test_a_bare_booking_intent_asks_rather_than_searching():
    assert "A BOOKING INTENT ON ITS OWN IS NOT A REQUEST FOR CARS." in _P
    assert "have a model in mind" in _P
    # It must route the answer, not just refuse to search.
    assert "get_vehicle_details" in _P and "search_vehicles" in _P


def test_popularity_claims_are_forbidden():
    assert "AND NEVER CALL THEM POPULAR." in _P
    for claim in ("best-selling", "most-loved", "customer favourite"):
        assert claim in _P, f"{claim!r} must be named — the model reaches for synonyms"
    # And it must say what to use INSTEAD, or the rule just removes vocabulary.
    assert "roomiest" in _P and "most efficient" in _P


def test_the_spread_bases_carry_no_popularity():
    """The prompt rule is only honest if the tool genuinely cannot answer it."""
    from app.services.catalogue import SPREAD_BASES

    assert SPREAD_BASES == ("most_seats", "efficiency", "lowest_price", "flagship")
    joined = " ".join(SPREAD_BASES)
    for word in ("popular", "selling", "rating", "favourite"):
        assert word not in joined
