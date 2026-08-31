"""STANDING GUARD: rule 17 / FR-CX-01, LOC-08 — no markdown reaches the customer.

This chat widget and the WhatsApp simulator render NO markdown. A `**` or a `1.`
that the model types is not formatting, it is literal punctuation the customer
reads. Rule 17 says so; nothing asserted it until now.

WHY THIS FILE EXISTS. Markdown has regressed TWICE in this build. The second time
was caught by eye during persona step 1, in a reply that had reached a customer-
visible slot offer:

    1. **Jetour Al Quoz, Dubai** — Sunday, 26 July at 6 PM
    2. **Jetour Deira, Dubai** — Sunday, 26 July at 6 PM

That leak did not reproduce under 14 controlled A/B turns (with and without the
tone layer). It is a LOW-RATE behaviour, which is precisely why eyeballing a demo
script cannot guard it and an assertion must. Same lesson as the AI disclosure:
every non-negotiable gets a test, not a hope.

STRUCTURAL ONLY — this suite never calls a live model (see CLAUDE.md): rule 17's
instruction is still IN the prompt, and the detector below is itself correct.
Catches deletion during the step-2 and step-3 (Arabic) prompt rewrites, in every
run. The live layer that once exercised the real model on the paths that actually
leak — spec-heavy catalogue answers, the slot offer, and a STUCK conversation
(S-20) — was removed: the test suite must not depend on a vendor API. The residual
gap (a low-rate leak only the real model reproduces) is caught in production by the
deterministic sanitiser, app/agent/sanitise.py, not by a live test.
"""

from __future__ import annotations

import re


from app.agent.prompt import SYSTEM_PROMPT, system_prompt


# What rule 17 forbids, as the model actually emits it. Anchored to line starts
# for the block constructs so ordinary prose punctuation is not a false positive:
# an em-dash mid-sentence is fine, a price range "146,000 - 179,000" is fine, a
# line BEGINNING "- " is a bullet.
_MARKDOWN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("heading", r"(?m)^\s{0,3}#{1,6}\s"),
    ("bullet", r"(?m)^\s{0,3}[-*•]\s"),
    ("numbered list", r"(?m)^\s{0,3}\d{1,2}[.)]\s"),
    ("horizontal rule", r"(?m)^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$"),
    ("bold", r"\*\*"),
    ("underscore emphasis", r"__"),
    ("italic asterisk", r"(?<!\*)\*(?!\*)\S[^\n*]*\S\*(?!\*)"),
)


def markdown_in(text: str) -> str | None:
    """The NAME of the first markdown construct found, or None if clean.

    Returns the name rather than a bool so a failure message says WHAT leaked.
    """
    for name, pattern in _MARKDOWN_PATTERNS:
        m = re.search(pattern, text or "")
        if m:
            return f"{name}: {m.group(0)!r}"
    return None


# --------------------------------------------------------------------------- #
# STRUCTURAL — always runs, no API key, no model.
# --------------------------------------------------------------------------- #
def test_prompt_still_forbids_markdown() -> None:
    """Rule 17 is still in the prompt at all — catches deletion in a rewrite."""
    body = SYSTEM_PROMPT.lower()
    assert "output format" in body, "the rule 17 section is gone"
    assert "markdown" in body
    for token in ("headings", "bold", "bullet"):
        assert token in body, f"rule 17 no longer names {token!r} as forbidden"


def test_prompt_explains_why_markdown_is_forbidden() -> None:
    """The REASON is load-bearing, not decoration.

    "No markdown" alone reads as a style preference a later rewrite may trade
    away for warmth. "This channel renders none of it, so it reaches the customer
    as literal characters" is a fact about the product that no persona layer has
    grounds to overrule. Keep the reason in the prompt.
    """
    body = SYSTEM_PROMPT.lower()
    assert "literal" in body, "rule 17 must say markup arrives as LITERAL characters"


def test_prompt_forbids_tool_narration() -> None:
    """The other half of rule 17 — no "let me check" before a tool result."""
    body = SYSTEM_PROMPT.lower()
    assert "narrate" in body
    assert "let me check" in body


def test_rule_17_survives_the_date_anchor() -> None:
    """system_prompt() is what the live node sends — not the bare body."""
    live = system_prompt().lower()
    assert "markdown" in live and "narrate" in live


def test_detector_catches_what_actually_leaked() -> None:
    """Guard the guard, part 1: the REAL observed leak must be detected."""
    observed = (
        "Both are in Dubai as you asked.\n\n"
        "1. **Jetour Al Quoz, Dubai** — Sunday, 26 July at 6 PM\n"
        "2. **Jetour Deira, Dubai** — Sunday, 26 July at 6 PM\n"
    )
    assert markdown_in(observed), "the detector misses the leak it was written for"

    assert markdown_in("## Your booking")
    assert markdown_in("- Jetour T2\n- Jetour X70")
    assert markdown_in("• Jetour T2")
    assert markdown_in("Here it is:\n\n1. Al Quoz\n2. Deira")
    assert markdown_in("The **Jetour T2** is a petrol SUV.")
    assert markdown_in("Ready?\n\n---\n\nNext steps")
    assert markdown_in("The __T2__ is a petrol SUV.")


def test_detector_does_not_fire_on_ordinary_prose() -> None:
    """Guard the guard, part 2: false positives would be worse than no test —
    a flaky guard on a Must-have gets muted, and then it guards nothing.

    Every string here is a real reply shape from the live runs.
    """
    clean = (
        "Ahlan wa sahlan, and welcome to Elite! I'm Aisha, Elite's AI assistant.",
        "It's priced between 146,000 and 179,000 AED.",
        "The T2 is a 2.0L turbo — 390 Nm — paired with a 7-speed DCT.",
        "Saturday, 25 July at 5 PM at Jetour Al Quoz. Does that work for you?",
        "You can choose between Jetour Al Quoz or Jetour Deira. Which would you prefer?",
        "Khalas — your booking is confirmed! Your reference is TD-2026-0001.",
        "We're open 9 AM to 9 PM daily, noon to 9 PM on Sundays.",
        "It has a 540-degree parking camera and a 15.6-inch touchscreen.",
        "أهلاً وسهلاً، أنا عائشة، مساعدة الذكاء الاصطناعي في إيليت.",
        "That'll be 3 hours to charge fully, with 800 km of total range.",
    )
    for text in clean:
        assert markdown_in(text) is None, f"false positive on ordinary prose: {text!r}"
