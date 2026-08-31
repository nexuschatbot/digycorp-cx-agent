"""Load agent prompts from Markdown, by version (T2.2).

WHAT THIS REPLACES. Prompt prose lived in Python constants across
prompts/{base,testdrive,bookingmanagement}.py, so changing a sentence was a code
change and a deploy. It now lives in app/agents/<agent>/<version>.md, versions
sit side by side in git, and `active.json` picks the live one.

THE PROSE MOVED; THE ORDER DID NOT, and that split is deliberate. The
single-agent body INTERLEAVES base and journey sections — PREAMBLE, WHO_YOU_ARE,
GROUNDING, then four testdrive blocks, then base BUTTONS, then more testdrive —
and that sequence is STRUCTURE, not writing. Prose changes often and belongs in
a file an editor can open; order changes rarely and belongs in code where a test
can see it. SINGLE_AGENT_ORDER below is the same tuple compose.py held, moved
rather than rewritten.

CACHED PER (agent, version), AND THAT IS LOAD-BEARING. graph.py builds the
Anthropic `system` blocks with a 1h cache_control breakpoint, and the cached
prefix is `tools -> system`. If this module read files per turn, the assembled
string could differ by a byte between turns — a stray editor newline, a
different file order — and every turn would pay a cache WRITE instead of a read.
The T0.1 baseline measured 1.47M cached tokens read against 80k written; losing
that is roughly a 10x cost increase on the prompt. So: read once, keep forever,
and `reset()` exists for tests rather than for production.

VERSION SELECTION, IN PRECEDENCE ORDER:

    PROMPT_VERSIONS env  ->  active.json  ->  "v1"

The env override is the no-deploy rollback, the same pattern ROUTER_ENABLED set:
`PROMPT_VERSIONS=aisha:v2,sales:v1`. It wins over active.json so a bad
activation can be undone by restarting with a variable set, without waiting for
a merge.

FAIL LOUD ON A MISSING FILE. Unlike retrieval or the cache, there is no degraded
mode here: an agent with no prompt is an agent with no persona, no grounding
rules and no guardrails. That must stop the boot, not answer a customer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("app.agents.loader")

AGENTS_DIR = Path(__file__).resolve().parent
ACTIVE_PATH = AGENTS_DIR / "active.json"

DEFAULT_VERSION = "v1"

# Sections that belong to the FAN-OUT Aisha and must never reach the single
# agent. The exception is named here rather than left implicit, because the
# invariant it breaks — "every aisha section appears in the single-agent body" —
# is a real guard: it is what stops ported prose being silently orphaned by a
# typo'd heading. An unnamed exception would disarm that guard for everything.
#
# DELEGATING is the only one, and the only re-authored prose in the card. It
# tells her that lock_slot and create_booking are no longer hers to call. The
# single agent still calls them, so it must not see it — and the S5 fixture,
# which has guarded the shipped body byte-for-byte since the split, is what
# would catch it if it ever did.
MULTI_AGENT_ONLY: frozenset[str] = frozenset({"DELEGATING"})

# The order the blocks appear in the body that ships. EXTRACTED MECHANICALLY from
# prompts/compose.py::SINGLE_AGENT_ORDER — tests/test_prompt_split.py asserts
# the reassembly is still byte-identical to the fixture that shipped, so a
# reordering here cannot pass unnoticed.
#
# TRANSCRIBED BY SCRIPT, NOT BY HAND. The first attempt at this tuple was
# typed from a reading of compose.py and put ONE_QUESTION in the wrong
# module — 29 entries is exactly the size where an eye slips and a test
# catches it late.
#
# `base` sections come from base/v1.md; everything else from aisha/v1.md, where
# the two journeys' prose was merged (reschedule/cancel folds into the test-drive
# agent, per the plan's roster).
SINGLE_AGENT_ORDER: tuple[tuple[str, str], ...] = (
    ("base", "PREAMBLE"),
    ("base", "WHO_YOU_ARE"),
    ("base", "GROUNDING"),
    ("aisha", "SHOWROOMS_FIRST"),
    ("aisha", "NEAREST_BRANCH"),
    ("aisha", "ONE_QUESTION_SHOWROOM_CARD"),
    ("aisha", "SLOT_ALTERNATIVES"),
    ("aisha", "BRANCH_STICKINESS"),
    ("base", "BUTTONS"),
    ("aisha", "BROCHURE"),
    ("aisha", "COMPARE"),
    ("aisha", "MODEL_LOOKUP"),
    ("aisha", "DOORSTEP"),
    ("aisha", "SECOND_DRIVE_ORDERING"),
    ("base", "LANGUAGE"),
    ("base", "HOW_YOU_TALK"),
    ("base", "TWICE_IS_THE_LIMIT"),
    ("base", "YOUR_VOICE"),
    ("base", "OUTPUT_FORMAT"),
    ("aisha", "FINDING_THE_RIGHT_CAR"),
    ("aisha", "MAKING_THE_VISIT_THEIRS"),
    ("base", "WHAT_WE_REMEMBER"),
    ("aisha", "BOOKING_SEQUENCE"),
    ("aisha", "MANAGING_EXISTING"),
    ("aisha", "RESCHEDULING"),
    ("aisha", "CANCELLING"),
    ("base", "BRINGING_IN_A_PERSON"),
    ("base", "HOLDING_THE_LINE"),
    ("base", "CLOSING"),
)

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_SECTION = re.compile(r"^## ([A-Z_][A-Z0-9_]*)\s*$", re.M)
# The provenance marker the T2.1 migration wrote above every section:
#   <!-- ported verbatim from prompts/testdrive.py::BROCHURE (originally ...) -->
# PARSED, NOT DECORATIVE. evals/differential.py checks tool reachability PER
# JOURNEY, and the two journeys were merged into one `aisha` file — without the
# origin recorded here, a bookingmanagement block naming a testdrive-only tool
# would stop being detectable. See origins().
_ORIGIN = re.compile(r"<!--\s*ported verbatim from prompts/(\w+)\.py::(\w+)")
_FENCE = re.compile(r"```text\n(.*?)\n```", re.S)

# RE-ENTRANT, AND IT MUST BE. `single_agent_body()` takes this lock and then
# calls `sections()`, which takes it again on a cache miss — a plain Lock
# DEADLOCKS there, and it deadlocks at BOOT, before any request is served.
#
# It survived the first test only because that test happened to call
# `sections()` first, populating the cache so the inner acquisition never
# happened. A warm cache hides this completely; a cold process hangs forever.
_lock = threading.RLock()
_cache: dict = {}


class PromptError(RuntimeError):
    """A prompt could not be loaded. Never caught inside this module."""


def _env_overrides() -> dict[str, str]:
    """PROMPT_VERSIONS=aisha:v2,sales:v1 -> {"aisha": "v2", "sales": "v1"}.

    A malformed entry is IGNORED with a warning rather than crashing the boot:
    this variable exists to recover from a bad activation, and a typo in the
    rollback lever must not be worse than the thing it is undoing.
    """
    raw = os.getenv("PROMPT_VERSIONS", "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        agent, sep, version = pair.partition(":")
        if not sep or not agent.strip() or not version.strip():
            log.warning("ignoring malformed PROMPT_VERSIONS entry %r", pair)
            continue
        out[agent.strip()] = version.strip()
    return out


def _active_map() -> dict[str, str]:
    if not ACTIVE_PATH.is_file():
        return {}
    try:
        data = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        raise PromptError(f"active.json is unreadable: {exc}") from exc


def version_for(agent: str) -> str:
    """Which version of this agent is live. env > active.json > v1."""
    return _env_overrides().get(agent) or _active_map().get(agent) or DEFAULT_VERSION


def active_versions() -> dict[str, str]:
    """Every agent's live version — for turn_trace and the audit trail."""
    names = set(_active_map()) | set(_env_overrides()) | {"base", "aisha"}
    return {a: version_for(a) for a in sorted(names)}


