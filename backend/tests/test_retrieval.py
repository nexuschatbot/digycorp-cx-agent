"""Retrieval: embeddings, chunking, the index, the cache and the tool (Phase 1).

TWO DISCIPLINES RUN THROUGH THIS FILE.

FIRST, EVERYTHING DEGRADES RATHER THAN RAISES. A missing model, a missing
tokenizer, a dead Redis, an unreadable cache entry — every one of them must cost
retrieval and nothing else. Those paths are tested directly, because they are
the paths that will actually run the day something is misconfigured, and they
are invisible in the happy case.

SECOND, THE MODEL IS 113MB AND MAY NOT BE IN EVERY CHECKOUT. Tests that need it
skip when it is absent instead of failing, so a clone without the vendored asset
still gets a green suite for everything else. The tests that matter most — the
failure modes — need no model at all.

conftest.py fails any test that opens a non-loopback socket, so nothing here can
reach a real Redis. The cache is driven through an injected stub instead, which
exercises every line of the serialisation path except the socket itself.
"""

from __future__ import annotations

import json

import pytest

from app.retrieval import chunk as chunker
from app.retrieval import embed, index
from app.services import cache
from app.tools.knowledge_tools import FENCE_CLOSE, search_knowledge

needs_model = pytest.mark.skipif(
    not embed.available(), reason="embedding model not vendored in this checkout"
)


@pytest.fixture(autouse=True)
def _clean_cache_state():
    """Every test starts with no client and no memo, so ordering cannot matter."""
    cache.set_client(None)
    index.reset()
    yield
    cache.set_client(None)
    index.reset()


class _FakeRedis:
    """The get/set/ping surface cache.py uses. Nothing more.

    THIS IS THE MITIGATION FOR AN UNTESTABLE BRANCH. Redis was chosen over a
    disk cache knowing conftest blocks the socket; injecting a stub means the
    encode/decode path still runs under test rather than shipping unexercised.
    """

    def __init__(self, fail: bool = False):
        self.store: dict[str, bytes] = {}
        self.fail = fail
        self.writes = 0

    def ping(self):
        if self.fail:
            raise ConnectionError("no server")
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("no server")
        return self.store.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("no server")
        self.writes += 1
        self.store[key] = value


# --------------------------------------------------------------------------- #
# Chunking — no model required
# --------------------------------------------------------------------------- #
def test_html_comments_never_reach_a_chunk():
    """They are notes to US — provenance, extraction warnings, "the designer
    reused a Dashing template". Retrieving one into a customer answer would be
    embarrassing, and they are the densest internal text in the corpus."""
    for collection in ("brochures", "knowledge"):
        for c in chunker.collect(collection):
            assert "<!--" not in c.text and "-->" not in c.text, c.citation
            assert "ported from" not in c.text.lower(), c.citation


def test_frontmatter_is_metadata_not_text():
    """A chunk beginning "model_code: JET-G700" would embed the header, not the
    car.

    ASSERTED STRUCTURALLY, not by keyword. The first version of this test also
    banned the string "review_status:" and failed — because g700.md legitimately
    DISCUSSES its own draft status in prose, inside its "Not in this brochure"
    section. Banning a word bans it everywhere, including where it is content.
    """
    for c in chunker.collect("brochures"):
        # model_code appears only in frontmatter, never in any body.
        assert "model_code:" not in c.text, c.citation
        assert not c.body.lstrip().startswith("---"), c.citation
        # The parsed header travelled as metadata, which is the actual claim.
        assert c.review_status
        assert c.meta.get("title") is not None, c.citation


def test_gap_sections_are_marked_rather_than_indexed_as_facts():
    """"Not in this file" is not an answer — but it IS the right response to a
    question we cannot answer, so it is kept and labelled rather than dropped.
    Confusing the two is how "I don't have that" becomes an invented figure."""
    chunks = chunker.collect("knowledge")
    gaps = [c for c in chunks if c.kind == "gap"]
    assert gaps, "the knowledge files all carry a 'Not in this file' section"
    assert all("not in this" in c.heading.lower() for c in gaps)
    facts = [c for c in chunks if c.kind == "fact"]
    assert len(facts) > len(gaps)


