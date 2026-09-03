"""The three turn-latency dials of 2026-09-03, measured on the test VM's journal.

    AISHA_CATALOGUE_READS      she calls search_vehicles / get_vehicle_details /
                               compare_vehicles herself instead of briefing Sales
                               (70 of 97 cascades were a read with nothing to
                               propose: ~1.4s judge + ~2.6s Haiku for a ~ms query)
    SPECULATE_ON_MESSAGE       the specialist selection starts on the customer's
                               message, under Aisha's whole first generation
                               (the brief-level offer fired 9ms before the end)
    ANTHROPIC_KEEPALIVE_SECONDS idle keep-alive above the SDK's 5s, so a turn's
                               first call does not re-handshake TLS (~300ms)

Every dial ships OFF or at the SDK default, and the first thing asserted here is
that off means byte-identical: same binding, same prompt, same client.
Offline throughout — selections are stubbed, no client is ever built against
the network.
"""
from __future__ import annotations

from concurrent.futures import Future

import pytest

from app.orchestrator import run, speculate
from app.orchestrator import select as select_mod


@pytest.fixture(autouse=True)
def _clean_registry():
    speculate.reset()
    yield
    speculate.reset()


def _done(selection) -> Future:
    f: Future = Future()
    f.set_result(selection)
    return f


# --------------------------------------------------------------------------- #
# AISHA_CATALOGUE_READS — the binding
# --------------------------------------------------------------------------- #
def test_off_means_the_binding_is_what_the_card_declares(monkeypatch):
    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", False)
    from app.agents import loader
    from app.tools import aisha_schemas

    assert [s["name"] for s in aisha_schemas()] == list(loader.frontmatter("aisha")["tools"])


def test_on_adds_the_three_reads_just_ahead_of_emit_briefs(monkeypatch):
    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", True)
    from app.tools import AISHA_CATALOGUE_READ_TOOLS, aisha_schemas

    names = [s["name"] for s in aisha_schemas()]
    assert names[-1] == "emit_briefs", "emit_briefs stays last — it is a cache prefix"
    assert names[-4:-1] == list(AISHA_CATALOGUE_READ_TOOLS)
    assert len(names) == len(set(names)), "no tool is bound twice"


def test_on_still_hands_her_no_write_and_no_specialist_only_read(monkeypatch):
    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", True)
    from app.tools import COMMERCIAL_WRITE_TOOLS, SUBAGENT_ONLY, WRITE_TOOLS, aisha_schemas

    bound = {s["name"] for s in aisha_schemas()}
    assert not bound & COMMERCIAL_WRITE_TOOLS
    assert not ({"search_vehicles", "get_vehicle_details", "compare_vehicles"} & WRITE_TOOLS)
    assert not bound & set(SUBAGENT_ONLY), "colours, stock, finance stay with the specialists"


def test_the_dispatch_allowlist_follows_the_binding_with_the_flag_on(monkeypatch):
    """tool_node refuses what the model was not handed; the two lists must agree."""
    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", True)
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", True)
    from app.agent import tool_node
    from app.tools import aisha_schemas

    assert tool_node._bound_tools({}, {}) == frozenset(s["name"] for s in aisha_schemas())
    assert "search_vehicles" in tool_node._bound_tools({}, {})


# --------------------------------------------------------------------------- #
# AISHA_CATALOGUE_READS — the prompt
# --------------------------------------------------------------------------- #
def test_the_catalogue_reads_section_is_appended_only_with_the_flag(monkeypatch):
    from app.agent import prompt
    from app.agents import loader

    section = loader.sections("aisha")["CATALOGUE_READS"]
    assert "CATALOGUE_READS" in loader.MULTI_AGENT_ONLY

    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", False)
    off = prompt._delegating_body()
    assert section not in off
    assert off == loader.sections("aisha")["DELEGATING"], "off is byte-identical"

    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", True)
    on = prompt._delegating_body()
    assert on.startswith(loader.sections("aisha")["DELEGATING"])
    assert on.endswith(section)