def _parse(path: Path) -> dict[str, str]:
    """`## NAME` + ```text fence -> {NAME: exact text}.

    THE FENCE IS WHY THIS IS NOT A NAIVE SPLIT. Leading and trailing whitespace
    is part of the prompt — several blocks end in a blank line that separates
    them from the next — and any markdown-aware strip would silently change the
    bytes the prompt cache is keyed on.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        fence = _FENCE.search(text, m.end(), end)
        if fence is None:
            raise PromptError(f"{path.name}: section {m.group(1)} has no ```text block")
        out[m.group(1)] = fence.group(1)
    # NO SECTIONS IS A LEGITIMATE STATE, for a routing card: sales, testdrive
    # and finance ship their frontmatter in Phase 2 and their prose in Phase 5.
    # It is NOT legitimate for base or aisha, and the difference is declared by
    # `routable` in the frontmatter rather than guessed from emptiness — an
    # accidentally-emptied aisha/v1.md must fail the gate, not be mistaken for a
    # card. See gate.check().
    return out


def frontmatter(agent: str, version: str | None = None) -> dict:
    """The YAML header of one agent version. {} when there is none.

    Read by the activation gate (does this agent need a body?) and, from Phase 3,
    by the orchestrator (domain, utterances, requires, produces, tools).
    """
    version = version or version_for(agent)
    key = ("__fm__", agent, version)
    if key in _cache:
        return _cache[key]
    with _lock:
        if key in _cache:
            return _cache[key]
        path = AGENTS_DIR / agent / f"{version}.md"
        if not path.is_file():
            raise PromptError(f"no prompt file for {agent} {version} at {path}")
        m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        data: dict = {}
        if m:
            try:
                import yaml

                loaded = yaml.safe_load(m.group(1))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:  # noqa: BLE001
                raise PromptError(f"{agent} {version}: unreadable frontmatter: {exc}") from exc
        _cache[key] = data
        return data


def origins(agent: str, version: str | None = None) -> dict[str, str]:
    """{SECTION: source module} from the provenance markers, e.g.
    {"BROCHURE": "testdrive", "RESCHEDULING": "bookingmanagement"}.

    WHY THIS IS NOT JUST A COMMENT. The two journeys' prose merged into one
    aisha file at T2.1, but tool bindings are still per journey — so the
    differential check needs to know which sections belong to which. An
    unmarked section maps to the agent's own name, which is correct for base.
    """
    version = version or version_for(agent)
    key = ("__origin__", agent, version)
    if key in _cache:
        return _cache[key]
    with _lock:
        if key in _cache:
            return _cache[key]
        path = AGENTS_DIR / agent / f"{version}.md"
        if not path.is_file():
            raise PromptError(f"no prompt file for {agent} {version} at {path}")
        text = path.read_text(encoding="utf-8")
        found = {const: module for module, const in _ORIGIN.findall(text)}
        _cache[key] = {name: found.get(name, agent) for name in sections(agent, version)}
        return _cache[key]


def sections(agent: str, version: str | None = None) -> dict[str, str]:
    """Every block of one agent version. Cached per (agent, version)."""
    version = version or version_for(agent)
    key = (agent, version)
    if key in _cache:
        return _cache[key]
    with _lock:
        if key in _cache:
            return _cache[key]
        path = AGENTS_DIR / agent / f"{version}.md"
        if not path.is_file():
            # FAIL LOUD: an agent with no prompt has no guardrails.
            raise PromptError(f"no prompt file for {agent} {version} at {path}")
        _cache[key] = _parse(path)
        log.info("loaded prompt %s %s (%d sections)", agent, version, len(_cache[key]))
        return _cache[key]


def single_agent_body() -> str:
    """The prompt body the live single agent ships.

    Byte-identical to what prompts/compose.single_agent_body() produced —
    tests/test_prompt_split.py asserts it against the same fixture that has
    guarded this text since S5.
    """
    key = ("__single__", tuple(sorted(active_versions().items())))
    if key in _cache:
        return _cache[key]
    with _lock:
        if key in _cache:
            return _cache[key]
        blocks = []
        for agent, name in SINGLE_AGENT_ORDER:
            block = sections(agent).get(name)
            if block is None:
                raise PromptError(f"{agent} {version_for(agent)} is missing {name}")
            blocks.append(block)
        _cache[key] = "".join(blocks)
        return _cache[key]


# The two journeys the SHIPPED single-agent router binds tools for. Their prose
# lives inside aisha/v1.md and is told apart by provenance.
LEGACY_JOURNEYS: tuple[str, ...] = ("testdrive", "bookingmanagement")


def _base_blocks() -> list[str]:
    """Every base section, in the order it ships in."""
    blocks = sections("base")
    return [blocks[n] for a, n in SINGLE_AGENT_ORDER if a == "base"]


def legacy_journey_body(journey: str) -> str:
    """The prompt for one of TODAY'S two journeys: base + that journey's prose.

    ⚠️ `testdrive` MEANS TWO DIFFERENT THINGS AND THIS IS THE OLDER ONE.

    The shipped router binds tools for `testdrive` and `bookingmanagement`, and
    their prose sits inside aisha/v1.md, separated by the provenance markers the
    T2.1 migration wrote. The PHASE 5 sub-agent of the same name is a different
    body: it absorbs bookingmanagement (per the plan's roster) and gives up the
    four car-choosing sections to Sales.

    They are not interchangeable and must not be resolved by the same lookup —
    handing the router the sub-agent's body would silently drop the sections it
    still has tools for, which is the orphaned-rule defect wearing a new hat.
    """
    if journey not in LEGACY_JOURNEYS:
        raise PromptError(f"unknown legacy journey: {journey!r}")
    blocks = sections("aisha")
    origin = origins("aisha")
    own = [
        blocks[n] for a, n in SINGLE_AGENT_ORDER
        if a == "aisha" and origin.get(n) == journey
    ]
    if not own:
        raise PromptError(f"no prose found for legacy journey {journey!r}")
    return "".join(_base_blocks() + own)


def body_for(agent: str) -> str:
    """The prompt body for one agent: every base block, then its own.

    SUB-AGENT SECTIONS ARE TAKEN IN FILE ORDER, not from SINGLE_AGENT_ORDER —
    sales, testdrive and finance have sections that never appear in the
    single-agent body at all, so that tuple cannot order them.

    Legacy journey names route to legacy_journey_body; see its docstring for why
    `testdrive` needs two answers.
    """
    if agent == "base":
        return "".join(_base_blocks())

    own = sections(agent) if (AGENTS_DIR / agent).is_dir() else {}
    if own:
        return "".join(_base_blocks() + list(own.values()))

    if agent in LEGACY_JOURNEYS:
        return legacy_journey_body(agent)

    if (AGENTS_DIR / agent).is_dir():
        raise PromptError(f"{agent} has no prompt body yet")
    raise PromptError(f"unknown agent: {agent!r}")


def reset() -> None:
    """Drop the cache. TESTS ONLY — see the module docstring on why production
    must never do this per turn."""
    _cache.clear()
