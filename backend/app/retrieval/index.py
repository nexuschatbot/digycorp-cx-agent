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
COLLECTIONS: tuple[str, ...] = ("brochures", "knowledge")

# Collections a customer question is answered from. Kept separate from
# COLLECTIONS so adding the routing cards cannot accidentally make them
# retrievable as facts.
KNOWLEDGE_COLLECTIONS: tuple[str, ...] = ("brochures", "knowledge")

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
    chunks = chunker.collect(collection)
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
    digest = chunker.content_hash(collection)
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


def search(
    query: str,
    *,
    collections: tuple[str, ...] = KNOWLEDGE_COLLECTIONS,
    k: int = 5,
    include_gaps: bool = True,
) -> list[tuple]:
    """The k best chunks for a question, best first, as (Chunk, score).

    RANKED ACROSS COLLECTIONS, not per collection. A warranty question should
    reach warranty.md whether the best chunk lives there or in a brochure, and
    quotas per collection would only encode a guess about where facts live.

    NO SCORE THRESHOLD, deliberately — see embed.top_k. On this content every
    similarity lands between 0.75 and 0.87, so any constant either admits
    everything or nothing. The caller gets the ranking and the scores and can
    compare them to EACH OTHER.
    """
    if not query or not query.strip():
        return []
    qv = embed.embed_query(query.strip())
    if qv is None:
        return []

    hits: list[tuple] = []
    for collection in collections:
        loaded = load(collection)
        if loaded is None:
            continue
        chunks, vectors = loaded
        for i, score in embed.top_k(qv, vectors, k=k):
            c = chunks[i]
            if not include_gaps and c.kind == "gap":
                continue
            hits.append((c, score))

    hits.sort(key=lambda h: -h[1])
    return hits[:k]


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
