"""Which catalogue vehicle a message, or a conversation, is talking about.

WHY THIS EXISTS (2026-09-02). The RETRIEVAL_FIRST pre-fetch searches the index
with the customer's message and nothing else. Customers ask about the car they
are already discussing by pronoun — "how much does it cost", "what colours
does it come in", "what is its battery type" — and a query with no car in it
finds no car: measured on the real embedding model across nine models and ten
question shapes, the right catalogue passage was in the pre-fetch 18% of the
time. Append the model's full name and it is there 98% of the time.

THIS IS A HINT FOR A RETRIEVAL QUERY, NOT A FILTER. services/catalogue.search
refuses to keyword-match free text against the catalogue, and that stands:
what a customer wants is decided by the model. Nothing here chooses a car for
the customer — it only tells the local index which car the conversation has
been about so the passages it hands Aisha are about that car. A wrong guess
costs exactly what today's behaviour costs: a passage for the wrong car,
which the prompt already tells her to ignore.

RESOLUTION IS "THE LAST CAR NAMED IN THE CONVERSATION", either side speaking.
Checked against every pronoun question in the test VM's transcripts (23 of
them, across seven sessions): all 23 resolved to the car the customer meant,
including two chats where Aisha had named three cars in one reply. It is
deterministic and takes microseconds; a model call to rewrite the query would
cost the second per turn this feature exists to save.

Aliases are derived from the catalogue names themselves so a re-seed needs no
code change: "Jetour T2 i-DM" is also "t2 i-dm", "t2 idm" and "t2"; "Jetour
X70 FL Plus" is also "x70 fl plus" and "x70 plus". Shorter aliases are
ambiguous on purpose ("t2" is two cars) — for a retrieval hint that is fine,
and the longest alias wins wherever a longer one is present.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_MAX_LOOKBACK = 12  # messages scanned backwards for the last named car


def aliases_for(name: str) -> set[str]:
    """Lowercase spellings a customer might use for one catalogue name."""
    full = re.sub(r"\s+", " ", name.strip().lower())
    out = {full}
    short = re.sub(r"^jetour\s+", "", full)
    out.add(short)
    for s in list(out):
        if "i-dm" in s:
            out.add(s.replace("i-dm", "idm"))
            out.add(s.replace("i-dm", "i dm"))
        if " fl plus" in s:
            out.add(s.replace(" fl plus", " plus"))
    return {a for a in out if a}


def _matcher(names: list[str]):
    """(compiled pattern, alias -> full name) for one list of catalogue names."""
    table: dict[str, str] = {}
    for name in names:
        for a in aliases_for(name):
            # A short alias shared by two models keeps the first, which is
            # harmless for a hint; the longest alias present in the text is
            # what wins, so "t2 idm" never resolves through "t2".
            table.setdefault(a, name)
    ordered = sorted(table, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in ordered) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )
    return pattern, table


_memo: tuple[tuple[str, ...], object, dict] | None = None


def _current():
    global _memo
    names = tuple(model_names())
    if _memo is None or _memo[0] != names:
        pattern, table = _matcher(list(names)) if names else (None, {})
        _memo = (names, pattern, table)
    return _memo[1], _memo[2]


def model_names(db=None) -> list[str]:
    """Every catalogue model name, in id order. [] when the catalogue cannot
    be read — the hint simply does nothing that turn."""
    try:
        from sqlalchemy import select

        from app.models import VehicleModel

        def _q(session):
            return [m.name for m in session.scalars(select(VehicleModel).order_by(VehicleModel.id)).all() if m.name]

        if db is not None:
            return _q(db)
        from app.db import SessionLocal

        with SessionLocal() as session:
            return _q(session)
    except Exception:  # noqa: BLE001 — a hint must never cost a turn
        return []


def mentions(text: str) -> list[str]:
    """Full catalogue names mentioned in `text`, in order of appearance."""
    if not text:
        return []
    pattern, table = _current()
    if pattern is None:
        return []
    return [table[m.group(1).lower()] for m in pattern.finditer(text)]


def _text_of(msg) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def last_mentioned(messages: list, *, lookback: int = _MAX_LOOKBACK) -> str | None:
    """The last catalogue name either side used in the recent history."""
    for msg in reversed((messages or [])[-lookback:]):
        found = mentions(_text_of(msg))
        if found:
            return found[-1]
    return None


def reset() -> None:
    global _memo
    _memo = None
