"""The additive catalogue vector pass (T1.6).

WHAT IT IS FOR. `body_type` is free text and _apply_filters matches it as a
literal SUBSTRING, so a customer phrase that is not a word in the catalogue
matches nothing at all and is silently discarded — "family car", "off roader",
"something for camping" all return zero rows. This gives those phrases an answer.

WHY VECTORS AND NOT A SYNONYM LIST. The plan rejected synonym maps in terms:
"maintained forever and always one phrasing behind". A map fixes the phrasings
somebody thought of; this handles the ones they did not.

TWO RULES MAKE IT SAFE, AND BOTH ARE ENFORCED HERE RATHER THAN HOPED FOR:

1. ADDITIVE ONLY. It supplies candidates where the filter supplied NONE. It
   never removes a row and never reorders one — in the only case it fires there
   is nothing to remove or reorder. _pick_spread and _ranked_for run afterwards
   unchanged, and MAX_RECOMMENDATIONS is still three.

2. NUMERIC CONSTRAINTS ARE NEVER LOOSENED. A candidate must still satisfy the
   budget, seats and fuel the customer actually stated. Without this, "under
   100k" could pick up a 169,000 G700 because it sounds roomy — a grounding
   failure dressed as a feature.

WHAT IT DOES AND DOES NOT DO, DECIDED BY MEASUREMENT (T1.6):

    body_type="suv"     substring matches 8 of 9 rows.
                        The X50 ("Compact crossover") ranks 9th of 9 at 0.8011,
                        BELOW every substring match (0.8123-0.8382).
    body_type="family car" / "off roader" / "something for camping"
                        substring matches 0 of 9.
                        Vector top-3 are the right cars, every time.

SO IT FIRES ONLY WHEN THE SUBSTRING FILTER FOUND NOTHING. When the literal word
appears in most rows, those rows win on lexical overlap and the embedding cannot
overcome it — the entire score range on this corpus is 0.04 wide, so no
threshold separates "compact crossover is an SUV" from noise. Tuning one to make
that case pass would admit everything.

That means the X50/SUV defect is NOT fixed here. It is a real, customer-visible
miss ("show me an SUV under 80,000" hides the cheapest car in the range) and it
needs a different remedy — recorded in the plan rather than papered over with a
threshold that happens to work on one example.

STILL ADDITIVE, and more strictly than before: it only ever supplies candidates
where the filter supplied NONE. It cannot remove a row and cannot reorder one,
because in the case it fires there is nothing to remove or reorder.
"""

from __future__ import annotations

import hashlib
import logging

from app.retrieval import embed

log = logging.getLogger("app.retrieval.catalogue_pass")

_memo: dict = {}


def _row_text(v) -> str:
    """What a catalogue row looks like to the embedder.

    Name, body type and the marketing bullets — the same text a human would skim
    to decide "is this an SUV?". Price and seats are deliberately absent: they
    are matched numerically and exactly, and putting them here would let a
    similarity score argue about a budget.
    """
    features = " ".join(str(f) for f in (v.headline_features or [])[:4])
    return f"{v.name} — {v.body_type or ''}. {features}".strip()


def _vectors(rows: list):
    """Embedded catalogue rows, memoised on their content.

    Nine rows, so a rebuild is well under a second — but it happens on every
    search, and a hash keyed on the row text means a re-seed invalidates it
    without anybody remembering to.
    """
    texts = [_row_text(v) for v in rows]
    digest = hashlib.sha256(" ".join(texts).encode()).hexdigest()[:16]
    if _memo.get("digest") == digest:
        return _memo["vectors"]
    vectors = embed.embed_passages(texts)
    if vectors is None:
        return None
    _memo.update(digest=digest, vectors=vectors)
    return vectors


def additional_candidates(
    rows: list,
    matched: list,
    *,
    body_type: str | None,
    feature_query: str | None,
    still_satisfies,
    k: int = 3,
) -> list:
    """Candidates for a free-text phrase the substring filter could not match.

    RETURNS [] UNLESS `matched` IS EMPTY. See the module docstring: this helps
    only where the literal filter found nothing, and measurably does not help
    where it found most things.

    `still_satisfies(v)` is supplied by the caller and applies every constraint
    EXCEPT the free-text one being loosened, so a budget is never widened by a
    similarity score. Passing it in means there is exactly one implementation of
    "fits the customer's budget", and it is catalogue's.
    """
    query = " ".join(p for p in (body_type, feature_query) if p).strip()
    if not query or not rows:
        return []
    # The rule. A filter that already found something is left alone.
    if matched:
        return []

    vectors = _vectors(rows)
    if vectors is None:
        return []
    qv = embed.embed_query(query)
    if qv is None:
        return []

    try:
        import numpy as np

        scores = np.asarray(vectors) @ np.asarray(qv)
        order = np.argsort(-scores)
    except Exception:  # noqa: BLE001 — retrieval must never break a search
        log.exception("catalogue vector pass failed")
        return []

    extra = []
    for i in order:
        v = rows[int(i)]
        # Numeric constraints are NEVER loosened — "under 100k" must not pick up
        # a 169,000 G700 because it sounds roomy.
        if not still_satisfies(v):
            continue
        extra.append(v)
        if len(extra) >= k:
            break

    if extra:
        log.info(
            "catalogue vector pass rescued %r with %s",
            query, [v.model_code for v in extra],
        )
    return extra


def reset() -> None:
    """Drop the memo. Tests, and a re-seed in a live process."""
    _memo.clear()
