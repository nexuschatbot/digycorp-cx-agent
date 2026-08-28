"""Local sentence embeddings, on CPU, offline (T1.1/T1.2).

THE MODEL IS VENDORED, exactly like app/assets/silero_vad.onnx and for the same
reasons: no download at boot, no network in the test suite, no surprise on a
deploy. `multilingual-e5-small`, int8-quantised, 384 dimensions, ~113MB —
NOT sentence-transformers/torch, which would put 2-3GB into an image that also
runs the whole test suite in CI.

FAIL SOFT, RETURNING None. Every function here returns None rather than raising
when the model or the tokenizer cannot be loaded. The plan's rule is that a
failed embedding SKIPS retrieval and falls back — never that it breaks a turn.
A customer whose question reached no brochure chunk still gets an answer from
the catalogue; a customer who gets an exception gets nothing.

TWO THINGS THAT LOOK LIKE DETAILS AND ARE NOT:

1. e5 REQUIRES ITS PREFIXES. This model family was trained with "query: " on
   the question and "passage: " on the document, and it is not decoration —
   embedding both sides the same way measurably degrades ranking. `embed_query`
   and `embed_passages` exist so a caller cannot get it wrong by passing a bare
   string to a generic function.

2. THE SCORES ARE COMPRESSED AND ABSOLUTE THRESHOLDS ARE MEANINGLESS. Measured
   on this repo's own content, every similarity lands between 0.75 and 0.87, and
   the gap between the right chunk and the wrong one is often ~0.015. A
   threshold like "route if >= 0.80" would fire on everything. RANK, never
   threshold — see `top_k`, and see the note on the plan's 0.80 routing
   placeholder, which needs the same treatment in Phase 3.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger("app.retrieval.embed")

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
MODEL_PATH = _ASSETS / "multilingual-e5-small-int8.onnx"
TOKENIZER_PATH = _ASSETS / "multilingual-e5-small-tokenizer.json"

# The model's own limit. Chunks are built well under this; truncation here is a
# backstop against a pathological input, not the chunker's job.
MAX_TOKENS = 512
DIMENSIONS = 384

# One session per process, built once. The model is stateless, so a session per
# call would pay ~200ms of load for ~10ms of work.
_lock = threading.Lock()
_state: dict = {}


def _load() -> tuple | None:
    """(tokenizer, session) or None. Cached, including the failure."""
    if "loaded" in _state:
        return _state["loaded"]
    with _lock:
        if "loaded" in _state:
            return _state["loaded"]
        try:
            import numpy  # noqa: F401 — imported here so a missing numpy is a soft failure too
            import onnxruntime as ort
            from tokenizers import Tokenizer

            if not MODEL_PATH.is_file() or not TOKENIZER_PATH.is_file():
                # The normal case in a checkout without the vendored model, and
                # a WARNING rather than an exception: retrieval degrades, the
                # app runs.
                log.warning("embedding model not vendored at %s — retrieval disabled", MODEL_PATH)
                _state["loaded"] = None
                return None

            if _is_lfs_pointer(MODEL_PATH) or _is_lfs_pointer(TOKENIZER_PATH):
                # THE ASSETS ARE IN GIT LFS — they exceed GitHub's 100MB file
                # limit, so plain git cannot carry them. A clone without LFS
                # leaves a ~130-byte TEXT POINTER where the model should be.
                # Detected explicitly because the alternative is an opaque
                # tokenizer parse error an hour from now, and the fix is one
                # command.
                log.warning(
                    "embedding assets are unfetched Git LFS pointers — run "
                    "`git lfs install && git lfs pull`. Retrieval disabled."
                )
                _state["loaded"] = None
                return None

            tok = Tokenizer.from_file(str(TOKENIZER_PATH))
            tok.enable_truncation(max_length=MAX_TOKENS)
            sess = ort.InferenceSession(
                str(MODEL_PATH), providers=["CPUExecutionProvider"]
            )
            _state["loaded"] = (tok, sess)
            log.info("embedding model loaded (%d dims)", DIMENSIONS)
        except Exception:  # noqa: BLE001 — see FAIL SOFT above
            log.exception("could not load the embedding model — retrieval disabled")
            _state["loaded"] = None
        return _state["loaded"]


def _is_lfs_pointer(path: Path) -> bool:
    """True when the file is an unfetched Git LFS pointer rather than content.

    A pointer is a ~130-byte text file beginning "version https://git-lfs...".
    Cheap to check and it turns a baffling failure into an instruction.
    """
    try:
        if path.stat().st_size > 1024:
            return False
        with path.open("rb") as fh:
            return fh.read(40).startswith(b"version https://git-lfs")
    except OSError:
        return False


def available() -> bool:
    """Whether embedding will work. Callers use this to skip retrieval cleanly
    rather than calling and handling None everywhere."""
    return _load() is not None


def _encode(texts: list[str], batch_size: int = 16):
    """Mean-pooled, L2-normalised vectors as a (N, 384) array, or None.

    MEAN POOLING OVER THE ATTENTION MASK, not over the whole tensor: padding
    tokens carry real activations and averaging them in would let a short
    sentence's vector drift toward whatever the padding happens to encode.

    L2-NORMALISED so a dot product IS the cosine similarity. Every caller then
    compares with `@` and nobody has to remember to divide.
    """
    loaded = _load()
    if loaded is None or not texts:
        return None
    tok, sess = loaded

    try:
        import numpy as np

        out = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            tok.enable_padding()
            enc = tok.encode_batch(batch)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            # This export takes token_type_ids; a different one may not. Fed
            # only when the graph asks for it rather than assumed.
            if any(i_.name == "token_type_ids" for i_ in sess.get_inputs()):
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = sess.run(None, feed)[0]
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            out.append(pooled / np.clip(norms, 1e-9, None))
        return np.vstack(out)
    except Exception:  # noqa: BLE001
        log.exception("embedding failed for %d texts", len(texts))
        return None


def embed_query(text: str):
    """A CUSTOMER'S QUESTION. Carries the "query: " prefix e5 was trained with."""
    v = _encode([f"query: {text}"])
    return None if v is None else v[0]


def embed_passages(texts: list[str]):
    """DOCUMENT CHUNKS. Carries the "passage: " prefix e5 was trained with.

    Separate from embed_query on purpose: one generic function taking a string
    would make the prefix a parameter someone forgets, and forgetting it is a
    silent quality loss with no error anywhere.
    """
    return _encode([f"passage: {t}" for t in texts])


def top_k(query_vector, matrix, k: int = 5) -> list[tuple[int, float]]:
    """The k closest rows as (index, score), best first.

    RANKED, NEVER THRESHOLDED, and the module docstring explains why: on this
    content every score sits between 0.75 and 0.87. A caller wanting "only good
    matches" should compare scores to EACH OTHER — the margin between first and
    second is the signal — not to a constant.
    """
    if query_vector is None or matrix is None or len(matrix) == 0:
        return []
    import numpy as np

    scores = np.asarray(matrix) @ np.asarray(query_vector)
    order = np.argsort(-scores)[: max(0, k)]
    return [(int(i), float(scores[i])) for i in order]
