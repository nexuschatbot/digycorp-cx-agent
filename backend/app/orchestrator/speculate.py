"""Speculative brief SELECTION, overlapped with Aisha's own typing (2026-09-02).

THE WINDOW THIS FILLS. Aisha's emit_briefs input streams over ~1-2s of
input_json_delta events after her opening line, and the judge — a Haiku round
trip measured at ~1.1s, paid on roughly two thirds of briefs — used to start
only after the whole generation had finished. Both are network waits; nothing
about the second needs the first to be complete. The stream handler offers
every COMPLETED brief string here the moment its closing quote arrives, a
small pool runs select() (embedding fast lane, judge fallback) while she is
still typing the rest, and plan_turn collects the answers instead of paying
the round trip cold.

SELECTION ONLY, DELIBERATELY. run.plan_turn's own comment records why this is
safe where speculating the SPECIALISTS would not be without more machinery:
select() "touches no database and no shared state" — it reads cached cards,
embeds locally, and may call the judge, whose pooled client already serves
plan_turn's own ThreadPoolExecutor. A speculative selection wasted on a
generation the language guard discards costs one Haiku read and nothing
customer-visible: no events, no writes, no cards. Speculating the specialist
steps themselves would save more and is the documented next stage — it needs
the event-buffering and cancellation guards this stage deliberately avoids.

PER SESSION, TAKEN ONCE. offer() accumulates under the session id;
_orchestrator_node take()s the whole batch at cascade start, so nothing
lingers, and a retried generation simply overwrites its predecessor's offers.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger("app.orchestrator.speculate")

# Two workers: six of every ten cascades measured carried exactly one brief,
# and plan_turn's own pool caps at four — this is a head start, not a fan-out.
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="speculate")

_lock = threading.Lock()
_store: dict[str, dict[str, Future]] = {}

# A runaway stream must not grow the registry without bound.
_MAX_BRIEFS_PER_SESSION = 8


def completed_briefs(partial_json: str) -> list[str]:
    """Every COMPLETE bare-string brief in a partial emit_briefs input.

    The input is streaming JSON like `{"briefs": ["what is the warr` — the
    text before `"briefs"` is skipped, then top-level string literals inside
    the array are read with escape handling, and an unterminated literal is
    simply not yet a brief. Object-form entries ({"brief": ..., "depends_on":
    ...}) are deliberately NOT speculated: they are the rare shape, their
    nesting is not worth parsing by hand, and missing one costs only the old
    serial path.
    """
    idx = partial_json.find('"briefs"')
    if idx < 0:
        return []
    start = partial_json.find("[", idx)
    if start < 0:
        return []
    out: list[str] = []
    i = start + 1
    n = len(partial_json)
    depth = 0
    while i < n:
        ch = partial_json[i]
        if depth == 0 and ch == '"':
            # A top-level string literal: scan to its unescaped closing quote.
            j = i + 1
            buf: list[str] = []
            while j < n:
                c = partial_json[j]
                if c == "\\" and j + 1 < n:
                    esc = partial_json[j + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                    j += 2
                    continue
                if c == '"':
                    out.append("".join(buf))
                    break
                buf.append(c)
                j += 1
            else:
                break  # literal still streaming — not a brief yet
            i = j + 1
            continue
        if depth == 0 and ch == "]":
            break
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return out


def offer(session_id: str, partial_json: str) -> None:
    """Start selecting every newly-completed brief. Never raises, never blocks.

    Called from the STREAM HANDLER on every input_json delta of an emit_briefs
    block — it must cost microseconds when there is nothing new.
    """
    try:
        briefs = completed_briefs(partial_json)
        if not briefs:
            return
        from app.orchestrator import select as _select
        from app.orchestrator.judge import judge as _judge

        with _lock:
            session = _store.setdefault(session_id, {})
            fresh = [
                b for b in briefs
                if b.strip() and b not in session
            ][: max(0, _MAX_BRIEFS_PER_SESSION - len(session))]
            for text in fresh:
                session[text] = _pool.submit(_select.select, text, judge=_judge)
        for text in fresh:
            log.info("speculating selection for brief %r — session=%s",
                     text[:60], session_id)
    except Exception:  # noqa: BLE001 — a head start must never cost the stream
        log.warning("speculative offer failed", exc_info=True)


def take(session_id: str) -> dict[str, Future]:
    """Pop this session's speculative selections — futures, possibly running.

    plan_turn resolves each with a bounded wait and falls back to its own
    select() on any failure, so a slow or dead future degrades to exactly the
    old serial path.
    """
    with _lock:
        return _store.pop(session_id, {})


def reset() -> None:
    """Tests only."""
    with _lock:
        _store.clear()
