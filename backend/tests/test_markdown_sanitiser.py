"""STANDING GUARD: the rule-17 sanitiser (FR-CX-01, LOC-08, LOC-01).

Rule 17 was prompt-only and prompt-only lost — measured leak rates of roughly 1 in
3 on the adversarial path, and the ordinary showrooms question leaking too, across
three rounds of prompt tuning that never landed it. app/agent/sanitise.py makes it
deterministic. These tests are what "deterministic" has to mean in practice.

THREE THINGS THEY PROVE:
  1. The markdown actually goes — including SPLIT ACROSS CHUNK BOUNDARIES, which
     is the case a naive per-chunk regex fails and the reason this is a state
     machine at all.
  2. Real text SURVIVES. A sanitiser that eats "AED 146,000 - 179,000" or mangles
     Arabic is worse than the leak it fixes: it corrupts every reply instead of
     embarrassing us on some of them. False positives are the bigger risk here.
  3. flush() happens. A tail left in the buffer would truncate the reply or leak
     into the next generation.

ARABIC IS NOT AN AFTERTHOUGHT. Step 3 rewrites the agent into Gulf Arabic on top
of this code, so RTL safety is required now, not later. The Arabic cases below use
real strings, put markers directly against RTL runs, and cover mixed-direction
lines (Latin model names and +971 numbers inside Arabic sentences).
"""

from __future__ import annotations

import pytest

from app.agent.sanitise import MAX_CARRY, MarkdownStripper, strip_markdown


def feed_in_chunks(text: str, size: int) -> str:
    """Stream `text` through the stripper in fixed-size chunks."""
    s = MarkdownStripper()
    out = [s.feed(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(s.flush())
    return "".join(out)


def feed_split_at(text: str, k: int) -> str:
    """Stream `text` as exactly two chunks, split at position k."""
    s = MarkdownStripper()
    return s.feed(text[:k]) + s.feed(text[k:]) + s.flush()


# --------------------------------------------------------------------------- #
# 1. The markdown goes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dirty,clean",
    [
        ("The **Jetour T2** is petrol.", "The Jetour T2 is petrol."),
        ("The __T2__ is petrol.", "The T2 is petrol."),
        ("That is *really* nice.", "That is really nice."),
        ("## Your booking\nis confirmed.", "Your booking\nis confirmed."),
        ("# Heading\ntext", "Heading\ntext"),
        ("- Jetour T2\n- Jetour X70", "Jetour T2\nJetour X70"),
        ("* Jetour T2\n* Jetour X70", "Jetour T2\nJetour X70"),
        ("• Jetour T2\n• Jetour X70", "Jetour T2\nJetour X70"),
        ("1. Al Quoz\n2. Deira", "Al Quoz\nDeira"),
        ("1) Al Quoz\n2) Deira", "Al Quoz\nDeira"),
        ("Ready?\n---\nNext", "Ready?\n\nNext"),
        ("  - indented bullet", "indented bullet"),
    ],
)
def test_markdown_is_removed(dirty: str, clean: str) -> None:
    assert strip_markdown(dirty) == clean


def test_the_real_observed_leak() -> None:
    """THE REGRESSION CASE, verbatim from the live run that failed the guard."""
    dirty = (
        "Both are in Dubai as you asked.\n\n"
        "1. **Jetour Al Quoz, Dubai** — Sunday, 26 July at 6 PM\n"
        "2. **Jetour Deira, Dubai** — Sunday, 26 July at 6 PM\n"
    )
    out = strip_markdown(dirty)
    assert "**" not in out
    assert "1." not in out and "2." not in out
    assert "Jetour Al Quoz, Dubai — Sunday, 26 July at 6 PM" in out
    assert "Jetour Deira, Dubai" in out


def test_the_second_observed_leak() -> None:
    """The bold-spec-sheet shape, from the stuck-conversation failure."""
    dirty = (
        "Great! Here's what we have:\n\n"
        "**Jetour T2 (petrol)** — AED 146,000 to 179,000.\n\n"
        "**Jetour T2 i-DM (hybrid)** — AED 144,000 to 159,000."
    )
    out = strip_markdown(dirty)
    assert "**" not in out
    assert "Jetour T2 (petrol) — AED 146,000 to 179,000." in out


# --------------------------------------------------------------------------- #
# 1b. SPLIT ACROSS CHUNK BOUNDARIES — the case a regex cannot handle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("k", range(0, 40))
def test_bold_split_at_every_position(k: int) -> None:
    """Two chunks, split at every index. `**` lands across the boundary at some
    of these, which is exactly how it arrives from a real token stream."""
    dirty = "The **Jetour T2** is a petrol SUV."
    assert feed_split_at(dirty, k) == "The Jetour T2 is a petrol SUV."


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13])
def test_everything_survives_any_chunk_size(size: int) -> None:
    """One character at a time is the worst case for a carry buffer."""
    dirty = (
        "Here:\n\n"
        "1. **Al Quoz** — AED 146,000 - 179,000\n"
        "2. __Deira__ — 15.6-inch screen\n"
        "---\n"
        "Which one?"
    )
    once = strip_markdown(dirty)
    assert feed_in_chunks(dirty, size) == once
    assert "**" not in once and "__" not in once