def test_the_section_names_her_tools_and_what_still_goes_out():
    from app.agents import loader

    text = loader.sections("aisha")["CATALOGUE_READS"]
    for tool in ("search_vehicles", "get_vehicle_details", "compare_vehicles"):
        assert tool in text
    assert "emit_briefs" in text, "a mixed question still briefs, in one call"
    for word in ("stock", "colours", "booking"):
        assert word in text, f"{word} must be named as still delegated"


# --------------------------------------------------------------------------- #
# SPECULATE_ON_MESSAGE — the store
# --------------------------------------------------------------------------- #
def test_offer_message_resets_the_session_store(monkeypatch):
    calls = []

    def fake_select(text, judge=None):  # noqa: ARG001
        calls.append(text)
        return select_mod.Selection(agent="sales", layer="judge", reason="stub")

    monkeypatch.setattr(select_mod, "select", fake_select)
    stale: Future = Future()
    stale.set_result(object())
    with speculate._lock:
        speculate._store["s1"] = {"leftover brief": stale}

    speculate.offer_message("s1", "what seven seaters do you have")
    got = speculate.take("s1")
    assert set(got) == {speculate.MESSAGE_KEY}, "the leftover is gone, the message is in"
    assert got[speculate.MESSAGE_KEY].result(timeout=5).agent == "sales"
    assert calls == ["what seven seaters do you have"]


def test_take_can_drop_the_message_entry_and_keep_the_briefs():
    m: Future = Future()
    b: Future = Future()
    with speculate._lock:
        speculate._store["s2"] = {speculate.MESSAGE_KEY: m, "a brief": b}
    got = speculate.take("s2", include_message=False)
    assert got == {"a brief": b}
    assert speculate.take("s2") == {}, "take pops either way"


def test_an_empty_message_offers_nothing():
    speculate.offer_message("s3", "   ")
    assert speculate.take("s3") == {}


# --------------------------------------------------------------------------- #
# SPECULATE_ON_MESSAGE — plan_turn's use of it
# --------------------------------------------------------------------------- #
def _undecided(text, judge=None):  # noqa: ARG001
    """The free layers had nothing to say; the judge was not supplied."""
    return select_mod.Selection(agent=None, layer="unroutable", reason="no clear winner")


def test_a_single_brief_the_free_layers_cannot_decide_takes_the_message_answer(monkeypatch):
    seen = []

    def local_only(text, judge=None):
        seen.append((text, judge))
        assert judge is None, "the brief's own judge must not run — that is the saving"
        return _undecided(text)

    monkeypatch.setattr(select_mod, "select", local_only)
    hit = select_mod.Selection(agent="finance", layer="judge", reason="judged")
    plan, unroutable = run.plan_turn(
        ["what would the monthly payment be on the X70"],
        judge=lambda *a, **k: "sales",  # would be wrong; must not be consulted
        preselected={speculate.MESSAGE_KEY: _done(hit)},
    )
    assert not unroutable
    assert plan.steps[0].agent == "finance"
    assert "customer message" in plan.steps[0].selection.reason
    assert len(seen) == 1


def test_the_brief_own_free_layers_win_over_the_message(monkeypatch):
    def fast(text, judge=None):  # noqa: ARG001
        return select_mod.Selection(agent="sales", layer="fast_lane", reason="beat finance")

    monkeypatch.setattr(select_mod, "select", fast)
    hit = select_mod.Selection(agent="finance", layer="judge", reason="judged")
    plan, _ = run.plan_turn(
        ["which seven seaters do you have"],
        judge=None,
        preselected={speculate.MESSAGE_KEY: _done(hit)},
    )
    assert plan.steps[0].agent == "sales"
    assert plan.steps[0].selection.layer == "fast_lane"