def test_every_chunk_carries_a_citation_and_a_review_status():
    """A fact the agent cannot attribute is a fact it should not state
    (FR-CX-07), and five brochures are still draft."""
    for c in chunker.collect("brochures"):
        assert c.source and c.citation
        assert c.review_status
    statuses = {c.review_status for c in chunker.collect("brochures")}
    assert any(s.startswith("draft") for s in statuses), (
        "draft brochures must stay distinguishable from reviewed ones"
    )


def test_the_heading_prefix_is_part_of_the_embedded_text():
    """WORTH +3 ON TOP-1 ACCURACY, measured. A bare paragraph from a spec table
    is a column of numbers with no idea which car it belongs to."""
    for c in chunker.collect("knowledge")[:20]:
        assert c.text.startswith(f"{c.source} — ")
        assert c.body in c.text
        # `body` is what a customer would be shown, and must NOT carry it.
        assert not c.body.startswith(c.source)


def test_no_chunk_exceeds_the_cap():
    for collection in ("brochures", "knowledge"):
        for c in chunker.collect(collection):
            assert len(c.body) <= chunker.MAX_CHARS, c.citation


def test_chunking_is_deterministic():
    """Two builds of the same content must produce the same order, or the
    content hash is meaningless and the cache thrashes."""
    a = [c.citation for c in chunker.collect("knowledge")]
    b = [c.citation for c in chunker.collect("knowledge")]
    assert a == b


def test_the_content_hash_changes_with_the_content_and_the_chunker(monkeypatch):
    """THE CACHE GATE. It covers the chunker settings as well as the files:
    change MAX_CHARS and every vector differs, which no content-only hash would
    catch — and stale vectors misroute with no error anywhere."""
    before = chunker.content_hash("knowledge")
    assert before == chunker.content_hash("knowledge")
    monkeypatch.setattr(chunker, "MAX_CHARS", chunker.MAX_CHARS + 1)
    assert chunker.content_hash("knowledge") != before


def test_an_unknown_collection_is_empty_not_an_error():
    assert chunker.collect("does-not-exist") == []
    assert chunker.content_hash("does-not-exist")


# --------------------------------------------------------------------------- #
# The cache — no model required
# --------------------------------------------------------------------------- #
def test_with_no_redis_everything_degrades_quietly():
    """A dead cache costs the CACHE, never the turn. There is no configuration
    in which a Redis problem produces a customer-visible error."""
    cache.set_client(None)
    cache._state["failed"] = True
    assert cache.enabled() is False
    assert cache.get("k") is None
    assert cache.set("k", b"v") is False


def test_a_raising_client_is_swallowed_on_both_read_and_write():
    cache.set_client(_FakeRedis(fail=True))
    assert cache.get("k") is None
    assert cache.set("k", b"v") is False


def test_a_working_client_round_trips_and_is_prefixed():
    fake = _FakeRedis()
    cache.set_client(fake)
    assert cache.set("mykey", b"payload") is True
    assert cache.get("mykey") == b"payload"
    assert all(k.startswith(cache.PREFIX) for k in fake.store), fake.store


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #
@needs_model
def test_the_index_round_trips_through_the_cache_and_is_not_rebuilt():
    """The serialisation path, exercised end to end against a stub. A miss costs
    ~14 seconds of embedding; a hit must cost a deserialise."""
    fake = _FakeRedis()
    cache.set_client(fake)

    chunks, vectors = index.load("knowledge")
    assert len(chunks) == len(vectors)
    writes_after_build = fake.writes
    assert writes_after_build == 2, "vectors and chunks are two keys"

    index.reset()  # drop the memo, keep the cache
    chunks2, vectors2 = index.load("knowledge")
    assert fake.writes == writes_after_build, "a cache hit must not re-write"
    assert [c.citation for c in chunks2] == [c.citation for c in chunks]
    assert vectors2.shape == vectors.shape


