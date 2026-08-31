"""Retention: delete what we are no longer entitled to keep (T4.6).

WHY THIS FILE EXISTS AT ALL. models.py has declared retention windows for a
while — the columns say how long a thing may be kept — and NOTHING EVER READ
THEM. A retention policy nobody enforces is not a policy, and under PDPL it is
the difference between a stated safeguard and an actual one.

DELETION IS BY AGE, NOT BY PERSON, which is what makes it different from
erasure.py. Erasure answers "this customer asked to be forgotten"; retention
answers "nobody asked, and we should not still have this". They share nothing
but the verb.

WHAT IS KEPT AND FOR HOW LONG, and why each is what it is:

    conversation_turns   transcripts. The longest of the three, because a
                         returning customer's history is what makes recognition
                         useful — but not indefinite.
    turn_trace           per-turn telemetry, INCLUDING the message text. Shorter,
                         because its value is operational (routing accuracy,
                         latency, cost) and that value decays in weeks.
    speech_clips         synthesised audio. Shortest: it is a rendering of text
                         we still hold, so keeping it buys nothing after the
                         conversation ends.

DRY RUN BY DEFAULT. `sweep(dry_run=True)` counts and returns; nothing is deleted
unless a caller asks for it. A retention job that deletes on import is how a
staging run takes production data with it.

NEVER TOUCHES BOOKINGS, CUSTOMERS OR CONSENT. Those have their own lifecycle and
their own legal basis — a booking is a commercial record, not telemetry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = logging.getLogger("app.services.retention")

# Days. Deliberately named constants rather than literals in the query: these
# are policy, they get quoted in a privacy review, and they change together.
TRANSCRIPT_DAYS = 365
TRACE_DAYS = 90
SPEECH_CLIP_DAYS = 30


def _cutoff(days: int, *, now: datetime | None = None) -> datetime:
    """The age boundary.

    NAIVE LOCAL TIME, matching services/booking.py and services/consent.py,
    which both use `datetime.now()`. The stored timestamps are written by
    `func.now()` in the same frame, so comparing against a UTC value here would
    shift every window by the timezone offset — a silent few hours of extra or
    missing retention, in whichever direction hurt more.

    `now` is injectable so a test can age rows without waiting a year.
    """
    return (now or datetime.now()) - timedelta(days=days)


def survey(db: Session, *, now: datetime | None = None) -> dict:
    """What WOULD be deleted, without deleting it. Safe to call anywhere."""
    from app.models import ConversationTurn, SpeechClip, TurnTrace

    return {
        "conversation_turns": int(
            db.scalar(
                select(func.count()).select_from(ConversationTurn).where(
                    ConversationTurn.ts < _cutoff(TRANSCRIPT_DAYS, now=now)
                )
            ) or 0
        ),
        "turn_trace": int(
            db.scalar(
                select(func.count()).select_from(TurnTrace).where(
                    TurnTrace.created_at < _cutoff(TRACE_DAYS, now=now)
                )
            ) or 0
        ),
        "speech_clips": int(
            db.scalar(
                select(func.count()).select_from(SpeechClip).where(
                    SpeechClip.created_at < _cutoff(SPEECH_CLIP_DAYS, now=now)
                )
            ) or 0
        ),
    }


def sweep(db: Session, *, dry_run: bool = True, now: datetime | None = None) -> dict:
    """Delete everything past its window. DRY RUN BY DEFAULT.

    Does NOT commit — the caller owns the transaction, exactly as erasure does,
    so a failure part-way through takes the whole sweep with it rather than
    leaving a half-applied policy.
    """
    counts = survey(db, now=now)
    counts["dry_run"] = dry_run
    if dry_run:
        return counts

    from app.models import ConversationTurn, SpeechClip, TurnTrace

    db.execute(
        sa_delete(ConversationTurn).where(
            ConversationTurn.ts < _cutoff(TRANSCRIPT_DAYS, now=now)
        )
    )
    db.execute(sa_delete(TurnTrace).where(TurnTrace.created_at < _cutoff(TRACE_DAYS, now=now)))
    db.execute(
        sa_delete(SpeechClip).where(SpeechClip.created_at < _cutoff(SPEECH_CLIP_DAYS, now=now))
    )
    log.info("retention sweep deleted %s", {k: v for k, v in counts.items() if k != "dry_run"})
    return counts
