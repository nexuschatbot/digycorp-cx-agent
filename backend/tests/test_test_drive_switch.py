"""TEST_DRIVE_ENABLED — the switch that stops us taking test drives (2026-09-09).

WHAT THE FLAG IS FOR. Test drive is not a module in this codebase, it is the
spine: nine of REGISTRY's twenty-two tools, twelve of Aisha's sixteen journey
sections, four tables, and most of this suite. Deleting it would be a rewrite
with no way back; a flag makes it one .env line and a restart, and keeps the
default state — which is what customers meet — under test exactly as it was.

THE FIRST THING ASSERTED HERE IS THAT ON MEANS UNCHANGED, the same contract
test_latency_dials.py opens with. Every gated surface has to be byte-identical
to what shipped when the flag is true, because the flag is a withdrawal and the
rollback target has to be the thing we already trust.

THE SURFACES, AND WHY EACH IS GATED SEPARATELY. There is no single choke point:

    aisha_schemas()        what the fan-out Aisha may call
    call_tool_schemas()    what the VOICE call may call — its own binding
    call_tool_fns()        what the voice call may DISPATCH, offered or not
    select.routable()      whether the TestDrive specialist takes briefs
    prompt bodies          both chat and BOTH voice personas
    /api/intent/*          the taps, which run with no model in the loop at all

A reviewer's instinct is that gating the first of those covers the rest. It does
not, and the tap routes are the sharpest case: they exist precisely so a slot
chip commits without waking a model, so unbinding every tool from every prompt
leaves a page loaded five minutes ago still able to move inventory over plain
HTTP.

OFFLINE THROUGHOUT. Nothing here builds a client or reaches the network.
"""

from __future__ import annotations

import pytest

from app import config
from app.agents import loader
from app.orchestrator import select as select_mod


@pytest.fixture()
def test_drives_on(monkeypatch):
    """The flag explicitly ON, caches dropped.

    FORCED RATHER THAN ASSUMED, because the suite can be run with the env var
    set — `TEST_DRIVE_ENABLED=false pytest` is how the withdrawal was sized
    before it shipped. A test that asserts "on means unchanged" has to SET on,
    or it is asserting whatever the shell happened to export.
    """
    monkeypatch.setattr(config, "TEST_DRIVE_ENABLED", True)
    select_mod.reset()
    loader.reset()
    yield
    select_mod.reset()
    loader.reset()


@pytest.fixture()
def test_drives_off(monkeypatch):
    """The flag off, with every per-process cache it feeds dropped.

    THE RESETS ARE THE FIXTURE. `select.cards()` memoises the roster's card text
    and `_vectors()` memoises its embeddings; the loader caches each composed
    body. None of them re-read a flag, because production never flips one inside
    a live process — so a test that only monkeypatched config would assert
    against whatever the previous test happened to warm.
    """
    monkeypatch.setattr(config, "TEST_DRIVE_ENABLED", False)
    select_mod.reset()
    loader.reset()
    yield
    select_mod.reset()
    loader.reset()


# --------------------------------------------------------------------------- #
# On means unchanged
# --------------------------------------------------------------------------- #
def test_the_default_is_on(monkeypatch):
    """A withdrawal ships ON. Anything else turns a merge into an outage.

    THE DECLARED DEFAULT, NOT THE AMBIENT VALUE. Reloading config with the
    variable unset is what actually asserts the literal in config.py; reading
    `config.TEST_DRIVE_ENABLED` would pass or fail on the shell's exports, which
    is the opposite of what this test is for.
    """
    import importlib

    monkeypatch.delenv("TEST_DRIVE_ENABLED", raising=False)
    fresh = importlib.reload(config)
    try:
        assert fresh.TEST_DRIVE_ENABLED is True
    finally:
        importlib.reload(config)


def test_with_the_flag_on_every_binding_is_what_it_was(test_drives_on):
    from app.tools import REGISTRY, aisha_schemas
    from app.tools.call_schemas import call_tool_fns, call_tool_schemas

    assert select_mod.routable() == select_mod.ROUTABLE
    assert {s["name"] for s in aisha_schemas()} == set(
        loader.frontmatter("aisha").get("tools") or []
    )
    # The voice binding is the whole single-agent registry plus its eight reads.
    assert set(REGISTRY) <= {s["name"] for s in call_tool_schemas()}
    assert set(call_tool_fns()) == {s["name"] for s in call_tool_schemas()}