@needs_model
def test_a_corrupt_cache_entry_rebuilds_rather_than_raising():
    """Untrusted input by construction. An unreadable entry must cost a rebuild,
    never a 500."""
    fake = _FakeRedis()
    cache.set_client(fake)
    index.load("knowledge")
    index.reset()

    for key in list(fake.store):
        if key.endswith(":vectors"):
            fake.store[key] = b"not an npy file"
    chunks, vectors = index.load("knowledge")
    assert len(chunks) == len(vectors) > 0


@needs_model
def test_a_length_mismatch_between_the_two_keys_rebuilds():
    """The keys can only disagree if something wrote them separately. Trusting
    them would mis-cite every chunk — an answer attributed to the wrong file."""
    fake = _FakeRedis()
    cache.set_client(fake)
    chunks, _ = index.load("knowledge")
    index.reset()
    for key in list(fake.store):
        if key.endswith(":chunks"):
            trimmed = json.loads(fake.store[key])[:3]
            fake.store[key] = json.dumps(trimmed).encode()
    rebuilt, vectors = index.load("knowledge")
    assert len(rebuilt) == len(vectors) == len(chunks)


@needs_model
def test_search_ranks_the_right_document_in_the_top_three():
    """Measured at 13/13 top-3 on the eval set the chunker was chosen against.
    Top-1 is 11/13, which is why the tool returns five passages for the model to
    read rather than one it must trust."""
    cases = [
        ("what is the warranty on the Dashing", "warranty"),
        ("how long until I get my car", "delivery"),
        ("when do I get my number plates", "registration"),
        ("any offers this month", "promotions"),
    ]
    for query, expected in cases:
        sources = [c.source for c, _ in index.search(query, k=3)]
        assert expected in sources, f"{query!r} -> {sources}"


@needs_model
def test_naming_the_car_does_not_bury_the_policy_document():
    """THE CASE THE EVAL SET ABOVE MISSES, and why it missed it: every query in
    it is bare ("what is the warranty on the Dashing" names a model the
    brochures barely mention). Add a model with a full brochure and the
    brochure takes every slot on volume alone — measured 2026-08-31,
    warranty.md fell from rank 1 to rank 39 on the same question."""
    cases = [
        ("what warranty comes with the X70 FL Plus", "warranty"),
        ("how long is the delivery time if I order an X70 FL Plus", "delivery"),
        ("how long does registration take for my X50", "registration"),
        # Both of these missed at floor=1, losing INSIDE the knowledge
        # collection by under 0.005 — see index.COLLECTION_FLOOR.
        ("any offers on the T2 i-DM this month", "promotions"),
        ("is there a discount on the X70 FL Plus", "promotions"),
    ]
    for query, expected in cases:
        sources = [c.source for c, _ in index.search(query, k=5)]
        assert expected in sources, f"{query!r} -> {sources}"


@needs_model
def test_the_floor_costs_one_slot_and_no_more():
    """A pure catalogue question still gets the brochures. The floor reserves
    one slot per collection, not a half share."""
    hits = index.search("how many seats does the X70 FL Plus have", k=5)
    collections = [c.collection for c, _ in hits]
    assert collections.count("brochures") == 3
    assert collections.count("knowledge") == index.COLLECTION_FLOOR


@needs_model
def test_the_floor_never_pads_beyond_what_a_collection_has():
    hits = index.search("what is the warranty", k=2)
    assert len(hits) == 2
    assert [s for _, s in hits] == sorted((s for _, s in hits), reverse=True)


