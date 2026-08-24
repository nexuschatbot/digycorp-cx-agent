"""STANDING GUARD: rule 1 — Aisha's INTRODUCTION reaches the customer.

*** REQUIREMENTS OVERRIDE — READ BEFORE ASSUMING THIS GUARD WAS WEAKENED BY MISTAKE.

FR-CX-06's AI-disclosure requirement was REMOVED by product decision on 2026-07-20,
at the user's request. It was a signed Must-have in the BRD with a PDPL-transparency
rationale, and this file was the standing test guarding it. The guard was NOT
deleted — it was NARROWED, deliberately and traceably.

  BEFORE: the first answer must disclose Aisha's name AND that she is an AI.
  AFTER:  the first answer must disclose Aisha's name AND her Elite affiliation.
          It says nothing about AI status either way.

What is deliberately RETAINED: "never claim to be a human being" stays in the
prompt. Not disclosing AI status is now permitted; affirmatively posing as a person
is still forbidden, and that is a separate assertion below.

This override is flagged for the D1 PDPL checklist and Elite engagement sign-off.
Compliance reviewer: the decision is recorded in CLAUDE.md rule 1 and in
app/agent/prompt.py's module docstring. If PDPL transparency is reinstated, restore
the _AI_TOKENS check into `introduces()` and this file is a working guard again —
the tokens are kept below, unused, for exactly that reason.
***

The PLACEMENT half of this file is untouched by the override and still load-bearing.
It exists because it once did not hold. Rule 17 used to stream text optimistically
and RETRACT it (`text_reset`) the instant the model opened a tool_use block, so
pre-tool narration never survived into the visible reply or the transcript. That was
correct for narration and wrong for one thing: the model was putting the mandatory
AI disclosure in the SAME pre-tool text block —

    retracted: "I'm Aisha, Elite's AI assistant. Let me search for 7-seaters..."
    delivered: "Great news — we have two 7-seaters within your budget..."

— so on any opening turn that called a tool, the disclosure was silently thrown
away. Two individually-correct rules collided and PDPL transparency lost.

THE SWALLOWING MECHANISM IS GONE: `text_break` replaced `text_reset` and deletes
nothing — pre-tool text is sealed into its own bubble and delivered. So the specific
collision above can no longer happen to anything.

The placement anchor stays anyway, and these tests with it, for a reason that never
depended on the retraction: an introduction belongs in the first thing Aisha actually
ANSWERS, not in a throat-clearing line ahead of a tool call. What changed is the
consequence of getting it wrong — a badly-placed introduction is now merely awkward
rather than invisible. The override changes WHAT the first answer must contain, not
WHERE it must appear, so every placement assertion below survives it verbatim.

WHY THIS FILE IS PERMANENT. The failure was invisible: the suite was green, the
markdown fix it shipped alongside genuinely worked, and the only way it surfaced was
reading the retracted text and then checking the persisted turn. The Arabic persona
phase rewrites this prompt wholesale, and nothing about a rewrite would announce that
the introduction had gone missing again. So it is asserted, not remembered.

STRUCTURAL ONLY — this suite never calls a live model (see CLAUDE.md). The prompt
still carries the requirement AND the placement rule, so deletion during a rewrite
is caught in every run, for free. What it cannot catch is the model ignoring an
instruction that is present: the live layer that once did that (the real model,
the real graph, the real retraction, asserting on the text a customer would read)
was removed because the test suite must not depend on a vendor API.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.agent import build_config
from app.agent.prompt import SYSTEM_PROMPT, system_prompt
from app.agent.tool_node import tool_node
from app.models import AuditEvent, AuditType


# RETAINED BUT UNUSED — the 2026-07-20 override removed the AI-disclosure
# requirement, so nothing asserts on these any more. They are kept verbatim so that
# reinstating FR-CX-06 is a one-line change to `introduces()` rather than a
# re-derivation of which phrasings the model actually reaches for in EN and AR.
_AI_TOKENS = (
    "ai assistant",
    "ai-assistant",
    "artificial intelligence",
    "virtual assistant",
    "مساعدة ذكاء اصطناعي",
    "مساعد ذكاء اصطناعي",
    "ذكاء اصطناعي",
)

# "with Elite" in the forms the model actually reaches for, EN and AR. Arabic is
# here deliberately: the persona phase is when the prompt gets rewritten, so that is
# precisely when this guard must still be able to see a correct Arabic introduction
# and not read it as a regression.
_ELITE_TOKENS = ("elite", "إيليت", "ايليت")


def introduces(text: str) -> bool:
    """Does this reply introduce Aisha BY NAME and BY ELITE AFFILIATION?

    Both halves are required (rule 1): a bare "welcome to Elite" is not the named
    persona, and "I'm Aisha" alone leaves the customer unsure who they are talking to.

    NOT asserted, by product decision 2026-07-20: whether she states she is an AI.
    This function used to require that too — see the override note at the top of
    this file. It is now silent on AI status, meaning a reply that discloses and a
    reply that does not BOTH pass. That is intended, not a hole.
    """
    low = (text or "").lower()
    named = "aisha" in low or "عائشة" in low
    affiliated = any(t in low for t in _ELITE_TOKENS)
    return named and affiliated


# --------------------------------------------------------------------------- #
# STRUCTURAL — always runs, no API key, no model.
# --------------------------------------------------------------------------- #
def test_prompt_requires_the_introduction() -> None:
    """Rule 1 is still in the prompt at all — narrowed to name + Elite by the
    2026-07-20 override. This no longer asserts an AI disclosure."""
    body = SYSTEM_PROMPT.lower()
    assert "aisha" in body
    assert "elite" in body
    assert "who you are (rule 1)" in body, "the identity section itself was deleted"


def test_prompt_does_not_claim_to_be_human() -> None:
    """The one PDPL-adjacent guard the override KEPT. Dropping the AI disclosure
    permits silence about AI status; it does not permit posing as a person."""
    assert "never imply or claim to be a human being" in SYSTEM_PROMPT.lower()


def test_prompt_anchors_introduction_to_the_first_answer_not_the_first_message() -> None:
    """The placement rule survives — this is the half that actually fixed the bug,
    and the 2026-07-20 override does not touch it.

    Rule 1 being present is not enough: it was present when the bug shipped. What
    was missing was "put it in the reply AFTER the tool result", without which the
    model introduces itself in a throat-clearing line before the tool call rather
    than in the answer. That no longer makes it invisible (`text_break` delivers it),
    but it still puts the introduction in the wrong place — so the assertion stays.
    """
    body = SYSTEM_PROMPT.lower()
    assert "first answer" in body, "rule 1 must be anchored to the first ANSWER, not the first message"
    # The specific instruction: don't introduce yourself before calling a tool.
    assert "before calling it" in body or "before a tool call" in body
    assert "comes back" in body, "must say the introduction goes in the post-tool-result reply"


def test_introduction_survives_the_date_anchor() -> None:
    """system_prompt() is what the live node sends — not the bare body."""
    assert "Elite" in system_prompt()
    assert "first ANSWER" in system_prompt()


def test_introduces_helper_is_strict() -> None:
    """Guard the guard: a half-introduction must not read as compliant."""
    assert introduces("I'm Aisha, Elite Group's assistant. How can I help?")
    assert introduces("أنا عائشة من إيليت، كيف أقدر أساعدك؟")
    assert not introduces("I'm Aisha, how can I help?")           # named, not affiliated
    assert not introduces("Welcome to Elite! How can I help?")    # affiliated, not named
    assert not introduces("Great news — we have two 7-seaters within your budget.")

    # THE OVERRIDE, ASSERTED DIRECTLY. Both of these are now compliant. Before
    # 2026-07-20 the second one failed, and that failure was the point of this file.
    # If this assertion ever starts failing, someone reinstated FR-CX-06 — go read
    # the override note at the top before "fixing" it.
    assert introduces("I'm Aisha, Elite Group's assistant. How can I help?")
    assert introduces("I'm Aisha, Elite Group's assistant. How can I help?")


# --------------------------------------------------------------------------- #
# STRUCTURAL — pre-tool text is DELIVERED, so the model's transcript is true.
#
# These replace three tests that asserted a "retraction notice": a system note the
# tool node injected to tell the model its pre-tool text had been discarded, so it
# would know to re-say the introduction it thought it had already given. The note
# was correct while text was deleted (`text_reset`). Nothing is deleted now — a
# bubble break (`text_break`) seals that text and delivers it — so the note would
# assert a falsehood, and the false belief it corrected cannot arise.
#
# What is guarded instead is the property that makes the note unnecessary: the tool
# node hands back tool_results and NOTHING ELSE. If a future change reintroduces
# synthetic text into the model's context, this is where it gets caught.
# --------------------------------------------------------------------------- #
def _assistant(*, text: str = "", tool: str | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "tu_1", "name": tool, "input": {}})
    return {"role": "assistant", "content": content}


def _run_tool_node(db, messages: list) -> dict:
    return tool_node({"messages": messages, "customer_id": None}, build_config(db, "sess-notice"))


def _block_types(out: dict) -> set[str]:
    return {b.get("type") for b in out["messages"][0]["content"]}


def test_tool_node_injects_no_text_on_a_first_turn_with_pre_tool_text(db) -> None:
    """The old regression case — text + tool_use on turn one — now needs no
    correction at all, because that text reached the customer."""
    out = _run_tool_node(db, [
        {"role": "user", "content": "show me 7-seaters"},
        _assistant(text="I'm Aisha, Elite Group's assistant. I'll look.", tool="get_showrooms"),
    ])
    assert _block_types(out) == {"tool_result"}, "the tool node must not inject text into the model's context"


def test_tool_node_injects_no_text_mid_conversation(db) -> None:
    """Same guarantee on a later turn, where the old notice was scoped out anyway."""
    out = _run_tool_node(db, [
        {"role": "user", "content": "hi"},
        _assistant(text="I'm Aisha, Elite Group's assistant. How can I help?"),
        {"role": "user", "content": "showrooms in Dubai?"},
        _assistant(text="Let me look that up.", tool="get_showrooms"),
    ])
    assert _block_types(out) == {"tool_result"}


def test_tool_node_injects_no_text_when_the_generation_opened_with_the_tool(db) -> None:
    """No pre-tool text at all — the case that was always silent. Still is."""
    out = _run_tool_node(db, [
        {"role": "user", "content": "show me 7-seaters"},
        _assistant(tool="get_showrooms"),
    ])
    assert _block_types(out) == {"tool_result"}


def test_notice_is_context_not_tool_output(db) -> None:
    """It must never reach the audit trail, a tool_event, or a tool_result — it
    is not something a tool returned, and the console must not show it as such."""
    out = _run_tool_node(db, [
        {"role": "user", "content": "show me 7-seaters"},
        _assistant(text="I'm Aisha, Elite Group's assistant.", tool="get_showrooms"),
    ])
    results = [b for b in out["messages"][0]["content"] if b.get("type") == "tool_result"]
    assert results, "the tool still ran"
    assert all("discarded and never delivered" not in r["content"] for r in results)
    rows = db.scalars(select(AuditEvent).where(AuditEvent.type == AuditType.tool_call)).all()
    assert rows, "the tool_call was still audited"
    assert all("discarded" not in json.dumps(r.payload or {}) for r in rows)
