"""Catalogue facts as retrieval passages (RETRIEVAL_FIRST, 2026-09-02).

A colour, price or trim question used to need the cascade — four to five
model calls, 14-23s measured — to run get_vehicle_details. The catalogue is
static per seed, so it is rendered into the retrieval index and the pre-fetch
hands Aisha the answer before she speaks. Offline throughout: the chunks are
rendered from the seeded test database and the embedding model is stubbed or
skipped, exactly as test_retrieval does.
"""

from __future__ import annotations

import pytest

from app.retrieval import catalogue_chunks, embed, index
from app.services import cache

needs_model = pytest.mark.skipif(
    not embed.available(), reason="embedding model not vendored in this checkout"
)


@pytest.fixture(autouse=True)
def _clean_index_state():
    cache.set_client(None)
    index.reset()
    yield
    cache.set_client(None)
    index.reset()


def test_every_model_gets_overview_colours_and_prices(db):
    chunks = catalogue_chunks.render(db)
    from sqlalchemy import select

    from app.models import VehicleModel

    n = len(db.scalars(select(VehicleModel)).all())
    assert n > 0 and len(chunks) == 3 * n
    assert {c.heading for c in chunks} == {"Overview", "Colours", "Prices and variants"}
    assert all(c.collection == "catalogue" and c.kind == "fact" for c in chunks)
    assert all(c.meta.get("vehicle_id") for c in chunks)


def test_colours_and_list_prices_are_worded_as_the_tools_word_them(db):
    by = {c.citation: c for c in catalogue_chunks.render(db)}
    colours = by["jet-g700#Colours"].body
    assert "Night Black" in colours and "Interior colours: Orange and Ivory" in colours
    assert "stock check" in colours  # availability stays a tool call
    prices = by["jet-g700#Prices and variants"].body
    assert "AED 169,000" in prices and "AED 240,450" in prices
    assert "Comfort AED 169,000" in prices and "Flagship AED 240,450" in prices
    assert "on-road" in prices  # the finance carve-out is named, not quoted
    assert "instalment" in prices


def test_embedded_text_leads_with_the_model_name_not_the_code(db):
    c = next(x for x in catalogue_chunks.render(db) if x.citation == "jet-g700#Colours")
    assert c.text.startswith("Jetour G700 — Colours\n")
    assert "JET-G700" not in c.text


def test_content_hash_tracks_the_rendered_facts(db):
    from sqlalchemy import select

    from app.models import VehicleModel

    before = catalogue_chunks.content_hash(db)
    assert before == catalogue_chunks.content_hash(db)
    m = db.scalars(select(VehicleModel).where(VehicleModel.model_code == "JET-G700")).one()
    m.exterior_colours = [*(m.exterior_colours or []), "Racing Red"]
    db.flush()
    assert catalogue_chunks.content_hash(db) != before


def test_collect_degrades_to_nothing_without_a_database(monkeypatch):
    """A missing catalogue must cost a collection, never the boot."""
    class _Broken:
        def __enter__(self):
            raise RuntimeError("no database")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Broken())
    assert catalogue_chunks.collect() == []


def test_the_index_builds_the_catalogue_from_the_database(db, monkeypatch):
    import numpy as np

    monkeypatch.setattr(catalogue_chunks, "collect", lambda db=None: catalogue_chunks.render(db) if db is not None else catalogue_chunks.render(_db))
    _db = db
    monkeypatch.setattr(embed, "embed_passages", lambda texts: np.zeros((len(texts), 4), dtype="float32"))
    loaded = index.load("catalogue")
    assert loaded is not None
    chunks, vectors = loaded
    assert len(chunks) == len(vectors) and chunks[0].collection == "catalogue"


def test_search_knowledge_stays_documents_only():
    """The specialists cite get_variants for prices; a knowledge search that
    returned the price list would have them quoting a passage instead."""
    assert "catalogue" not in index.KNOWLEDGE_COLLECTIONS
    assert "catalogue" in index.PREFETCH_COLLECTIONS
    assert "catalogue" in index.COLLECTIONS


def test_prefetch_asks_for_all_three_collections_with_a_floor_of_one(monkeypatch):
    from app.agent import graph

    seen = {}

    def fake_search(q, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(index, "search", fake_search)
    graph._prefetch_passages({"messages": [{"role": "user", "content": "what colours does the Dashing come in"}]})
    assert seen["collections"] == index.PREFETCH_COLLECTIONS
    assert seen["k"] == graph._PREFETCH_K and seen["floor"] == graph._PREFETCH_FLOOR
    # Three collections at a floor of one leave room for global ranking.
    assert graph._PREFETCH_K > len(index.PREFETCH_COLLECTIONS) * graph._PREFETCH_FLOOR


@needs_model
def test_a_colour_question_reaches_the_catalogue_chunk(db, monkeypatch):
    """End to end on the real model: the pre-fetch's best catalogue hit for a
    colour question is that car's colour passage."""
    monkeypatch.setattr(catalogue_chunks, "collect", lambda db=None: catalogue_chunks.render(_db))
    _db = db
    hits = index.search(
        "what colours do you have for the Jetour Dashing",
        collections=index.PREFETCH_COLLECTIONS, k=5, floor=1,
    )
    catalogue_hits = [c for c, _ in hits if c.collection == "catalogue"]
    assert catalogue_hits, "the floor must reserve the catalogue's best chunk a slot"
    assert catalogue_hits[0].source == "jet-dash"
    assert catalogue_hits[0].heading == "Colours"
