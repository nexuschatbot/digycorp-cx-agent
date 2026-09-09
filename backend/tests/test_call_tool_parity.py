"""The voice call can answer what the chat cascade can answer.

WHY THIS FILE EXISTS. Chat's specialists reach eight tools that live in
`SUBAGENT_ONLY` rather than `REGISTRY`, and the realtime call built its vendor
function list from `TOOL_SCHEMAS` — the single-agent registry. So finance,
stock, colours, accessories, trims and the brochures were answerable by typing
and unanswerable by phone, and nothing failed: the model simply had no tool, and
the personas sent every finance question to a human callback.

That class of defect is the one this repo keeps rediscovering — a card naming a
tool nothing provides (`tests/test_subagent_tools.py`), a component built and
reachable by nothing (architecture doc §6). It is invisible to a test that
exercises either channel alone, because each is internally consistent. So the
assertions here are all COMPARISONS BETWEEN THE TWO CHANNELS.
"""

from __future__ import annotations

import json

import pytest

from app.agents import loader
from app.services.realtime_agent import _brief, _openai_tools
from app.tools import REGISTRY, SUBAGENT_ONLY, WRITE_TOOLS
from app.tools.call_schemas import (
    CALL_ESSENTIALS,
    CALL_EXTRA_TOOLS,
    call_tool_fns,
    call_tool_schemas,
    essentials_for,
)

# The specialists whose tools the call must be able to reach. Read from the
# cards rather than listed here, so adding a tool to a card is enough to make
# this file demand it on the call too.
SPECIALISTS = ("sales", "testdrive", "finance")


def _chat_reachable() -> set[str]:
    """Every tool a chat turn can end up executing on the multi-agent path.

    Aisha's own card, plus each specialist's reads, plus the commercial writes
    they may PROPOSE — those are executed by orchestrator/write.py, so they are
    reachable in chat even though no sub-agent holds them.
    """
    names = set(loader.frontmatter("aisha").get("tools") or [])
    names.discard("emit_briefs")  # answered by the orchestrator, never dispatched
    for agent in SPECIALISTS:
        fm = loader.frontmatter(agent)
        names |= set(fm.get("tools") or [])
        # `hold_slot` is the card's name for what lock_slot performs.
        names |= {
            "lock_slot" if p == "hold_slot" else p
            for p in (fm.get("proposes") or [])
        }
    return names


def _call_offered() -> set[str]:
    return {schema["name"] for schema in call_tool_schemas()}


# --------------------------------------------------------------------------- #
# The parity itself
# --------------------------------------------------------------------------- #
def test_call_offers_everything_chat_can_reach():
    """THE POINT OF THE FILE. A question chat can answer, the call can answer."""
    missing = sorted(_chat_reachable() - _call_offered())
    assert not missing, (
        f"the call cannot reach {missing}, which the chat cascade can — a "
        "customer asking this by phone gets 'we don't hold that' and by typing "
        "gets an answer"
    )


def test_the_eight_specialist_reads_are_the_additions():
    """Named explicitly, so removing one is a deliberate act with a diff."""
    assert set(CALL_EXTRA_TOOLS) == set(SUBAGENT_ONLY)
    assert _call_offered() == set(REGISTRY) | set(SUBAGENT_ONLY)


def test_the_call_gains_no_write():
    """Widening what the call can LOOK UP must never widen what it can DO."""
    assert not (set(CALL_EXTRA_TOOLS) & WRITE_TOOLS)
    # And the write surface is exactly what it was before this change.
    assert _call_offered() & WRITE_TOOLS == set(REGISTRY) & WRITE_TOOLS


def test_offered_and_dispatchable_are_the_same_set():
    """A tool offered but not dispatchable returns `unknown_tool` AFTER the
    model has already told the customer it was checking — the failure mode that
    made `fns=call_tool_fns()` necessary at the dispatch site."""
    assert set(call_tool_fns()) == _call_offered()
    assert all(callable(fn) for fn in call_tool_fns().values())


def test_dispatch_table_holds_the_registry_implementation():
    """No shadowing: the call runs the SAME function chat runs."""
    fns = call_tool_fns()
    for name, entry in REGISTRY.items():
        assert fns[name] is entry["fn"]
    for name in CALL_EXTRA_TOOLS:
        assert fns[name] is SUBAGENT_ONLY[name]["fn"]