def test_two_briefs_ignore_the_message_answer(monkeypatch):
    judged = []

    def with_judge(text, judge=None):
        if judge is None:
            return _undecided(text)
        judged.append(text)
        return select_mod.Selection(agent=judge(text, []), layer="judge", reason="judged")

    monkeypatch.setattr(select_mod, "select", with_judge)
    hit = select_mod.Selection(agent="finance", layer="judge", reason="judged")
    plan, _ = run.plan_turn(
        ["which seven seaters do you have", "what do they cost a month"],
        judge=lambda text, agents: "sales",  # noqa: ARG005
        preselected={speculate.MESSAGE_KEY: _done(hit)},
    )
    assert sorted(judged) == sorted(["which seven seaters do you have", "what do they cost a month"])
    assert [s.agent for s in plan.steps] == ["sales", "sales"]


def test_a_message_the_judge_declined_falls_back_to_judging_the_brief(monkeypatch):
    judged = []

    def with_judge(text, judge=None):
        if judge is None:
            return _undecided(text)
        judged.append(text)
        return select_mod.Selection(agent="sales", layer="judge", reason="judged")

    monkeypatch.setattr(select_mod, "select", with_judge)
    declined = select_mod.Selection(agent=None, layer="judge", reason="judge returned 'none'")
    plan, unroutable = run.plan_turn(
        ["what is the warranty on the T2"],
        judge=lambda *a, **k: "sales",
        preselected={speculate.MESSAGE_KEY: _done(declined)},
    )
    assert judged == ["what is the warranty on the T2"]
    assert not unroutable and plan.steps[0].agent == "sales"


def test_a_brief_level_hit_still_beats_everything(monkeypatch):
    def boom(text, judge=None):  # noqa: ARG001
        raise AssertionError("selection ran in-line despite a speculative hit")

    monkeypatch.setattr(select_mod, "select", boom)
    brief_hit = select_mod.Selection(agent="sales", layer="judge", reason="speculated")
    msg_hit = select_mod.Selection(agent="finance", layer="judge", reason="judged")
    plan, _ = run.plan_turn(
        ["what colours does the X50 come in"],
        judge=None,
        preselected={
            "what colours does the X50 come in": _done(brief_hit),
            speculate.MESSAGE_KEY: _done(msg_hit),
        },
    )
    assert plan.steps[0].agent == "sales"


# --------------------------------------------------------------------------- #
# SPECULATE_ON_MESSAGE — the graph's gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, offered",
    [
        ("what seven seater SUVs do you have", True),
        ("Hi", False),
        ("yes please", False),
        ("Hadia Hamad 0501234567", False),
        ("No", False),
    ],
)
def test_the_message_offer_uses_the_prefetch_gate(monkeypatch, text, offered):
    from app.agent import graph

    calls = []
    monkeypatch.setattr(speculate, "offer_message", lambda sid, t: calls.append((sid, t)))
    state = {"messages": [{"role": "user", "content": text}]}
    graph._offer_message_selection(state, "s9", slot_pinned=False)
    assert bool(calls) is offered, (text, calls)


def test_a_pinned_slot_offers_nothing(monkeypatch):
    from app.agent import graph

    calls = []
    monkeypatch.setattr(speculate, "offer_message", lambda sid, t: calls.append(t))
    state = {"messages": [{"role": "user", "content": "what seven seater SUVs do you have"}]}
    graph._offer_message_selection(state, "s9", slot_pinned=True)
    assert calls == []


def test_a_cascade_after_a_tool_round_drops_the_message_entry():
    from app.agent import graph

    m: Future = Future()
    with speculate._lock:
        speculate._store["s4"] = {speculate.MESSAGE_KEY: m}
    assert graph._speculate_take("s4", first_generation=False) == {}
    with speculate._lock:
        speculate._store["s4"] = {speculate.MESSAGE_KEY: m}
    assert graph._speculate_take("s4", first_generation=True) == {speculate.MESSAGE_KEY: m}


def test_the_agent_node_offers_on_the_first_generation_only():
    import inspect

    from app.agent import graph

    src = inspect.getsource(graph._agent_node)
    assert "if SPECULATE_ON_MESSAGE and _multi_agent(cfg) and iterations == 0:" in src
    assert "_offer_message_selection(" in src