def test_flush_emits_a_held_tail() -> None:
    """A chunk ending mid-marker must not truncate the reply."""
    s = MarkdownStripper()
    assert s.feed("Bring your licence_") + s.flush() == "Bring your licence_"

    s2 = MarkdownStripper()
    assert s2.feed("Ends with a dash -") + s2.flush() == "Ends with a dash -"


def test_nothing_leaks_between_generations() -> None:
    """One stripper per generation: a fresh one starts at a line start with an
    empty buffer, so no tail can cross a generation boundary."""
    s = MarkdownStripper()
    s.feed("dangling *")
    fresh = MarkdownStripper()
    assert fresh.feed("- bullet") == "bullet"


def test_carry_stays_bounded() -> None:
    """The buffer must never grow into a line, let alone a paragraph — the first
    token has to reach the customer immediately (AD-A/AD-D)."""
    s = MarkdownStripper()
    for _ in range(200):
        s.feed("some ordinary prose without any markers ")
        assert len(s._pending) <= MAX_CARRY


# --------------------------------------------------------------------------- #
# 2. REAL TEXT SURVIVES — false positives are the bigger risk
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "It's priced between AED 146,000 - 179,000.",
        "AED 146,000 to 179,000, all in.",
        "It has a 15.6-inch touchscreen and a 540° parking camera.",
        "The T2 is a 2.0L turbo — 390 Nm — with a 7-speed DCT.",
        "Your reference is TD-2026-0142.",
        "Call us on +971 50 123 4567 or +971501234567.",
        "We're open 9 AM to 9 PM, noon to 9 PM on Sundays.",
        "Saturday, 25 July at 5 PM at Jetour Al Quoz.",
        "Khalas — your booking is confirmed!",
        "Ahlan wa sahlan, and welcome to Elite!",
        "Up to 140 km on electric alone, 800 km combined.",
        "That'll be 3 hours to charge fully.",
        "e-mail, co-operate, mid-size, plug-in hybrid, 7-seater",
    ],
)
def test_ordinary_prose_is_untouched(text: str) -> None:
    assert strip_markdown(text) == text


@pytest.mark.parametrize("size", [1, 2, 4, 7])
def test_ordinary_prose_untouched_when_streamed(size: int) -> None:
    """Chunking must not create false positives either."""
    text = "It's AED 146,000 - 179,000 for the 15.6-inch, 540° T2 — TD-2026-0142."
    assert feed_in_chunks(text, size) == text


# --------------------------------------------------------------------------- #
# 3. ARABIC / RTL (LOC-01) — required before step 3 builds on this
# --------------------------------------------------------------------------- #
AR_PLAIN = "أهلاً وسهلاً، أنا عائشة، مساعدة الذكاء الاصطناعي في إيليت."
AR_LICENCE = "لا تنسى إحضار رخصة القيادة معك يوم التجربة."
AR_MIXED_MODEL = "سيارة Jetour T2 متوفرة في معرض الاعتمادية بدبي."
AR_MIXED_PHONE = "رقمك هو +971501234567 وسنرسل لك تأكيداً على واتساب."
AR_MIXED_REF = "رقم حجزك هو TD-2026-0142، نراك يوم السبت."
AR_PRICE = "السعر بين 146,000 و 179,000 درهم."


@pytest.mark.parametrize(
    "text",
    [AR_PLAIN, AR_LICENCE, AR_MIXED_MODEL, AR_MIXED_PHONE, AR_MIXED_REF, AR_PRICE],
)
def test_arabic_is_untouched(text: str) -> None:
    """Byte-for-byte identical: the machine only removes ASCII markers, and it
    appends in input order, so logical order (and therefore bidi rendering) is
    unchanged."""
    assert strip_markdown(text) == text


@pytest.mark.parametrize("size", [1, 2, 3, 5])
def test_arabic_untouched_when_streamed(size: int) -> None:
    """Chunking can split a multi-byte character's neighbours; Python strings are
    code points, but the carry logic must still not disturb them."""
    for text in (AR_PLAIN, AR_MIXED_MODEL, AR_MIXED_PHONE):
        assert feed_in_chunks(text, size) == text


def test_markers_adjacent_to_arabic_are_stripped_cleanly() -> None:
    """A marker touching an RTL run — no space between marker and Arabic."""
    assert strip_markdown("**رخصة القيادة**") == "رخصة القيادة"
    assert strip_markdown("__إيليت__") == "إيليت"
    assert strip_markdown("- رخصة القيادة") == "رخصة القيادة"
    assert strip_markdown("1. معرض دبي\n2. معرض أبوظبي") == "معرض دبي\nمعرض أبوظبي"


