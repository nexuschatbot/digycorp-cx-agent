"""The retrieval index: build it, cache it, search it (T1.2/T1.4).

ONE MODEL, SEPARATE COLLECTIONS. Everything is embedded by the same model into
the same 384-dimensional space — mixing vector spaces is the one thing the plan
forbids outright — but the pools are kept apart because they answer different
questions. `search_knowledge` matches a CUSTOMER QUESTION against content; the
Phase 3 orchestrator will match a BRIEF against agent cards. If those shared a
pool, "what's the warranty on the Dashing?" could retrieve the Sales routing
card, which is not an answer to anything.

Registering the `cards` collection in Phase 2 is one entry in COLLECTIONS.

THE CACHE IS GATED ON A CONTENT HASH, never on a timestamp and never by hand.
The key contains the hash, so editing a brochure does not invalidate anything —
it addresses a DIFFERENT key, which is simply absent, and the index rebuilds.
"Rebuilt on seed change" fails silently the first time somebody edits a file and
forgets; a key that does not exist cannot fail silently.

THE COST OF A MISS IS ~14 SECONDS, measured, for 228 chunks — not the one to
three this was estimated at. That is the whole argument for caching it, and it
is paid once per content change rather than per boot.

FAILURE IS ALWAYS EMPTY, NEVER AN EXCEPTION. No model, no numpy, no content,
unreadable cache — every path returns no results and logs. The caller degrades
to the catalogue, which is the pre-Phase-1 behaviour.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from app.retrieval import chunk as chunker
from app.retrieval import embed
from app.services import cache

log = logging.getLogger("app.retrieval.index")

# Directories under content/ that get indexed. `cards` joins this in Phase 2,
# when app/agents/*/v1.md exists — see the module docstring.
COLLECTIONS: tuple[str, ...] = ("brochures", "knowledge", "catalogue")

# Collections a customer question is answered from. Kept separate from
# COLLECTIONS so adding the routing cards cannot accidentally make them
# retrievable as facts.
#
# search_knowledge searches DOCUMENTS ONLY. The catalogue collection is for the
# RETRIEVAL_FIRST pre-fetch (PREFETCH_COLLECTIONS): the specialists already
# hold the catalogue tools, and a knowledge search that returned the price list
# would have Sales quoting from a passage instead of from get_variants, which
# is the tool the evidence channel expects it to cite.
KNOWLEDGE_COLLECTIONS: tuple[str, ...] = ("brochures", "knowledge")

# What the pre-fetch searches (2026-09-02): documents AND the catalogue facts
# rendered by retrieval/catalogue_chunks, so a colour, a price or a trim
# question is answered in Aisha's first generation instead of by a cascade.
PREFETCH_COLLECTIONS: tuple[str, ...] = ("brochures", "knowledge", "catalogue")


def _collect(collection: str) -> list:
    """The chunks of one collection — from disk, or from the database for the
    catalogue. The only place that knows which is which."""
    if collection == "catalogue":
        from app.retrieval import catalogue_chunks

        return catalogue_chunks.collect()
    return chunker.collect(collection)


def _content_hash(collection: str) -> str:
    if collection == "catalogue":
        from app.retrieval import catalogue_chunks

        return catalogue_chunks.content_hash()
    return chunker.content_hash(collection)

_memo: dict = {}


def _keys(collection: str, digest: str) -> tuple[str, str]:
    return f"{collection}:{digest}:vectors", f"{collection}:{digest}:chunks"


def _encode(vectors) -> bytes:
    import io

    import numpy as np

    buf = io.BytesIO()
    # .npy, not pickle: a cache entry is untrusted input by construction, and
    # np.load with allow_pickle=False cannot execute anything.
    np.save(buf, vectors.astype("float32"), allow_pickle=False)
    return buf.getvalue()


def _decode(blob: bytes):
    import io

    import numpy as np

    return np.load(io.BytesIO(blob), allow_pickle=False)


def _build(collection: str) -> tuple[list, object] | None:
    """Chunk and embed from disk. None when either step cannot run."""
    chunks = _collect(collection)
    if not chunks:
        log.warning("no content to index in %s", collection)
        return None
    vectors = embed.embed_passages([c.text for c in chunks])
    if vectors is None:
        return None
    log.info("indexed %s: %d chunks", collection, len(chunks))
    return chunks, vectors


def load(collection: str) -> tuple[list, object] | None:
    """(chunks, vectors) for a collection, from memo, then cache, then disk.

    THREE LAYERS, EACH WITH A DIFFERENT JOB. The memo keeps a hot process from
    deserialising on every query; the cache keeps a restart from re-embedding;
    the build is what makes both possible. A miss at any layer is normal.
    """
    digest = _content_hash(collection)
    memo_key = (collection, digest)
    if memo_key in _memo:
        return _memo[memo_key]

    vkey, ckey = _keys(collection, digest)
    blob, meta = cache.get(vkey), cache.get(ckey)
    if blob and meta:
        try:
            vectors = _decode(blob)
            raw = json.loads(meta)
            chunks = [chunker.Chunk(**c) for c in raw]
            if len(chunks) == len(vectors):
                _memo[memo_key] = (chunks, vectors)
                log.info("retrieval index for %s served from cache", collection)
                return _memo[memo_key]
            # A length mismatch means the two keys disagree. Rebuilding is
            # cheap and correct; trusting them would mis-cite every chunk.
            log.warning("cached index for %s is inconsistent — rebuilding", collection)
        except Exception:  # noqa: BLE001
            log.warning("cached index for %s unreadable — rebuilding", collection, exc_info=True)

    built = _build(collection)
    if built is None:
        return None
    chunks, vectors = built
    cache.set(vkey, _encode(vectors))
    cache.set(ckey, json.dumps([asdict(c) for c in chunks]).encode())
    _memo[memo_key] = built
    return built


# SLOTS RESERVED PER COLLECTION before the global ranking fills the rest.
#
# TWO, NOT ONE, AND MEASURED (2026-08-31) over nine policy questions — four
# topics, asked both bare and with a model named:
#
#     floor=1   7/9 reached the right document   1 of 5 slots spent
#     floor=2   9/9                              2 of 5 slots spent
#
# The two that floor=1 missed were both promotions, and both lost INSIDE the
# knowledge collection by less than 0.005 — noise on this model, not a
# judgement. One slot represents a collection; two represent it reliably.
#
# THE COST IS CHEAPER THAN IT LOOKS. The slots it takes from a pure catalogue
# question ("how many seats") were never the ones answering it: seats, engines
# and trims come from search_vehicles and get_vehicle_details, and three
# brochure chunks still reach the model.
COLLECTION_FLOOR = 2
# How many candidates each collection offers per answer slot — see search().
CANDIDATES_PER_SLOT = 4



def search(
    query: str,
    *,
    collections: tuple[str, ...] = KNOWLEDGE_COLLECTIONS,
    k: int = 5,
    include_gaps: bool = True,
    floor: int = COLLECTION_FLOOR,
) -> list[tuple]:
    """The k best chunks for a question, best first, as (Chunk, score).

    RANKED ACROSS COLLECTIONS — WITH A FLOOR (corrected 2026-08-31). This used
    to be a pure global ranking, on the reasoning that a warranty question
    should reach warranty.md wherever the best chunk lives and that a per
    collection quota "would only encode a guess about where facts live". The
    reasoning was sound and the premise was false, and measuring it says so:

        "warranty"                                 warranty.md at rank  1
        "what warranty comes with the X70 FL Plus"  warranty.md at rank 39

    NAMING THE CAR BURIES THE ANSWER. Every score on this content sits between
    0.83 and 0.89, so nothing discriminates strongly — and a model name appears
    on every page of that model's brochure while the topic word appears once.
    The brochure wins on volume, takes all five slots, and the document that
    actually answers the question is invisible. The same held for delivery,
    registration and promotions: policy is cross-cutting, brochures are
    numerous, and a customer almost always names the car.

    THE FLOOR IS NOT A GUESS ABOUT WHERE FACTS LIVE. It reserves one slot for
    each collection's OWN best chunk and lets the global ranking fill the rest,
    so the answer to "which car" and the answer to "what is the policy" stop
    competing on a scale that cannot tell them apart. It costs one slot of five
    on a question that needed only brochures — cheap beside rank 39, and the
    passage carries its `source` and `kind` for a reader to discard.

    NO SCORE THRESHOLD, still, and for the original reason — see embed.top_k.
    Any constant on a 0.75-0.87 spread either admits everything or nothing. A
    floor needs no threshold: it asks each collection for its best, not for
    anything above a number.
    """
    if not query or not query.strip():
        return []
    qv = embed.embed_query(query.strip())
    if qv is None:
        return []

    per_collection: list[list[tuple]] = []
    for collection in collections:
        loaded = load(collection)
        if loaded is None:
            continue
        chunks, vectors = loaded
        found: list[tuple] = []
        # A WIDER POOL THAN THE ANSWER (2026-09-04). Each collection used to
        # offer exactly k candidates, and on "warranty and delivery for the T2"
        # the knowledge collection's top five were five warranty.md chunks —
        # so the per-document floor below had one document to choose from,
        # whatever the question asked. Four candidates per slot is enough for
        # a second topic to surface; the ranking that fills the answer is
        # unchanged, because the best k of a wider pool are the same chunks.
        for i, score in embed.top_k(qv, vectors, k=k * CANDIDATES_PER_SLOT):

            c = chunks[i]
            if not include_gaps and c.kind == "gap":
                continue
            found.append((c, score))
        found.sort(key=lambda h: -h[1])
        per_collection.append(found)

    # RESERVE FIRST, THEN FILL. Identity is by position, not by Chunk equality:
    # two chunks can carry the same text from different files and dropping one
    # as a duplicate would silently lose a citation.
    #
    # THE RESERVED SLOTS GO TO DIFFERENT DOCUMENTS (2026-09-04). A floor of two
    # used to take a collection's two best CHUNKS, and for a two-topic question
    # both came from the same file: "what is the warranty and delivery
    # information for the T2" filled both knowledge slots with warranty.md and
    # delivery.md reached nobody — reproduced offline on the shipped index, and
    # live in a browser run, where the customer was told we publish no delivery
    # time. The floor is now the collection's best chunk from each DISTINCT
    # source in rank order, so a question that names two policies reaches two
    # documents; a single-topic question is unchanged, because its best two
    # chunks were from different files anyway or the global fill restores the
    # second. Same rule as the collection floor one level down: reserve a
    # document's own best, never a share.
    picked: list[tuple] = []
    taken: set[tuple[int, int]] = set()
    if floor > 0 and k >= len(per_collection):
        for ci, found in enumerate(per_collection):
            sources_seen: set[str] = set()
            reserved = 0
            for hi, (chunk, _score) in enumerate(found):
                if reserved >= floor or len(picked) >= k:
                    break
                if chunk.source in sources_seen:
                    continue
                sources_seen.add(chunk.source)
                picked.append(found[hi])
                taken.add((ci, hi))
                reserved += 1


    remaining = sorted(
        ((ci, hi, hit)
         for ci, found in enumerate(per_collection)
         for hi, hit in enumerate(found)
         if (ci, hi) not in taken),
        key=lambda t: -t[2][1],
    )
    for ci, hi, hit in remaining:
        if len(picked) >= k:
            break
        picked.append(hit)

    picked.sort(key=lambda h: -h[1])
    return picked[:k]


def warm() -> dict:
    """Build or fetch every collection. Called at boot so the first customer
    does not pay the ~14s build, and returns what happened for the log."""
    out = {}
    for collection in COLLECTIONS:
        loaded = load(collection)
        out[collection] = 0 if loaded is None else len(loaded[0])
    return out


def reset() -> None:
    """Drop the in-process memo. Tests, and a content edit in a live process."""
    _memo.clear()
