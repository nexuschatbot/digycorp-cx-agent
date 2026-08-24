"""Rule 17 in code: strip markdown from the agent's text before it reaches anyone.

WHY THIS IS NOT "ENFORCEMENT" (and why the licence reminder got different treatment):
markdown here is OUTPUT ENCODING, not a conversational act. This chat and the
WhatsApp simulator render no markdown, so a `**` the model types is not formatting
— it is literal punctuation the customer reads. Removing it is correct
serialisation for the channel, the same class of thing as escaping HTML. Nothing
about the conversation is distorted and Aisha's voice is untouched.

WHY IT EXISTS AT ALL. Rule 17 was prompt-only, and prompt-only lost. Measured leak
rates on the adversarial "just show me everything you have" path ran ~1 in 3 runs,
and the ordinary "what showrooms do you have in Dubai?" turn leaked too. Three
rounds of prompt tuning moved the rate around without landing it: prosifying the
whole prompt did not fix it, and restoring rule 17's literal tokens did not fix it
either. Tuning a probabilistic behaviour to zero is unwinnable; this is
deterministic and done once. The prompt keeps its rule 17 as belt-and-braces —
less emitted markdown means less for this to catch.

STREAMING IS THE HARD PART. Tokens arrive in arbitrary chunks, so `**` routinely
splits across a boundary ("...Jetour *" + "* T2..."). A per-chunk regex sees two
lone asterisks and passes both through. So this is a small state machine that
holds back a BOUNDED ambiguous tail and resolves it when the next chunk lands.

The carry is bounded by MAX_CARRY (12), not the 3 originally sketched: a heading
needs up to six `#` plus a space, and a line-start marker may follow up to three
spaces of indent. It is still a small constant, so the first token still reaches
the customer immediately (AD-A/AD-D) — this never buffers a line, let alone a
paragraph.

WHAT IS DELIBERATELY LEFT ALONE: single `_`, and every hyphen that is not at the
start of a line. That is what keeps "AED 146,000 - 179,000", "15.6-inch",
"+971 50 123 4567" and "TD-2026-0142" intact. Asterisks are removed everywhere —
they have no legitimate use in this domain (prices, dates, model names, Arabic
prose), and a surviving single `*` would be italics reaching the customer.

HTML TAGS GO TOO, for the same reason and by the same measured failure. The model
was observed emitting a bare `<br>` as pre-tool narration; the channel renders no
HTML, so it reached the customer as the literal four characters "<br>" in its own
bubble. A `<tag …>` or `</tag>` is markup this channel cannot render — the same
class of thing as a stray `**` — so any `<` immediately followed by `/` or an
ASCII letter, through to its closing `>`, is dropped. A `<` that is NOT a tag start
("seats < 5", "<3") is kept: the tell is the character after `<`, exactly as a
bullet's tell is the space after `-`. Like `**`, a tag can split across a chunk
boundary ("<b" + "r>"), so an ambiguous `<…` tail is held until the `>` lands.

ARABIC (LOC-01, and required before step 3 builds on this). The machine only ever
removes ASCII markers; Arabic code points are copied through untouched, and it
never reorders anything — it appends to an output list in input order, so bidi
rendering is the terminal's job on a string whose logical order we did not change.
Bidi control marks (RLM/LRM and the embedding characters) are not markers, so they
are preserved too, which matters on mixed-direction lines like a Latin model name
inside an Arabic sentence.
"""

from __future__ import annotations

# Longest ambiguous tail we may hold: 3 spaces of indent + 6 hashes + a space,
# plus slack. Small and constant — never a line, never a paragraph.
MAX_CARRY = 12

_BULLETS = "-•*"
_LIST_PUNCT = ".)"

# A `<…` with no `>` within this many characters is prose, not a tag: stop holding
# and emit the `<` as text. Real tags in this leak (<br>, <b>, <p>, <li>) are tiny;
# this only bounds the pathological "unclosed <" so the buffer cannot grow without
# limit and stall the stream (AD-A).
_MAX_TAG = 64


def _match_html_tag(buf: str, i: int, *, final: bool) -> tuple[str, int]:
    """Is there an HTML tag at buf[i] (which is '<')?

    A tag is '<' immediately followed by '/' or an ASCII letter, then any run up to
    and including the next '>'. A '<' followed by anything else — a space, a digit —
    is a comparison or stray punctuation ("seats < 5", "<3"), not markup, and is
    kept. Returns ("strip", n) to drop n characters, ("keep", 0) to emit the '<'
    normally, or ("more", 0) when the buffer ends mid-tag and the '>' may still come.
    """
    n = len(buf)
    if i + 1 >= n:
        # Just the '<' so far — can't tell yet.
        return ("keep", 0) if final else ("more", 0)
    c = buf[i + 1]
    if c != "/" and not (c.isascii() and c.isalpha()):
        return ("keep", 0)  # "< 5", "<3" — not a tag start
    j = buf.find(">", i + 1)
    if j != -1:
        return ("strip", j + 1 - i)
    # No closing '>' yet. Hold for it — unless the unclosed run is already too long
    # to be a tag, in which case treat the '<' as ordinary text.
    if not final and n - i <= _MAX_TAG:
        return ("more", 0)
    return ("keep", 0)