def test_the_single_agent_body_is_untouched_in_either_state(test_drives_off):
    """The fan-out kill switch rolls back to the single-agent prompt, so no flag
    added afterwards may shorten it — that path is byte-frozen against
    tests/fixtures/prompt_body_original.txt and is the thing we fall back TO."""
    from app.agent import prompt

    assert prompt._PROMPT_BODY == loader.single_agent_body()
    assert loader.multi_agent_body(test_drive=True) == loader.single_agent_body()
    # The single agent still books, so it keeps the journey prose AND must never
    # see the section that withdraws it — the same two-way check
    # test_prompt_versions applies to DELEGATING.
    body = loader.single_agent_body()
    assert loader.sections("aisha")["TEST_DRIVE_OFF"] not in body
    assert loader.sections("aisha")["BOOKING_SEQUENCE"] in body


# --------------------------------------------------------------------------- #
# Off: the bindings
# --------------------------------------------------------------------------- #
def test_aisha_loses_every_test_drive_tool(test_drives_off):
    """Prose is not what stops a tool call — the binding is."""
    from app.tools import TEST_DRIVE_TOOLS, aisha_schemas

    bound = {s["name"] for s in aisha_schemas()}
    assert bound & TEST_DRIVE_TOOLS == set()
    # And she keeps everything that is not about a drive: identity, consent,
    # memory, the brochure, the briefs.
    for kept in ("find_or_create_customer", "record_consent", "send_brochure",
                 "request_human_callback", "emit_briefs"):
        assert kept in bound


def test_the_voice_call_loses_them_too(test_drives_off):
    """ITS OWN SURFACE. The call never touches aisha_schemas or the
    orchestrator — it binds REGISTRY whole, writes included, against its own
    persona. Gating only the chat leaves a phone line that still books."""
    from app.tools import TEST_DRIVE_TOOLS
    from app.tools.call_schemas import call_tool_schemas

    assert {s["name"] for s in call_tool_schemas()} & TEST_DRIVE_TOOLS == set()


def test_the_voice_call_cannot_dispatch_what_it_was_not_offered(test_drives_off):
    """A schema list constrains what is OFFERED, never what a model EMITS —
    the lesson tool_node._dispatch learned on 2026-08-29. The dispatch table
    has to shrink with the offer, not merely alongside it."""
    from app.tools import TEST_DRIVE_TOOLS
    from app.tools.call_schemas import call_tool_fns, call_tool_schemas

    assert set(call_tool_fns()) & TEST_DRIVE_TOOLS == set()
    assert set(call_tool_fns()) == {s["name"] for s in call_tool_schemas()}


def test_no_write_tool_survives_anywhere(test_drives_off):
    """The four commercial writes exist only to book a drive. If one is still
    reachable from any binding, something can still commit inventory."""
    from app.tools import COMMERCIAL_WRITE_TOOLS, aisha_schemas
    from app.tools.call_schemas import call_tool_fns, call_tool_schemas

    reachable = (
        {s["name"] for s in aisha_schemas()}
        | {s["name"] for s in call_tool_schemas()}
        | set(call_tool_fns())
    )
    assert reachable & COMMERCIAL_WRITE_TOOLS == set()


# --------------------------------------------------------------------------- #
# Off: the roster
# --------------------------------------------------------------------------- #
def test_the_testdrive_specialist_leaves_the_roster(test_drives_off):
    assert "testdrive" not in select_mod.routable()
    assert set(select_mod.routable()) == {"sales", "finance"}
    # The card stays on disk, versioned and gate-checked, so turning the flag
    # back on needs no prompt change.
    assert loader.frontmatter("testdrive").get("routable") is True


def test_the_judge_is_never_told_about_an_agent_it_cannot_answer(test_drives_off):
    """The mirror of the 2026-09-01 defect _route_tool records. There the schema
    was NARROWER than the prose and every finance brief died as "fallback"; here
    the prose would be WIDER than the schema, and a drive question would route
    confidently to whichever of the remaining two sounded closest."""
    from app.orchestrator import judge

    brief = judge._system(select_mod.routable())
    assert "testdrive" not in brief
    assert "Move my booking to Sunday" not in brief
    assert "sales" in brief and "finance" in brief
    # The few-shot comes from the same roster, so no utterance teaches a route
    # the enum will not accept.
    assert "-> testdrive" not in judge._examples()


def test_a_booking_brief_is_unroutable_rather_than_misrouted(test_drives_off):
    """WHAT THE CUSTOMER GETS INSTEAD. An unroutable brief is a real outcome the
    orchestrator already handles — it reaches Aisha's compose payload under
    `unavailable` with a reason, and she hands off. What must never happen is a
    booking request answered by Sales, which is what an ungated roster does."""
    from app.orchestrator import run

    plan, unroutable = run.plan_turn(
        [{"brief": "Book a test drive for the Jetour T2 on Saturday at Al Quoz"}],
        judge=None,
    )
    assert plan.steps == []
    assert [s.agent for s in unroutable] == [None]
    assert unroutable[0].status == "unroutable"


