"""STANDING GUARD: rule 5 (discovery caps) and rule 8 (personalisation).

Both are SOFT behavioural rules — they live in the prompt, because "ask at most
two questions" is judgement, not an invariant a service layer can enforce. That
makes them exactly the kind of rule that decays silently across prompt rewrites,
which is why they are asserted rather than trusted. Step 3 (Arabic) rewrites this
prompt wholesale; nothing about that rewrite would announce that the caps had
quietly become suggestions.

STRUCTURAL ONLY — this suite never calls a live model (see CLAUDE.md): the caps
and the grounding restriction are still IN the prompt, and the prompt still obeys
rule 17 in its own formatting. The live layer that once checked the model actually
honours them was removed — the test suite must not depend on a vendor API.

WHY THE PROMPT-SHAPE TEST IS HERE. The prompt teaches output shape by example:
while rule 9's preconditions were an indented numbered list, the agent leaked
numbered lists and bold at a measured ~43% on the adversarial path. The prompt is
now prose throughout and must stay that way. Rule 17's own parenthesised tokens
(#, **, __) are the ONE exception and are deliberately exempt — citing a
forbidden token is instruction, using it is example. See the comment at the top of
app/agent/prompt.py before "cleaning" them.
"""

from __future__ import annotations

import re


from app.agent.prompt import SYSTEM_PROMPT, system_prompt


# Cars that exist in the world but NOT in the Elite catalogue. A "helpful" model
# reaching for one of these is the single most likely grounding failure in
# discovery (rule 2 / FR-CX-07 applied to rule 5).
_NOT_OURS = (
    "toyota", "nissan", "honda", "hyundai", "kia", "mitsubishi", "ford",
    "chevrolet", "mazda", "volkswagen", "bmw", "mercedes", "audi", "lexus",
    "land cruiser", "prado", "patrol", "pajero", "fortuner", "highlander",
)

_FEMALE_CONSULTANT = re.compile(r"female|lady|ladies|woman|women", re.I)
_CHILD_SEAT = re.compile(r"child seat|baby seat|car seat|child'?s seat", re.I)


def count_questions(text: str) -> int:
    """Question marks, Latin and Arabic, as a proxy for questions asked."""
    return text.count("?") + text.count("؟")


def catalogue_names(db) -> list[str]:
    from app.models import VehicleModel
    from sqlalchemy import select

    return [n.lower() for n in db.scalars(select(VehicleModel.name)).all()]


def foreign_makes_in(text: str) -> list[str]:
    """Off-catalogue makes actually NAMED, by whole word.

    Substring matching was the first version and it was wrong in the way this
    codebase keeps getting wrong: "audi" sits inside "audio" (the T2's Sony audio
    system), "ford" sits inside "affordable" (which a car agent says constantly),
    and "patrol" inside "patrolling". Every reply describing the T2 was reported
    as recommending an Audi it never mentioned. A test that lies about grounding
    is worse than no test — it would have had us "fix" a correct agent.
    """
    low = (text or "").lower()
    return [m for m in _NOT_OURS if re.search(rf"\b{re.escape(m)}\b", low)]


def models_recommended(text: str, names: list[str]) -> list[str]:
    """Which catalogue models this text names — each counted ONCE.

    TWO bugs fixed here, both the same trap: matching prose more strictly than
    people speak it.

    NESTING — "Jetour T2 i-DM" contains "Jetour T2", and "Jetour T1 i-DM"
    contains "Jetour T1". Naive substring counting turned two recommendations
    into three, firing the cap assertion on an agent that was capping correctly.
    Longest-first with the matched span consumed fixes it.

    NATURAL SPEECH — the catalogue stores "Jetour X70 Plus", but a warm agent
    that has already said "Jetour" calls it "the X70 Plus" from then on. Matching
    only full names reported "no catalogue car was recommended" for a reply
    recommending two of them. So the brand prefix is optional: each model is
    matched by its full name OR its model part.
    """
    low = (text or "").lower()
    # (full name, what to search for) — longest form first so nested names and
    # the fuller "Jetour X70 Plus" are consumed before bare "X70 Plus".
    variants: list[tuple[str, str]] = []
    for name in names:
        variants.append((name, name))
        without_brand = name.split(" ", 1)[1] if " " in name else name
        if without_brand != name:
            variants.append((name, without_brand))
    variants.sort(key=lambda v: len(v[1]), reverse=True)

    found: list[str] = []
    for full, needle in variants:
        if full in found:
            continue
        idx = low.find(needle)
        if idx != -1:
            found.append(full)
            # Blank the span so a shorter nested name cannot re-match it.
            low = low[:idx] + " " * len(needle) + low[idx + len(needle) :]
    return found