# --------------------------------------------------------------------------- #
# ANTHROPIC_KEEPALIVE_SECONDS
# --------------------------------------------------------------------------- #
def test_zero_hands_the_sdk_its_own_default_client(monkeypatch):
    monkeypatch.setattr("app.config.ANTHROPIC_KEEPALIVE_SECONDS", 0)
    from app.services.anthropic_http import pooled_http_client

    assert pooled_http_client() is None


def test_the_keepalive_reaches_the_connection_pool(monkeypatch):
    monkeypatch.setattr("app.config.ANTHROPIC_KEEPALIVE_SECONDS", 300)
    from app.services.anthropic_http import pooled_http_client

    client = pooled_http_client()
    try:
        pool = client._transport._pool
        assert pool._keepalive_expiry == 300.0
        assert pool._max_keepalive_connections == 100, "the SDK's own pool sizes are kept"
    finally:
        client.close()


def test_every_pooled_client_is_built_on_it():
    """Three constructors, one keep-alive — a fourth copy would reopen the gap."""
    import inspect

    from app.agent import graph
    from app.agent.router import classifier

    assert inspect.getsource(graph._build_client).count("pooled_http_client()") == 2
    assert "pooled_http_client()" in inspect.getsource(classifier._get_client)


# --------------------------------------------------------------------------- #
# AISHA_CATALOGUE_READS — her own lookup establishes the car (found live)
# --------------------------------------------------------------------------- #
_SEARCH = {
    "matches": [
        {"vehicle_id": 7, "name": "Jetour X70 FL Plus", "variants": [{"variant_id": 13}, {"variant_id": 14}]},
        {"vehicle_id": 8, "name": "Jetour X70 FL", "variants": [{"variant_id": 15}]},
    ],
    "no_exact_match": False,
}
_DETAILS = {"vehicle": {"vehicle_id": 1, "name": "Jetour G700", "variants": [{"variant_id": 1}]}}
_COMPARE = {
    "compared": True,
    "left": {"vehicle_id": 7, "name": "Jetour X70 FL Plus"},
    "right": {"vehicle_id": 8, "name": "Jetour X70 FL"},
    "rows": [{"label": "Price", "left": "AED 89,000", "right": "AED 79,000"}],
}


def test_a_search_establishes_the_car_it_led_with():
    from app.agent import tool_node

    facts = tool_node._fold_catalogue_facts("search_vehicles", _SEARCH, {"name": "Hanif"})
    assert facts["vehicle_id"] == 7, "first row of a ranked search wins — the specialist's rule"
    assert facts["variant_id"] == 13
    assert facts["vehicle_name"] == "Jetour X70 FL Plus"
    assert facts["name"] == "Hanif", "existing facts survive"


def test_details_and_compare_establish_the_car_too():
    from app.agent import tool_node

    d = tool_node._fold_catalogue_facts("get_vehicle_details", _DETAILS, {})
    assert (d["vehicle_id"], d["vehicle_name"]) == (1, "Jetour G700")
    c = tool_node._fold_catalogue_facts("compare_vehicles", _COMPARE, {})
    assert (c["vehicle_id"], c["vehicle_name"]) == (7, "Jetour X70 FL Plus")


def test_an_error_envelope_or_empty_result_establishes_nothing():
    from app.agent import tool_node

    assert tool_node._fold_catalogue_facts("search_vehicles", {"error": "boom"}, {"a": 1}) == {"a": 1}
    assert tool_node._fold_catalogue_facts("search_vehicles", "not a dict", {"a": 1}) == {"a": 1}
    assert tool_node._fold_catalogue_facts("search_vehicles", {"matches": []}, {}) == {}


def test_the_car_then_satisfies_a_test_drive_step():
    """The failure this fixes: TestDrive requires vehicle_id|booking_ref."""
    from app.agent import graph, tool_node

    facts = tool_node._fold_catalogue_facts("search_vehicles", _SEARCH, {})
    known = graph._known_entities({"session_facts": facts}, {})
    assert "vehicle_id" in known


@pytest.mark.parametrize(
    "flag, multi, voice, expected",
    [(True, True, False, True), (False, True, False, False), (True, False, False, False), (True, True, True, False)],
)
def test_the_fold_runs_only_on_the_flagged_multi_agent_chat_path(monkeypatch, flag, multi, voice, expected):
    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", flag)
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", multi)
    from app.agent import tool_node

    assert tool_node._aisha_catalogue_reads({"voice_call": voice}) is expected