def test_mixed_direction_line_with_markers() -> None:
    """The realistic step-3 failure: bold around a Latin model name inside an
    Arabic sentence. The Arabic must survive exactly; only the markers go."""
    dirty = "سيارة **Jetour T2** متوفرة في **معرض الاعتمادية** بدبي."
    assert strip_markdown(dirty) == "سيارة Jetour T2 متوفرة في معرض الاعتمادية بدبي."


def test_arabic_indic_digits_are_not_read_as_list_markers() -> None:
    """The numbered-list rule is ASCII-only on purpose: Arabic-Indic digits are
    prose. Treating "١." as a bullet would silently eat Arabic content."""
    text = "١. معرض دبي"
    assert strip_markdown(text) == text


def test_bidi_control_marks_are_preserved() -> None:
    """RLM/LRM are not markers. Stripping them would change how a mixed line
    renders — the exact corruption this test exists to rule out."""
    text = "‏سيارة Jetour T2‎ متوفرة."
    assert strip_markdown(text) == text
    assert "‏" in strip_markdown(text) and "‎" in strip_markdown(text)


def test_arabic_hyphens_and_numbers_survive() -> None:
    """Prices and references inside Arabic must not trip the dash rules."""
    text = "السعر 146,000 - 179,000 درهم، ورقم الحجز TD-2026-0142."
    assert strip_markdown(text) == text


# --------------------------------------------------------------------------- #
# 4. HTML TAGS — the observed "<br>" leak (rule 17)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dirty,clean",
    [
        # The exact live leak: a lone pre-tool "<br>" that reached the customer as
        # its own bubble. It must sanitise to nothing at all.
        ("<br>", ""),
        ("<br/>", ""),
        ("<br />", ""),
        ("<BR>", ""),
        ("Let me check.<br>Here are your slots.", "Let me check.Here are your slots."),
        ("One line.<br/>Another line.", "One line.Another line."),
        ("A <b>bold</b> claim.", "A bold claim."),
        ("<p>Your booking is confirmed.</p>", "Your booking is confirmed."),
        ("<ul><li>Al Quoz</li><li>Deira</li></ul>", "Al QuozDeira"),
        ('<span style="color:red">note</span>', "note"),
    ],
)
def test_html_tags_are_removed(dirty: str, clean: str) -> None:
    assert strip_markdown(dirty) == clean


@pytest.mark.parametrize("k", range(0, 6))
def test_br_split_at_every_position(k: int) -> None:
    """"<br>" landing across a chunk boundary ("<b" + "r>") is exactly how a real
    token stream delivers it — held in the carry, then dropped when '>' arrives."""
    assert feed_split_at("A<br>B", k) == "AB"


@pytest.mark.parametrize("size", [1, 2, 3, 4])
def test_html_survives_any_chunk_size(size: int) -> None:
    dirty = "Ready?<br/>Here:<br>See you Saturday."
    once = strip_markdown(dirty)
    assert once == "Ready?Here:See you Saturday."
    assert feed_in_chunks(dirty, size) == once


def test_lone_br_streamed_one_char_at_a_time_yields_nothing() -> None:
    """The pre-tool case: a bubble that is ONLY "<br>". Streamed a character at a
    time it must still sanitise to empty — so no stray bubble is ever sealed."""
    assert feed_in_chunks("<br>", 1) == ""


# A '<' that is NOT a tag start must survive — the bigger risk is a false positive.
@pytest.mark.parametrize(
    "text",
    [
        "Seats < 5 won't fit the family.",
        "Anything < 200k is in budget.",
        "5 < 10 and 10 > 5.",
        "It's a <3 kind of car.",  # '<' before a digit, not a letter
        "Use a < b in your head.",
    ],
)
def test_bare_less_than_is_kept(text: str) -> None:
    assert strip_markdown(text) == text


@pytest.mark.parametrize("size", [1, 2, 4])
def test_bare_less_than_kept_when_streamed(size: int) -> None:
    text = "Anything < 200k is fine, and 5 < 10 too."
    assert feed_in_chunks(text, size) == text


def test_unclosed_tag_is_emitted_not_swallowed() -> None:
    """An implausibly long '<…' with no '>' is prose, not a tag: it must come out,
    never vanish (flush's contract), and never hold the buffer without bound."""
    text = "a < b and then a long tail with no closing bracket ever appears here ok"
    assert strip_markdown(text) == text


def test_html_tag_adjacent_to_arabic_is_stripped_cleanly() -> None:
    """A tag touching an RTL run — the Arabic must survive byte-for-byte."""
    assert strip_markdown("رخصة القيادة<br>نراك السبت") == "رخصة القيادةنراك السبت"
    assert strip_markdown("سيارة <b>Jetour T2</b> متوفرة") == "سيارة Jetour T2 متوفرة"


# --------------------------------------------------------------------------- #
# The fallback bypasses the stripper, so assert it is clean rather than route it
# --------------------------------------------------------------------------- #
def test_fallback_text_carries_no_markdown() -> None:
    from app.agent.graph import FALLBACK_TEXT

    assert strip_markdown(FALLBACK_TEXT) == FALLBACK_TEXT