# --------------------------------------------------------------------------- #
# STRUCTURAL — always runs.
# --------------------------------------------------------------------------- #
def test_prompt_carries_the_discovery_caps() -> None:
    body = SYSTEM_PROMPT.lower()
    assert "at most two clarifying questions" in body, "rule 5's question cap is gone"
    assert "at most three cars" in body, "rule 5's recommendation cap is gone"


def test_prompt_restricts_recommendations_to_the_catalogue() -> None:
    """The grounding half of rule 5 — the bit most likely to slip in practice."""
    body = SYSTEM_PROMPT.lower()
    assert "search_vehicles" in body
    assert "do not know cars in general" in body
    assert "does not exist in this conversation" in body


def test_prompt_requires_honest_no_match_with_the_tradeoff() -> None:
    body = SYSTEM_PROMPT.lower()
    assert "nearest real thing" in body
    assert "trade-off" in body


def test_prompt_carries_infer_never_re_ask() -> None:
    body = SYSTEM_PROMPT.lower()
    assert "never ask something this conversation has already told you" in body
    assert "child seat" in body


def test_prompt_carries_offer_never_presume() -> None:
    """LOC-06 is one-directional: honour on request, never volunteer. Both halves
    must survive — a rewrite keeping only "arrange it warmly" would license the
    agent to start offering it."""
    body = SYSTEM_PROMPT.lower()
    assert "never raise a preference about who serves them" in body
    assert "honour it when asked, never volunteer it" in body


def test_personalisation_is_one_exchange_before_the_recap() -> None:
    body = SYSTEM_PROMPT.lower()
    assert "before you recap and book" in body
    assert "it happens once" in body


def test_prompt_obeys_rule_17_in_its_own_formatting() -> None:
    """THE SHAPE GUARD. The prompt teaches output shape by example: an indented
    numbered list in here produced numbered lists and bold out there at a measured
    ~43% on the adversarial path. Rule 17's own token citations are exempt —
    citing a forbidden token is instruction, using it is example.
    """
    violations: list[str] = []
    for i, line in enumerate(system_prompt().split("\n"), 1):
        if re.match(r"^\s{0,3}#{1,6}\s", line):
            violations.append(f"line {i}: heading — {line[:60]!r}")
        if re.match(r"^\s{0,3}[-•]\s", line):
            violations.append(f"line {i}: bullet — {line[:60]!r}")
        if re.match(r"^\s{0,3}\d{1,2}[.)]\s", line):
            violations.append(f"line {i}: numbered list — {line[:60]!r}")
        if line.startswith(" ") and line.strip():
            violations.append(f"line {i}: indented block — {line[:60]!r}")
        if ("**" in line or "__" in line) and "Do NOT use headings" not in line:
            violations.append(f"line {i}: emphasis markup — {line[:60]!r}")
    assert not violations, "the prompt must model the prose it demands:\n" + "\n".join(violations)


def test_no_sentence_demonstrates_an_over_cap(db) -> None:
    """The prompt teaches by example, so it must not SHOW more than it allows.

    Checking for the words "four cars" was the obvious version and it was wrong:
    the prompt says "being asked five questions in a row feels like an application
    form", which argues AGAINST five rather than demonstrating it. What actually
    matters is whether any single sentence enumerates more than three catalogue
    models — a demonstrated over-cap the model would copy.
    """
    names = catalogue_names(db)
    for sentence in re.split(r"(?<=[.!?])\s+", system_prompt()):
        listed = [n for n in names if n in sentence.lower()]
        assert len(listed) <= 3, f"a sentence names {len(listed)} models: {listed}\n{sentence!r}"