# --------------------------------------------------------------------------- #
# Off: the prompts
# --------------------------------------------------------------------------- #
def test_the_journey_prose_is_gone_from_the_chat_body(test_drives_off):
    """NOT MERELY OUTRANKED — LEFT OUT. This repo has twice recorded that prose
    contradicting a binding produces a model reading an instruction it cannot
    follow (SLOT_ALTERNATIVES before get_available_slots was hers; DELEGATING
    against the ported lock_slot rules). Appending an override and leaving
    twelve sections of booking sequence in place would be that defect again."""
    from app.agent import prompt

    body = prompt._resolved_body(multi_agent=True)
    sections = loader.sections("aisha")
    for name in loader.TEST_DRIVE_SECTIONS:
        assert sections[name] not in body, name
    # And the four car-choosing sections stay, because a customer can still be
    # helped to a shortlist with no drive at the end of it.
    for name in ("BROCHURE", "COMPARE", "MODEL_LOOKUP", "FINDING_THE_RIGHT_CAR"):
        assert sections[name] in body, name


def test_the_chat_body_says_what_to_do_instead(test_drives_off):
    from app.agent import prompt

    body = prompt._resolved_body(multi_agent=True)
    assert loader.sections("aisha")["TEST_DRIVE_OFF"] in body
    assert "test_drive_unavailable" in body
    assert "request_human_callback" in body


def test_both_voice_personas_are_gated(test_drives_off):
    """CALL_PERSONA picks between them at runtime, so gating one leaves the
    other live behind an unrelated env var."""
    from app.agent import prompt
    from app.agent.call_persona import natural_instructions

    for text in (natural_instructions(), prompt.realtime_instructions()):
        for tool in ("create_booking", "lock_slot", "get_available_slots",
                     "reschedule_booking", "cancel_booking", "get_booking"):
            assert tool not in text, tool
        assert "test_drive_unavailable" in text


# --------------------------------------------------------------------------- #
# Off: the paths with no model in them
# --------------------------------------------------------------------------- #
def test_the_slot_tap_is_refused(test_drives_off, db, open_slot):
    """THE GATE THAT ACTUALLY MATTERS HERE. This route exists so a slot chip
    commits without waking a model — so every prompt and binding change above
    is invisible to it, and a page loaded before the flag flipped would still
    move inventory over plain HTTP."""
    from fastapi import HTTPException

    from app.api import intents

    with pytest.raises(HTTPException) as excinfo:
        intents._tap(db, "sess-td-off", "lock_slot", {"slot_id": open_slot.id})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "test_drive_unavailable"


def test_the_licence_tap_is_refused(test_drives_off, db):
    """It reads like a consent record and is not: it records that someone was
    told to bring a licence TO A DRIVE, and there is no drive to bring it to."""
    from fastapi import HTTPException

    from app.api import intents

    with pytest.raises(HTTPException) as excinfo:
        intents._tap(db, "sess-td-off", "acknowledge_licence_reminder", {})
    assert excinfo.value.status_code == 409


def test_the_consent_tap_still_works(test_drives_off, db):
    """WhatsApp consent is not a booking act — it also gates the brochure — so
    it must survive the flag. A gate that took it would break a journey this
    change is supposed to leave alone."""
    from app.api import intents

    assert "record_consent" not in _test_drive_tools()
    # Reaches the real tool rather than the 409 branch.
    result = intents._tap(db, "sess-td-off", "record_consent", {"messaging": True})
    assert isinstance(result, dict)


def _test_drive_tools() -> frozenset[str]:
    from app.tools import TEST_DRIVE_TOOLS

    return TEST_DRIVE_TOOLS


# --------------------------------------------------------------------------- #
# Off: what operators and customers are told
# --------------------------------------------------------------------------- #
def test_the_escalation_reason_exists_on_both_sides():
    """The tool's enum and the service's closed set are two lists of the same
    thing; a reason in one and not the other is silently coerced to `other`,
    which is exactly the invisibility this reason was added to avoid."""
    from app.services.escalation import REASONS
    from app.tools.escalation_tools import REQUEST_HUMAN_CALLBACK_SCHEMA

    enum = REQUEST_HUMAN_CALLBACK_SCHEMA["input_schema"]["properties"]["reason"]["enum"]
    assert "test_drive_unavailable" in REASONS
    assert set(enum) == set(REASONS)


def test_the_console_registry_stops_claiming_a_live_booking_agent(test_drives_off):
    """Rule 16: nothing may be listed that is not actually running. The one live
    row is named for the errand it no longer runs."""
    from app.services import console

    rows = {r["agent"]: r["status"] for r in console.agent_registry()}
    assert rows["Test Drive Booking Agent"] == "retired"
    assert "live" in rows.values()


def test_the_client_config_route_tells_the_browser(test_drives_off):
    """The booking affordances that never reach Aisha — the starter card, the
    dialog's book button — are static markup, and would go on promising a drive
    the agent hands straight to a colleague."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/config").json() == {"test_drive_enabled": False}