def test_the_dispatch_loop_folds_after_the_identity_fold():
    import inspect

    from app.agent import tool_node

    src = inspect.getsource(tool_node.tool_node)
    assert "_fold_catalogue_facts(name, result, session_facts)" in src
    assert src.index("_fold_session_facts(db, name, result, session_facts)") < src.index(
        "_fold_catalogue_facts(name, result, session_facts)"
    )


# --------------------------------------------------------------------------- #
# One question draws one strip of cars, however many searches it took
# (found live 2026-09-03: four cards under a single answer)
# --------------------------------------------------------------------------- #
def _search(call_id: str, **args) -> dict:
    return {"type": "tool_use", "id": call_id, "name": "search_vehicles", "input": args}


def _run_tools(db, monkeypatch, blocks: list, *, reads=True, multi=True) -> list:
    """Run one tool_node batch and return everything it wrote to the stream."""
    from app.agent import build_config
    from app.agent import tool_node as tool_node_mod

    monkeypatch.setattr("app.config.AISHA_CATALOGUE_READS", reads)
    monkeypatch.setattr("app.config.MULTI_AGENT_ENABLED", multi)
    seen: list = []
    monkeypatch.setattr(tool_node_mod, "_safe_writer", lambda: seen.append)
    state = {
        "messages": [
            {"role": "user", "content": "which seven seaters do you have?"},
            {"role": "assistant", "content": blocks},
        ],
        "customer_id": None,
    }
    tool_node_mod.tool_node(state, build_config(db, "strip-sess"))
    return seen


def _searches(seen: list) -> list:
    return [e for e in seen if e.get("type") == "tool_event" and e.get("tool") == "search_vehicles"]


def test_two_searches_in_one_generation_draw_one_merged_strip(db, monkeypatch):
    seen = _run_tools(db, monkeypatch, [
        _search("t1", min_seats=5, fuel="phev"),
        _search("t2", min_seats=5),
    ])
    events = _searches(seen)
    assert len(events) == 1, "one question, one strip"
    rows = events[0]["output"]["matches"]
    ids = [r["vehicle_id"] for r in rows]
    assert len(ids) == len(set(ids)), "the same car must not appear twice"
    assert 0 < len(ids) <= 3, "the product rule is at most three cards"


def test_a_single_search_still_streams_exactly_as_it_did(db, monkeypatch):
    """The curation only fires on more than one search — the common case must
    keep its own event, arguments and all."""
    seen = _run_tools(db, monkeypatch, [_search("t1", min_seats=5)])
    events = _searches(seen)
    assert len(events) == 1
    assert events[0]["input"] == {"min_seats": 5}, "the real call, not a synthetic strip"


def test_with_the_flag_off_nothing_is_curated(db, monkeypatch):
    """Off means off: the cascade owns the cards on that path and this code
    must not touch the stream at all."""
    seen = _run_tools(db, monkeypatch, [
        _search("t1", min_seats=5, fuel="phev"),
        _search("t2", min_seats=5),
    ], reads=False)
    assert len(_searches(seen)) == 2


def test_the_merge_keeps_the_first_car_seen_and_caps_at_three():
    from app.agent.tool_node import _merged_search_strip

    strip = _merged_search_strip([
        {"matches": [{"vehicle_id": 7}, {"vehicle_id": 8}]},
        {"matches": [{"vehicle_id": 8}, {"vehicle_id": 3}, {"vehicle_id": 9}]},
    ])
    assert [r["vehicle_id"] for r in strip] == [7, 8, 3]


def test_the_merge_falls_back_to_the_nearest_alternatives():
    from app.agent.tool_node import _merged_search_strip

    strip = _merged_search_strip([
        {"matches": [], "nearest_alternatives": [{"vehicle_id": 5}]},
        {"error": "boom"},
        "not a dict",
    ])
    assert [r["vehicle_id"] for r in strip] == [5]
