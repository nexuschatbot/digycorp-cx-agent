"""Prompts as versioned Markdown: the loader, the gate, the rollback (Phase 2).

REPLACES tests/test_prompt_split.py, which guarded the S5 split of one Python
literal into per-journey Python constants. Those constants are gone — the prose
lives in app/agents/{base,aisha}/v1.md now — but the invariant they protected is
not, so it is carried forward here against the SAME fixture that has guarded
this text since S5.

WHAT MUST STAY TRUE THROUGH TWO MIGRATIONS:

  1. The assembled body is BYTE-IDENTICAL to what shipped. Not "equivalent",
     not "the same modulo whitespace" — identical, because it is the cached
     prompt prefix and a single byte turns every cache read into a write.
  2. Every block appears exactly once, in the order it shipped in.
  3. A journey's body is a strict subset of the single-agent body.

AND WHAT PHASE 2 ADDS:

  4. A broken version is REFUSED, not served.
  5. PROMPT_VERSIONS rolls back without a deploy.
  6. Files are read ONCE, not per turn.

Offline: files and string arithmetic. No model, no database.
"""

from __future__ import annotations

import pathlib

import pytest

from app.agents import gate, loader

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "prompt_body_original.txt"
AGENTS_DIR = pathlib.Path(loader.AGENTS_DIR)


@pytest.fixture(autouse=True)
def _clean_loader():
    loader.reset()
    yield
    loader.reset()


# --------------------------------------------------------------------------- #
# 1-3: what survived the migration
# --------------------------------------------------------------------------- #
def test_the_markdown_reassembles_byte_identically_to_the_body_that_shipped():
    """THE LOAD-BEARING TEST OF PHASE 2.

    The prose moved from one Python literal (pre-S5) to per-journey constants
    (S5) to Markdown (T2.1), and this fixture has been the witness at every
    step. Regenerating it is NOT how you fix a failure here: if the body must
    change, change it in the MD and update the fixture in the same commit,
    deliberately.

    IT HAS BEEN REGENERATED EXACTLY ONCE, AT PHASE 6, and that was the point of
    Phase 6 — "the only deliberate re-authoring of battle-tested prose". Four
    changes, each a decision rather than an edit:

      - rule 13's scope: indicative on-road prices and instalments are now
        Aisha's; the commitment is not;
      - HOLDING THE LINE stops forbidding financing outright;
      - the handoff may not promise a time, because it joins a queue and not a
        phone that rings;
      - the third-person leak the T0.1 baseline caught, where notes-to-self
        written before a tool call were delivered to the customer verbatim.

    The diff was read line by line before the fixture was accepted. Everything
    since S5 was a MOVE; this was the first CHANGE.
    """
    assert loader.single_agent_body() == FIXTURE.read_text(encoding="utf-8")


def test_the_live_prompt_module_serves_that_same_body():
    from app.agent import prompt

    assert prompt._PROMPT_BODY == FIXTURE.read_text(encoding="utf-8")


def test_every_block_in_the_order_exists_and_is_used_exactly_once():
    """A renamed section is a silently SHORTER prompt — the failure most likely
    to survive a human read."""
    seen = list(loader.SINGLE_AGENT_ORDER)
    assert len(seen) == len(set(seen)), "a block is composed twice"

    available = {
        ("base", n) for n in loader.sections("base")
    } | {("aisha", n) for n in loader.sections("aisha")}
    ordered = set(loader.SINGLE_AGENT_ORDER)
    assert ordered - available == set(), "the order names a section that does not exist"
    assert available - ordered == set(), "a section exists but is never composed"


def test_a_journey_body_is_a_strict_subset_of_the_single_agent_body():
    full = loader.single_agent_body()
    for agent in ("aisha",):
        for text in loader.sections(agent).values():
            assert text in full


def test_each_journey_carries_every_base_block():
    """Persona, grounding, language and guardrails are not optional for anyone."""
    body = loader.body_for("aisha")
    for text in loader.sections("base").values():
        assert text in body


def test_an_unknown_agent_is_refused_rather_than_silently_empty():
    """A body of "" is a prompt with no persona and no guardrails. It must raise."""
    with pytest.raises(loader.PromptError):
        loader.body_for("fallback")
    with pytest.raises(loader.PromptError):
        loader.sections("nonexistent")


# --------------------------------------------------------------------------- #
# Whitespace: the reason the sections are fenced
# --------------------------------------------------------------------------- #
def test_leading_and_trailing_whitespace_survives_the_round_trip():
    """Several blocks end in a blank line that separates them from the next. A
    markdown-aware strip would silently change the bytes the cache is keyed on,
    which is why every section sits inside a ```text fence."""
    blocks = loader.sections("base")
    assert any(t.endswith("\n\n") for t in blocks.values()), (
        "no block ends in a blank line — the fence may have been stripped"
    )
    joined = "".join(
        loader.sections(a)[n] for a, n in loader.SINGLE_AGENT_ORDER
    )
    assert joined == FIXTURE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 4: the activation gate