# --------------------------------------------------------------------------- #
# The restored lines — selection, never authorship
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(CALL_ESSENTIALS))
def test_restored_lines_are_verbatim_from_the_chat_description(name):
    """THE DRIFT GUARD, and the reason this may be done at all.

    `app/tools/call_schemas.py` restores grounding rules that `_brief`'s
    240-character cut would drop. It is allowed to do that ONLY because it
    copies nothing: every line is a substring of the tool's real description, so
    there is still one source of truth. Editing that description in chat fails
    here rather than silently orphaning a rule the call depends on.
    """
    source = SUBAGENT_ONLY[name]["schema"]["description"]
    for line in CALL_ESSENTIALS[name]:
        assert line in source, (
            f"{name}: this line is no longer in the chat description, so the "
            f"call is now carrying prose nothing else says — {line[:80]!r}"
        )


@pytest.mark.parametrize("name", sorted(CALL_ESSENTIALS))
def test_restored_lines_would_actually_have_been_lost(name):
    """A line the trim already keeps must not be restored twice — that is
    duplicated prose in a per-response budget, and it means the entry is stale."""
    kept = _brief(SUBAGENT_ONLY[name]["schema"]["description"])
    for line in CALL_ESSENTIALS[name]:
        assert line not in kept, (
            f"{name}: _brief already keeps this line, so restoring it repeats "
            f"it to the model — {line[:80]!r}"
        )


def test_every_added_tool_reaches_the_vendor_with_its_essentials():
    """End to end through the real builder, which is what the vendor is sent."""
    by_name = {t["name"]: t for t in _openai_tools()}
    for name in CALL_EXTRA_TOOLS:
        assert name in by_name, f"{name} never reached the vendor tool list"
        for line in CALL_ESSENTIALS.get(name, ()):
            assert line in by_name[name]["description"]


def test_the_original_twenty_two_are_untouched():
    """The live call path keeps its exact prose: this change adds, never edits."""
    by_name = {t["name"]: t for t in _openai_tools()}
    for name, entry in REGISTRY.items():
        assert by_name[name]["description"] == _brief(
            entry["schema"].get("description", "")
        ), f"{name}: an existing call tool's description changed"
        assert essentials_for(name) == ""


def test_the_added_schemas_keep_their_parameters_verbatim():
    """Names, types, enums and required lists BIND the model — only prose is
    trimmed, so a parameter can never drift between the two channels."""
    by_name = {t["name"]: t for t in _openai_tools()}
    for name in CALL_EXTRA_TOOLS:
        source = SUBAGENT_ONLY[name]["schema"].get("input_schema", {})
        sent = by_name[name]["parameters"]
        assert sorted(sent.get("properties", {})) == sorted(
            source.get("properties", {})
        )
        assert sent.get("required", []) == source.get("required", [])


# --------------------------------------------------------------------------- #
# The budget this trimmer exists to protect
# --------------------------------------------------------------------------- #
def test_the_vendor_tool_payload_stays_within_budget():
    """Realtime re-counts this on every response — the 40k-TPM incident of
    2026-07-28 is why `_brief` exists at all.

    THE NUMBER IS A TRIPWIRE, NOT A TUNED LIMIT, and it is set close on purpose.
    Measured here: 22,817 chars (~5.7k tokens) for 30 tools, up from 15,553
    (~3.9k) for 22 — the eight specialist reads plus the grounding lines the
    240-character cut would have dropped. A ceiling with 40% headroom would
    never fire, which is the same as not having one; ~14% means the next
    addition has to be a decision somebody makes rather than one that happens.
    Raise it deliberately, the way the persona ceilings in
    test_call_realtime_mode.py are raised, with the reason written down.
    """
    chars = len(json.dumps(_openai_tools()))
    assert chars < 26_000, (
        f"the call's tool payload is {chars:,} chars (~{chars // 4:,} tokens) "
        "and is sent on every response — trim a description, drop a tool, or "
        "raise this deliberately and say why"
    )


def test_personas_no_longer_send_finance_to_a_human():
    """The call could not answer a finance question, so both personas told her
    to offer a callback. It can now, and a persona that still escalates would
    make the eight new tools unreachable in practice."""
    from app.agent.call_persona import natural_instructions
    from app.agent.prompt import realtime_instructions

    for persona in (realtime_instructions(), natural_instructions()):
        escalation = persona.split("PEOPLE.", 1)[1]
        assert "finance" not in escalation.split("Never invent policy")[0].lower()
        # And she is told these are hers to answer. The brief does NOT name all
        # eight tools — their own descriptions do that, and a per-response
        # budget is the wrong place to repeat them. What it must carry is the
        # policy change and the dependency chain that the schemas cannot
        # enforce: a price or stock question about one version needs a
        # variant_id, and get_variants is the only tool that produces one.
        assert "instalments" in persona
        assert "get_variants" in persona
        assert "variant_id" in persona
