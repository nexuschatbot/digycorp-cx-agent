"""Turning the content MD into retrievable chunks (T1.2).

THE CHUNKER WAS CHOSEN BY MEASUREMENT, NOT BY TASTE. Four strategies were run
against a 13-question eval set covering brochures, knowledge files and Arabic:

    section chunks, no prefix          153 chunks   top-1  7/13   top-3 10/13
    paragraph chunks, no prefix        226 chunks   top-1  8/13   top-3 10/13
    paragraph chunks + heading prefix  226 chunks   top-1 11/13   top-3 13/13  <-
    section chunks + heading prefix    153 chunks   top-1 10/13   top-3 12/13

THE HEADING PREFIX IS WORTH +3 ON TOP-1 and it is the least obvious part of this
file. A bare paragraph from a specification table is a column of numbers with no
idea which car or which topic it belongs to; "x70-fl-plus — At a glance" in front
of it is what lets "how many seats does the X70 FL Plus have" find it. The
prefix is part of the embedded TEXT, not metadata.

TWO KINDS OF THING ARE DELIBERATELY NOT INDEXED AS ANSWERS:

1. HTML COMMENTS ARE STRIPPED ENTIRELY. They are notes to us — provenance,
   extraction warnings, "the designer reused a Dashing template". Retrieving one
   into a customer answer would be embarrassing, and they are the densest
   internal text in the corpus, so leaving them in would also pull ranking
   toward our own commentary.

2. "NOT IN THIS FILE" SECTIONS ARE KEPT BUT MARKED `kind="gap"`. They are not
   answers, but they are the best possible response to a question we cannot
   answer — "we don't publish that" beats an invented figure (FR-CX-07). The
   caller decides how to present them; the index just refuses to pretend they
   are facts.

FRONTMATTER RIDES ALONG AS METADATA, not as text. `review_status` in particular:
five brochures are still `draft-partial`, and a chunk from one should be
answerable-with-a-caveat rather than silently equal to a reviewed one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# backend/app/retrieval/chunk.py -> backend/content
CONTENT_DIR = Path(__file__).resolve().parent.parent.parent / "content"

# Long enough that a heading plus a table row is not its own chunk; short enough
# that a chunk is about ONE thing. Measured band, not a round number pulled from
# the air — see the ablation above, which ran at this cap.
MAX_CHARS = 900
MIN_CHARS = 60

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_HEADING_SPLIT = re.compile(r"\n#+ ")


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage, and everything needed to cite it."""

    text: str            # what gets embedded, heading prefix included
    body: str            # the passage alone, for showing to a customer
    source: str          # file stem, e.g. "warranty" or "x70-fl-plus"
    collection: str      # "knowledge" or "brochures"
    heading: str
    kind: str = "fact"   # "fact" | "gap"
    review_status: str = "complete"
    meta: dict = field(default_factory=dict)

    @property
    def citation(self) -> str:
        return f"{self.source}#{self.heading}" if self.heading else self.source


def _frontmatter(text: str) -> tuple[dict, str]:
    """Parsed frontmatter and the body without it. Never raises on bad YAML —
    a malformed header must cost the METADATA, never the chunk."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    data: dict = {}
    try:
        import yaml

        loaded = yaml.safe_load(m.group(1))
        if isinstance(loaded, dict):
            data = loaded
    except Exception:  # noqa: BLE001
        pass
    return data, text[m.end():]


def _split_paragraph(block: str) -> list[str]:
    """Paragraphs, then a hard wrap for anything still over the cap.

    A long specification table is ONE paragraph by the blank-line rule and can
    run past MAX_CHARS, so it is cut on line boundaries rather than mid-row —
    half a table row embeds to nothing useful and reads as corruption if shown.
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", block):
        para = para.strip()
        if not para:
            continue
        if len(para) <= MAX_CHARS:
            out.append(para)
            continue
        current: list[str] = []
        size = 0
        for line in para.splitlines():
            if size + len(line) > MAX_CHARS and current:
                out.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            out.append("\n".join(current))
    return out


def chunk_file(path: Path, collection: str) -> list[Chunk]:
    """Every chunk in one markdown file."""
    raw = path.read_text(encoding="utf-8")
    meta, body = _frontmatter(raw)
    # Strip AFTER the frontmatter split: a comment can sit anywhere below it.
    body = _COMMENT.sub("", body)

    review = str(meta.get("review_status") or "complete")
    chunks: list[Chunk] = []

    for section in _HEADING_SPLIT.split(body)[1:]:
        heading, _, rest = section.partition("\n")
        heading = heading.strip().lstrip("#").strip()
        # A gap section is not an answer — see the module docstring.
        kind = "gap" if "not in this" in heading.lower() else "fact"

        for passage in _split_paragraph(rest):
            if len(passage) < MIN_CHARS:
                continue
            chunks.append(
                Chunk(
                    # THE PREFIX IS EMBEDDED TEXT. Worth +3 on top-1 accuracy.
                    text=f"{path.stem} — {heading}\n{passage}",
                    body=passage,
                    source=path.stem,
                    collection=collection,
                    heading=heading,
                    kind=kind,
                    review_status=review,
                    meta={"title": meta.get("title_en") or meta.get("name_en")},
                )
            )
    return chunks


def collect(collection: str) -> list[Chunk]:
    """Every chunk in one content directory, in a stable order.

    SORTED BY FILENAME so the index is deterministic: two builds of the same
    content must produce the same chunk order, or the content hash is useless
    and the cache thrashes.
    """
    directory = CONTENT_DIR / collection
    if not directory.is_dir():
        return []
    out: list[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        out.extend(chunk_file(path, collection))
    return out


def content_hash(collection: str) -> str:
    """A digest of the SOURCE FILES, not of the chunks.

    THE CACHE GATE. The plan's rule is that the index rebuilds when the content
    changes and never otherwise: "rebuilt on seed change" fails silently the
    first time someone edits a file and forgets, and stale vectors misroute with
    no error anywhere.

    Hashes the file contents and their names — a renamed file changes citations
    even when its text does not.
    """
    directory = CONTENT_DIR / collection
    h = hashlib.sha256()
    # The chunker itself is part of the identity: change MAX_CHARS or the
    # prefix and every vector is different, so a stale cache would be wrong in a
    # way no content hash would catch.
    h.update(f"v1|{MAX_CHARS}|{MIN_CHARS}".encode())
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            h.update(path.name.encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:16]