# --------------------------------------------------------------------------- #
def test_the_live_versions_all_pass_the_gate():
    for agent, report in gate.verify_active().items():
        assert report.ok, report.render()


def test_a_version_missing_a_guardrail_is_refused(tmp_path, monkeypatch):
    """The whole point of the gate. An edit that tidies away the no-markdown
    rule removes a guardrail, and nothing else in the system would notice."""
    fake = tmp_path / "base"
    fake.mkdir()
    # Everything base needs, EXCEPT any mention of markdown.
    body = "\n".join(
        f"## {n}\n```text\none question. a human. record_consent. licence.\n```"
        for _, n in loader.SINGLE_AGENT_ORDER if _ == "base"
    )
    (fake / "v9.md").write_text("---\nagent: base\nroutable: false\n---\n\n" + body)
    monkeypatch.setattr(loader, "AGENTS_DIR", tmp_path)
    loader.reset()

    report = gate.check("base", "v9")
    assert not report.ok
    assert any("markdown" in p for p in report.missing_phrases), report.render()


def test_a_version_missing_a_composed_section_is_refused(tmp_path, monkeypatch):
    fake = tmp_path / "base"
    fake.mkdir()
    (fake / "v9.md").write_text(
        "---\nagent: base\nroutable: false\n---\n\n"
        "## PREAMBLE\n```text\none question markdown human record_consent licence\n```"
    )
    monkeypatch.setattr(loader, "AGENTS_DIR", tmp_path)
    loader.reset()

    report = gate.check("base", "v9")
    assert not report.ok
    assert len(report.missing_sections) > 5, report.render()


def test_an_emptied_body_is_refused_and_not_mistaken_for_a_routing_card(
    tmp_path, monkeypatch
):
    """THE HOLE THE FIRST VERSION OF THE GATE HAD. It treated "no sections" as
    "this must be a routing card" — which would have let an accidentally
    truncated aisha/v1.md sail through, removing every guardrail from the live
    prompt. Bodylessness is legal only when the file DECLARES itself routable.
    """
    fake = tmp_path / "aisha"
    fake.mkdir()
    (fake / "v9.md").write_text("---\nagent: aisha\nroutable: false\n---\n\n")
    monkeypatch.setattr(loader, "AGENTS_DIR", tmp_path)
    loader.reset()

    report = gate.check("aisha", "v9")
    assert not report.ok
    assert "not declared routable" in " ".join(report.missing_sections)


def test_a_routing_card_with_no_body_is_accepted():
    """sales, testdrive and finance ship frontmatter in Phase 2 and prose in
    Phase 5. A gate that failed on every boot for two phases is a gate people
    turn off."""
    for agent in ("sales", "testdrive", "finance"):
        report = gate.check(agent)
        assert report.ok and report.body_only, report.render()


# --------------------------------------------------------------------------- #
# 5: version selection and rollback
# --------------------------------------------------------------------------- #
def test_active_json_picks_the_live_version():
    assert loader.version_for("aisha") == "v1"
    assert loader.active_versions()["base"] == "v1"


def test_the_env_override_wins_over_active_json(monkeypatch):
    """THE NO-DEPLOY ROLLBACK, same pattern ROUTER_ENABLED set. A bad activation
    is undone by restarting with a variable, not by waiting for a merge."""
    monkeypatch.setenv("PROMPT_VERSIONS", "aisha:v7,sales:v3")
    assert loader.version_for("aisha") == "v7"
    assert loader.version_for("sales") == "v3"
    assert loader.version_for("base") == "v1", "unlisted agents are unaffected"


def test_a_malformed_override_is_ignored_rather_than_fatal(monkeypatch):
    """This variable exists to RECOVER from a bad activation. A typo in the
    rollback lever must not be worse than the thing it is undoing."""
    monkeypatch.setenv("PROMPT_VERSIONS", "garbage,,aisha:v7,:v2,sales:")
    assert loader.version_for("aisha") == "v7"
    assert loader.version_for("base") == "v1"


# --------------------------------------------------------------------------- #
# 6: the prompt cache depends on this being read once
# --------------------------------------------------------------------------- #
def test_files_are_read_once_not_per_call(monkeypatch):
    """If this module read files per turn, the assembled string could vary by a
    byte and every turn would pay a cache WRITE instead of a read. The T0.1
    baseline measured 1.47M cached tokens read against 80k written."""
    loader.reset()
    reads = {"n": 0}
    real = pathlib.Path.read_text

    def counting(self, *a, **k):
        if self.suffix == ".md":
            reads["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", counting)
    for _ in range(20):
        loader.single_agent_body()
    assert reads["n"] <= 4, f"{reads['n']} file reads for 20 calls"


def test_the_assembled_body_is_stable_across_calls():
    """Byte-stability is what the cache is keyed on."""
    first = loader.single_agent_body()
    for _ in range(5):
        assert loader.single_agent_body() == first


