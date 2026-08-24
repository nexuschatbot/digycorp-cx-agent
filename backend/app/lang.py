"""Per-turn language detection (FR-CX-02/03, LOC-01, rule 3) — ONE implementation.

Extracted from api/chat.py when voice notes arrived (2026-07-21). The service
layer now needs the same answer chat does, and a service cannot import from the
API layer without inverting AD-I. Copying the regex would have been worse: two
detectors that agree today and drift the first time either is touched, deciding
the single most visible property of a reply — which language it is in.

`api.chat._detect_lang` still exists and still has its old signature; it now
delegates here, so nothing that called it had to change.

SCRIPT, NOT LANGUAGE ID. This asks "are there Arabic characters in this string",
not "what language is this". That is deliberate and it is enough, because both
callers feed it text that is already in its native script — the customer typed
it, or the transcriber returned it that way. It is also why the V0 voice spike
checked the transcriber's OUTPUT SCRIPT before any code was written: Gulf Arabic
returned as Latin transliteration would read as English here, and Aisha would
answer an Arabic speaker in English. It came back in Arabic script, so this
holds. If a future transcriber transliterates, this function is not the thing to
fix — the transcription is.

Mixed-direction text resolves to `ar`, which is correct for the code-switching
rule 3 describes: "أبغى أجرب الـ Jetour T2" is an Arabic sentence with an English
model name in it, and it deserves an Arabic reply.
"""

from __future__ import annotations

import re

from app.models import Language

# U+0600–U+06FF. Matches on the FIRST Arabic character anywhere in the string.
_ARABIC = re.compile(r"[؀-ۿ]")

# Basic Latin letters only — enough to say "there are English words here". Digits
# and punctuation are script-neutral: "+971505550103" belongs to no language.
_LATIN = re.compile(r"[A-Za-z]")


def detect(text: str | None) -> str:
    """"ar" if the text contains any Arabic character, else "en". Never raises."""
    return "ar" if _ARABIC.search(text or "") else "en"


def classify(text: str | None) -> str:
    """"en", "ar", or "mixed" — the script composition of one message.

    `detect` answers the storage question: which Language enum tags this turn,
    with mixed text resolving to ar. This answers the prompt question: how
    firmly the reply language may be directed. A message written wholly in one
    script is unambiguous evidence of this turn's language and earns a hard
    directive; a message carrying both scripts is the code-switching rule 3
    tells the model to follow, and stays a judgement call. Added 2026-07-27
    after the soft-for-everything anchor lost to conversation momentum on the
    VM: a pure-English "no i want cheaper" after two Arabic turns was answered
    in Arabic.
    """
    t = text or ""
    has_arabic = bool(_ARABIC.search(t))
    has_latin = bool(_LATIN.search(t))
    if has_arabic and has_latin:
        return "mixed"
    return "ar" if has_arabic else "en"


def detect_enum(text: str | None) -> Language:
    """As `detect`, but as the Language enum the ORM columns want."""
    return Language.ar if detect(text) == "ar" else Language.en