@needs_model
def test_an_arabic_question_reaches_the_arabic_sections():
    """The whole reason a multilingual model was vendored over an English one
    5x smaller. If this ever fails, that decision stopped paying for itself."""
    hits = index.search("ما هو الضمان", k=3)
    assert hits
    assert any(c.source == "warranty" for c, _ in hits)


@needs_model
def test_search_is_scoreless_at_the_boundary_but_ordered():
    """Scores are returned for inspection and are deliberately NOT thresholded:
    on this corpus every similarity sits between roughly 0.75 and 0.87, so any
    constant admits everything or nothing."""
    hits = index.search("what is the warranty", k=5)
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.5 < s < 1.0 for s in scores)


def test_an_empty_query_searches_nothing():
    assert index.search("") == []
    assert index.search("   ") == []


def test_search_returns_nothing_when_the_model_is_unavailable(monkeypatch):
    """The degraded path, forced. A checkout without the vendored asset must
    answer from the catalogue, not raise."""
    monkeypatch.setattr(embed, "embed_query", lambda *a, **k: None)
    assert index.search("what is the warranty") == []


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
@needs_model
def test_every_passage_is_fenced_and_attributed():
    """Document text is the first untrusted-SHAPED input this system puts in a
    prompt. The fence is structural — applied here, not left to the prompt to
    remember — while the only author is still us."""
    out = search_knowledge(None, "s", query="what is the warranty on the Dashing")
    assert out["found"] is True
    for p in out["passages"]:
        assert p["text"].startswith("<<<SOURCE ")
        assert p["text"].rstrip().endswith(FENCE_CLOSE)
        assert p["source"] and p["section"] is not None
        assert p["kind"] in {"fact", "gap"}


@needs_model
def test_the_tool_returns_a_rank_and_never_a_raw_score():
    """A float invites the model to compare it against a threshold it invents.
    A rank cannot be misread that way."""
    out = search_knowledge(None, "s", query="how long until I get my car")
    ranks = [p["relevance_rank"] for p in out["passages"]]
    assert ranks == list(range(1, len(ranks) + 1))
    assert "score" not in json.dumps(out)


def test_a_missing_query_is_an_error_and_an_unanswerable_one_is_not():
    """"We have no document about that" is a real answer; an empty call is a
    mistake. They must not look the same to the agent."""
    assert search_knowledge(None, "s", query="  ")["error"] == "missing_query"


def test_the_tool_is_not_bound_yet_and_that_is_deliberate(monkeypatch):
    """T1.5's whole decision. Binding changes TOOL_SCHEMAS, which graph.py says
    must stay byte-stable for the prompt cache, and needs a line of prompt prose
    telling Aisha when to call it — both belong with the MD prompts in Phase 2.

    When Phase 2 binds it, delete this test in the same commit.
    """
    from app.tools import TOOL_SCHEMAS

    assert "search_knowledge" not in {s["name"] for s in TOOL_SCHEMAS}


@needs_model
def test_a_two_topic_question_reaches_both_documents():
    """The tester's exact words. Both policies live in ONE collection, so the
    per-collection floor filled both knowledge slots with warranty.md and the
    customer was told we publish no delivery time (reproduced live in a
    browser, 2026-09-04). The floor now reserves a collection's best chunk
    from each distinct DOCUMENT, drawn from a wider candidate pool."""
    sources = [c.source for c, _ in index.search(
        "What is the warranty and delivery information for the T2?", k=5)]
    assert "warranty" in sources and "delivery" in sources, sources


@needs_model
def test_the_reserved_knowledge_slots_come_from_different_documents():
    hits = index.search("What is the warranty and delivery information for the T2?", k=5)
    knowledge = [c.source for c, _ in hits if c.collection == "knowledge"]
    assert len(set(knowledge)) >= 2, knowledge


def test_each_collection_offers_more_candidates_than_slots():
    """Five candidates for five slots left the per-document floor nothing to
    choose from: on that question the knowledge collection's top five were
    five warranty chunks."""
    assert index.CANDIDATES_PER_SLOT >= 4