# --------------------------------------------------------------------------- #
# Provenance: the migration is auditable, not trusted
# --------------------------------------------------------------------------- #
def test_every_section_records_where_it_was_ported_from():
    """The plan's rule zero is "port, don't re-author". These markers are what
    make that checkable a year from now — and they are load-bearing besides:
    evals/differential.py recovers the journey split from them."""
    for agent in ("base", "aisha"):
        origins = loader.origins(agent)
        assert set(origins) == set(loader.sections(agent))
        assert all(v for v in origins.values())


def test_the_journey_split_survives_the_merge():
    """testdrive and bookingmanagement merged into one aisha file, but tool
    bindings are still per journey. Losing this would make a bookingmanagement
    block naming a testdrive-only tool invisible to the differential check."""
    origins = loader.origins("aisha")
    assert set(origins.values()) == {"testdrive", "bookingmanagement"}
    assert origins["RESCHEDULING"] == "bookingmanagement"
    assert origins["BROCHURE"] == "testdrive"


def test_the_deleted_python_prose_has_not_come_back():
    """Two copies of the prompt is one copy that drifts, and the one that drifts
    is always the one nobody is looking at."""
    prompts = AGENTS_DIR.parent / "agent" / "prompts"
    for gone in ("base.py", "testdrive.py", "bookingmanagement.py", "compose.py"):
        assert not (prompts / gone).exists(), f"{gone} is back — which copy ships?"
    assert (prompts / "anchors.py").exists(), "the DYNAMIC anchors must stay in code"


# --------------------------------------------------------------------------- #
# The journey seam: two names, one file
# --------------------------------------------------------------------------- #
def test_the_live_journeys_still_get_their_own_bodies():
    """THE AWKWARD SEAM, now explicit. The shipped router binds tools per JOURNEY
    (testdrive, bookingmanagement) and asks for a prompt per journey. Their prose
    merged into one aisha/v1.md at T2.1, so those bodies are RECONSTRUCTED from
    the provenance markers.

    RE-POINTED AT T5.2. `body_for("testdrive")` used to answer this; it now
    answers for the PHASE 5 SUB-AGENT of the same name, which is a different
    body — it absorbs bookingmanagement and gives the car-choosing sections to
    Sales. The legacy lookup got its own function precisely so the two can never
    be confused, because handing the router the sub-agent's body would silently
    drop sections it still has tools for.
    """
    td = loader.legacy_journey_body("testdrive")
    bm = loader.legacy_journey_body("bookingmanagement")
    blocks = loader.sections("aisha")

    assert blocks["RESCHEDULING"] in bm and blocks["RESCHEDULING"] not in td
    assert blocks["BROCHURE"] in td and blocks["BROCHURE"] not in bm
    for base_text in loader.sections("base").values():
        assert base_text in td and base_text in bm


def test_the_two_meanings_of_testdrive_resolve_to_different_bodies():
    """`testdrive` names BOTH a live router journey and a Phase 5 sub-agent, and
    they are not interchangeable:

        legacy journey  base + the 13 testdrive-origin sections of aisha/v1.md
        sub-agent       base + testdrive/v1.md, which absorbs bookingmanagement
                        and gives BROCHURE/COMPARE/MODEL_LOOKUP to Sales

    Resolving both through one lookup is how the router silently loses the
    sections it still binds tools for.
    """
    legacy = loader.legacy_journey_body("testdrive")
    sub = loader.body_for("testdrive")
    assert legacy != sub

    blocks = loader.sections("aisha")
    assert blocks["BROCHURE"] in legacy, "the router still owns brochures"
    assert blocks["BROCHURE"] not in sub, "the sub-agent gave them to Sales"
    assert blocks["RESCHEDULING"] in sub, "the sub-agent absorbed bookingmanagement"
    assert blocks["RESCHEDULING"] not in legacy


def test_every_sub_agent_now_has_a_body():
    """Phase 2 shipped them bodyless on purpose; T5.2 fills them in."""
    for agent in ("sales", "testdrive", "finance"):
        assert loader.sections(agent), f"{agent} has no prompt body"
        assert len(loader.body_for(agent)) > 20_000


def test_a_sub_agent_body_carries_every_base_block():
    """Grounding and data minimisation are not optional for anyone."""
    for agent in ("sales", "testdrive", "finance"):
        body = loader.body_for(agent)
        for base_text in loader.sections("base").values():
            assert base_text in body, agent


def test_an_unknown_agent_is_still_refused():
    with pytest.raises(loader.PromptError):
        loader.body_for("concierge")
    with pytest.raises(loader.PromptError):
        loader.legacy_journey_body("sales")


def test_the_journey_bodies_do_not_leak_into_each_other():
    td = loader.legacy_journey_body("testdrive")
    bm = loader.legacy_journey_body("bookingmanagement")
    origins = loader.origins("aisha")
    for name, text in loader.sections("aisha").items():
        if origins[name] == "testdrive":
            assert text in td and text not in bm, name
        else:
            assert text in bm and text not in td, name