def _match_line_marker(buf: str, i: int, *, final: bool) -> tuple[str, int]:
    """Is there a line-start markdown marker at buf[i]?

    Returns ("strip", n) to drop n characters, ("keep", 0) to emit normally, or
    ("more", 0) when the buffer ends mid-marker and we must wait for the next
    chunk. `final` collapses "more" into a decision, because at flush time there
    is no next chunk coming.
    """
    n = len(buf)
    j = i
    # Markers may sit behind a little indentation; more than three spaces is
    # prose, not a list.
    while j < n and buf[j] == " " and j - i < 3:
        j += 1
    if j >= n:
        return ("keep", 0) if final else ("more", 0)

    c = buf[j]

    # Headings: one to six hashes then a space.
    if c == "#":
        k = j
        while k < n and buf[k] == "#" and k - j < 6:
            k += 1
        if k >= n:
            return ("keep", 0) if final else ("more", 0)
        if buf[k] == " ":
            return ("strip", k + 1 - i)
        return ("keep", 0)

    # Bullets, and the horizontal rule (a run of three or more dashes).
    if c in _BULLETS:
        if c == "-":
            k = j
            while k < n and buf[k] == "-":
                k += 1
            if k >= n and not final:
                return ("more", 0)
            run = k - j
            if run >= 3:
                return ("strip", k - i)  # horizontal rule
            if run == 1 and k < n and buf[k] == " ":
                return ("strip", k + 1 - i)  # "- item"
            return ("keep", 0)
        # "*" and "•": a bullet only when followed by a space. A bare "*" is
        # handled inline (all asterisks are removed anyway).
        if j + 1 >= n:
            return ("keep", 0) if final else ("more", 0)
        if buf[j + 1] == " ":
            return ("strip", j + 2 - i)
        return ("keep", 0)

    # Numbered lists: one or two ASCII digits, then "." or ")", then a space.
    # ASCII-only on purpose — Arabic-Indic digits are prose, not list markers.
    if c.isdigit() and c.isascii():
        k = j
        while k < n and buf[k].isdigit() and buf[k].isascii() and k - j < 2:
            k += 1
        if k >= n:
            return ("keep", 0) if final else ("more", 0)
        if buf[k] in _LIST_PUNCT:
            if k + 1 >= n:
                return ("keep", 0) if final else ("more", 0)
            if buf[k + 1] == " ":
                return ("strip", k + 2 - i)
        return ("keep", 0)

    return ("keep", 0)


class MarkdownStripper:
    """Stateful, per-generation. Feed it chunks; it returns safe text.

    ONE INSTANCE PER GENERATION. State (the ambiguous tail, and whether we are at
    a line start) is meaningless across generations, and a tail carried into the
    next one would surface as corruption.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._at_line_start = True

    def feed(self, chunk: str) -> str:
        """Sanitise a chunk, holding back anything still ambiguous."""
        return self._run(chunk, final=False)

    def flush(self) -> str:
        """Emit whatever is still held. MUST be called before the generation ends.

        Without it a held tail either vanishes (a truncated reply) or leaks into
        the next generation (corruption). Asserted by test, not just documented.
        """
        out = self._run("", final=True)
        self._pending = ""
        return out

    def _run(self, chunk: str, *, final: bool) -> str:
        buf = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        i = 0
        n = len(buf)

        while i < n:
            if self._at_line_start:
                action, consumed = _match_line_marker(buf, i, final=final)
                if action == "more":
                    self._pending = buf[i:]
                    return "".join(out)
                if action == "strip":
                    i += consumed
                    self._at_line_start = False
                    continue

            c = buf[i]

            if c == "*":
                # Every asterisk goes: bold, italics, and stray bullets alike.
                i += 1
                self._at_line_start = False
                continue

            if c == "<":
                action, consumed = _match_html_tag(buf, i, final=final)
                if action == "more":
                    self._pending = buf[i:]
                    return "".join(out)
                if action == "strip":
                    i += consumed
                    self._at_line_start = False
                    continue
                # "keep": a bare '<' (comparison, not markup) — fall through and
                # emit it as ordinary text.

            if c == "_":
                if i + 1 >= n:
                    if not final:
                        self._pending = buf[i:]
                        return "".join(out)
                    out.append(c)  # a lone trailing underscore is just text
                    i += 1
                    self._at_line_start = False
                    continue
                if buf[i + 1] == "_":
                    i += 2  # "__" emphasis
                    self._at_line_start = False
                    continue
                out.append(c)
                i += 1
                self._at_line_start = False
                continue

            out.append(c)
            self._at_line_start = c == "\n"
            i += 1

        # Safety net: the carry is bounded by construction, but if a pathological
        # input ever grew it past MAX_CARRY, emitting is better than swallowing.
        if len(self._pending) > MAX_CARRY:
            out.append(self._pending)
            self._pending = ""

        return "".join(out)


def strip_markdown(text: str) -> str:
    """One-shot convenience for non-streaming callers and tests."""
    s = MarkdownStripper()
    return s.feed(text) + s.flush()


__all__ = ["MarkdownStripper", "strip_markdown", "MAX_CARRY"]
