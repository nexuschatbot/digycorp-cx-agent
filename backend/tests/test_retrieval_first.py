"""RETRIEVAL_FIRST (2026-09-02): the local index answers before the cascade.

Measured live, a warranty question spent five model calls (~14s) to execute a
document lookup the local index resolves in ~50ms. With the flag on, the top
passages ride into Aisha's VOLATILE prompt tail as a pre-supplied
search_knowledge result and a fact question becomes one Sonnet call.

Everything here is offline: index.search is stubbed, no embedding model and no
network is touched, and the flag's shipped default (off) must leave every
prompt byte-identical.
"""

from __future__ import annotations

from app.agent import graph
from app.agent.prompt import system_prompt_parts
from app.retrieval.chunk import Chunk


def _chunk(body: str, source: str = "warranty", heading: str = "Petrol models") -> Chunk:
    return Chunk(
        text=body, body=body, source=source, collection="knowledge", heading=heading
    )


# --------------------------------------------------------------------------- #
# The prompt half
# --------------------------------------------------------------------------- #
def test_passages_land_in_the_volatile_tail_never_the_stable_prefix():
    """The passages change every message; putting them ahead of the cache
    breakpoint would rewrite the whole ~22k prefix per turn — the exact
    failure the stable/volatile split exists to prevent."""
    stable, volatile = system_prompt_parts(
        multi_agent=True, retrieved_passages="<<<SOURCE warranty>>>\n10 years\n<<<END SOURCE>>>"
    )
    assert "10 years" in volatile and "10 years" not in stable
    assert "PRE-FETCHED FOR THIS MESSAGE" in volatile


def test_no_passages_means_a_byte_identical_prompt():
    """Off is off: everywhere the flag is unset, nothing may move."""
    assert system_prompt_parts(multi_agent=True) == system_prompt_parts(
        multi_agent=True, retrieved_passages=None
    )


# --------------------------------------------------------------------------- #
# The prefetch half
# --------------------------------------------------------------------------- #
def _state(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}]}


def test_prefetch_formats_hits_like_search_knowledge(monkeypatch):
    from app.retrieval import index

    monkeypatch.setattr(
        index, "search", lambda q, **k: [(_chunk("Engine: 10 years or 1,000,000 km"), 0.87)]
    )
    out = graph._prefetch_passages(_state("what is the warranty of the Dashing"))
    assert out is not None
    assert "<<<SOURCE warranty#Petrol models>>>" in out
    assert "10 years or 1,000,000 km" in out


def test_prefetch_skips_greetings_and_fragments(monkeypatch):
    from app.retrieval import index

    def boom(q, **k):  # noqa: ARG001 — reaching the index at all is the failure
        raise AssertionError("greeting reached the index")

    monkeypatch.setattr(index, "search", boom)
    assert graph._prefetch_passages(_state("hi")) is None
    assert graph._prefetch_passages(_state("yes ok")) is None


def test_prefetch_failure_costs_nothing(monkeypatch):
    from app.retrieval import index

    def boom(q, **k):
        raise RuntimeError("index on fire")

    monkeypatch.setattr(index, "search", boom)
    assert graph._prefetch_passages(_state("what is the warranty of the Dashing")) is None


def test_prefetch_respects_the_character_cap(monkeypatch):
    from app.retrieval import index

    big = [( _chunk("x" * 3000, source=f"s{i}"), 0.8) for i in range(4)]
    monkeypatch.setattr(index, "search", lambda q, **k: big)
    out = graph._prefetch_passages(_state("tell me everything about the warranty"))
    assert out is not None and len(out) <= graph._PREFETCH_MAX_CHARS + 200


# --------------------------------------------------------------------------- #
# The wiring
# --------------------------------------------------------------------------- #
def test_the_prefetch_is_gated_on_flag_path_and_first_generation():
    """Flag off (the shipped default) or a later generation of the turn — the
    compose call after a cascade — must never pay the tail. Pinned at the
    source like the trace-recording tests."""
    import inspect

    src = inspect.getsource(graph._agent_node)
    assert "RETRIEVAL_FIRST and _multi_agent(cfg) and iterations == 0" in src


def test_the_prefetch_reserves_two_documents_per_collection():
    """One reserved slot per collection pre-fetched warranty.md and no
    delivery passage for "warranty and delivery for the T2" — the live case
    on the path that never reaches a specialist. Two per collection, one
    global slot to keep a single-topic question's brochure depth."""
    assert graph._PREFETCH_FLOOR == 2
    assert graph._PREFETCH_K == graph._PREFETCH_FLOOR * len(__import__("app.retrieval.index", fromlist=["x"]).PREFETCH_COLLECTIONS) + 1
