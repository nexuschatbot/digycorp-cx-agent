"""One turn at a time, per session (T4.5).

THE RACE THIS CLOSES. Two POSTs on one session_id spawn two threads writing the
same checkpointer thread, and LangGraph's SqliteSaver has no notion of a second
writer — the later write wins and the earlier turn's messages vanish, or the two
interleave into a transcript that never happened.

It has always been possible. It becomes LIKELY in Phase 5: turn duration goes
from ~2s to ~4-6s once a turn fans out to sub-agents, so the window in which a
second message can arrive roughly triples.

THE UI IS THE REAL FIX; THIS IS THE BACKSTOP. The composer is blocked while a
turn is in flight (T5.7), which handles the ordinary case. This handles the ones
the UI cannot: two tabs, a reload mid-turn, a client that ignores the flag, and
the WhatsApp path where there is no composer at all.

REJECT, DO NOT QUEUE. A queued second message would be answered against state
the customer has not seen yet, minutes later, out of context. Telling them we
are still working on the first is honest and recoverable; silently answering a
stale question is not.

IN-PROCESS ONLY, and that is a stated limit rather than an oversight. The
Dockerfile runs a single uvicorn process, so a per-process lock IS the whole
system today. The day it becomes several processes this must move to Redis or a
database row, and the note in `acquire` says so.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

log = logging.getLogger("app.agent.session_guard")

_lock = threading.Lock()
_busy: dict[str, float] = {}


class SessionBusy(RuntimeError):
    """A turn is already running for this session."""

    def __init__(self, session_id: str):
        super().__init__(f"a turn is already running for session {session_id}")
        self.session_id = session_id


@contextmanager
def acquire(session_id: str):
    """Hold the turn slot for one session, or raise SessionBusy.

    NOT A THREADING.LOCK PER SESSION, deliberately: that would BLOCK the second
    request until the first finished, which is queueing wearing a different hat.
    A flag that refuses is the behaviour we want.

    SINGLE PROCESS ONLY. See the module docstring — this is the whole system
    while the Dockerfile runs one uvicorn, and must become shared state the day
    it does not.
    """
    import time

    if not session_id:
        # No session to protect. Better than inventing a key that collides.
        yield
        return

    with _lock:
        if session_id in _busy:
            held_for = time.monotonic() - _busy[session_id]
            log.info("rejected a concurrent turn for %s (held %.1fs)", session_id, held_for)
            raise SessionBusy(session_id)
        _busy[session_id] = time.monotonic()
    try:
        yield
    finally:
        # RELEASED IN `finally`, ALWAYS. A turn that raised must not leave the
        # session permanently wedged — that would be a worse failure than the
        # race it is guarding against.
        with _lock:
            _busy.pop(session_id, None)


def is_busy(session_id: str) -> bool:
    with _lock:
        return session_id in _busy


def reset() -> None:
    """Clear every held session. Tests only."""
    with _lock:
        _busy.clear()
