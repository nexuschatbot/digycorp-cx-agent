"""EG management insights — the read-only API behind the Salesforce-linked
management portal (2026-08-05).

WHAT IT DOES. Joins three things the business already produces and presents them
for NON-TECHNICAL management, so LangSmith's developer view never has to be:
  - the CUSTOMER RANKING — the 1-5 star rating + comment left at chat/call close
    (agent_feedback, mirrored to Salesforce's Test_Drive_Agent_Feedback__c),
  - the AI's OWN ASSESSMENT — the LangSmith evaluator scores for that session_id
    (accuracy, language, compliance, warmth, and for calls audio/transcription),
  - the TRANSCRIPT — the stored ConversationTurn rows for the session.
It then translates the evaluator numbers into plain-language verdicts and a
one-line "likely reason" for the rating.

An ADDITIVE read layer: it changes nothing about how feedback reaches Salesforce
or how runs reach LangSmith. Read-only, and — for this provisioning build —
UNAUTHENTICATED (the portal is opened from a Salesforce deep link; access
hardening is a deliberate, separate follow-up).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AgentFeedback, ConversationTurn

log = logging.getLogger("app.api.insights")

router = APIRouter(prefix="/api/insights", tags=["insights"])

# Each LangSmith evaluator feedback key → (management label, higher_is_better).
# Direction matters: `hallucination` and `audio_quality` score 1 when a PROBLEM
# is present, so for them LOWER is better; the rest are quality measures where
# 1 is good. Unknown keys pass through with higher-is-better assumed.
_DIMENSIONS: dict[str, tuple[str, bool]] = {
    "answer_relevance": ("Helpfulness", True),
    "conciseness": ("Conciseness", True),
    "hallucination": ("Accuracy", False),
    "language_match": ("Right language", True),
    "pdpl_compliance": ("Compliance", True),
    "cultural_fit": ("Cultural fit", True),
    "warmth": ("Warmth", True),
    "audio_quality": ("Audio quality", False),
    "vocal_affect": ("Vocal tone", True),
    "transcription_accuracy": ("Transcription", True),
    "user_interrupts": ("Handled interruptions", True),
}

# Evaluators that only make sense for a VOICE CALL (they score audio / speech).
# On a chat session they run against no audio and return noise (e.g. Vocal tone
# 0%), so they are hidden from the chat drill-down and only shown for calls.
_VOICE_ONLY: set[str] = {
    "audio_quality",
    "vocal_affect",
    "transcription_accuracy",
    "user_interrupts",
}


def _mask_mobile(mobile: str | None) -> str | None:
    """Show enough to recognise the customer, hide the rest (no-auth portal)."""
    if not mobile:
        return None
    digits = mobile.strip()
    if len(digits) <= 4:
        return digits
    return digits[:-4].rstrip() + " ••" + digits[-2:]


def _verdict(good: float) -> str:
    """`good` is normalised so 1.0 is always best, whatever the metric's raw
    direction — so one threshold serves every dimension."""
    return "good" if good >= 0.7 else ("warn" if good >= 0.4 else "bad")


def _is_rating_key(key: str) -> bool:
    """The customer's own star rating is also logged to LangSmith as feedback; it
    is the ranking, not an AI assessment, so it never appears among the dimensions."""
    return key.lower().strip().replace(" ", "_") in {"customer_rating", "user_rating", "stars"}


def _goodness(key: str, vals: list[float]) -> tuple[str, float]:
    """(management label, goodness) for one evaluator key, where goodness is
    normalised so 1.0 is always best regardless of the metric's raw direction."""
    label, hib = _DIMENSIONS.get(key, (key.replace("_", " ").title(), True))
    raw = sum(vals) / len(vals)
    return label, (raw if hib else (1.0 - raw))


def _why(stars: int | None, dims: list[dict]) -> str:
    bad = [d["label"].lower() for d in dims if d["verdict"] == "bad"]
    warn = [d["label"].lower() for d in dims if d["verdict"] == "warn"]
    good = [d["label"].lower() for d in dims if d["verdict"] == "good"]
    if stars is not None and stars <= 2:
        if bad:
            return f"Likely why it was rated low: the AI scored weak on {', '.join(bad[:3])}."
        if warn:
            return f"Rated low; the AI flagged some concern on {', '.join(warn[:3])}."
        return "Rated low, but the AI evaluators didn't flag a clear problem — worth a human look."
    if stars is not None and stars >= 4:
        if good:
            return f"Rated well; the AI scored strongly on {', '.join(good[:3])}."
        return "Rated well by the customer."
    if bad:
        return f"The AI flagged issues with {', '.join(bad[:3])}."
    return "No major issues flagged by the AI evaluators."


def _langsmith_scores(session_id: str) -> tuple[dict[str, list[float]], int]:
    """Evaluator scores for every run tagged with this session_id, grouped by
    feedback key. Returns ({key: [scores]}, run_count). Never raises here — the
    caller wraps it — but returns empty when LangSmith is unreachable."""
    from langsmith import Client

    client = Client()
    project = os.getenv("LANGSMITH_PROJECT") or "default"
    # ROOT runs only, one per turn — the evaluators score the trace root, so
    # this is where their feedback lives, and it keeps the count to the ~turns.
    # `eq(metadata_value, ...)` matches the run whose metadata carries this id
    # (session_ids are unique). LIMIT 100 is LangSmith's hard maximum — asking
    # for more 400s the whole query.
    runs = list(
        client.list_runs(
            project_name=project,
            filter=f'and(eq(metadata_value, "{session_id}"), eq(is_root, true))',
            limit=100,
        )
    )
    run_count = len(runs)
    scores: dict[str, list[float]] = {}
    # Feedback lookup is separate so a failure here never loses the run count.
    try:
        for i in range(0, len(runs), 100):
            batch = [r.id for r in runs[i : i + 100]]
            for fb in client.list_feedback(run_ids=batch):
                if fb.key and fb.score is not None:
                    scores.setdefault(fb.key, []).append(float(fb.score))
    except Exception:  # noqa: BLE001
        log.warning("list_feedback failed — session=%s", session_id[:8], exc_info=True)
    return scores, run_count


@router.get("/feedback")
def list_feedback(db: Session = Depends(get_db)) -> dict:
    """Every customer ranking, newest first, plus a summary strip. The list the
    portal lands on; each row deep-links to /session/{id}."""
    rows = (
        db.query(AgentFeedback)
        .order_by(AgentFeedback.created_at.desc(), AgentFeedback.id.desc())
        .limit(300)
        .all()
    )
    items = [
        {
            "id": r.id,
            "session_id": r.session_id,
            "client_name": r.client_name or "Guest",
            "mobile": _mask_mobile(r.mobile),
            "stars": r.stars,
            "comment": r.comment,
            "booking_ref": r.booking_ref,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    total = len(rows)
    avg = round(sum(r.stars for r in rows) / total, 2) if total else 0.0
    dist = {str(s): sum(1 for r in rows if r.stars == s) for s in range(1, 6)}
    positive = sum(1 for r in rows if r.stars >= 4)
    negative = sum(1 for r in rows if r.stars <= 2)
    return {
        "summary": {
            "total": total,
            "avg_stars": avg,
            "distribution": dist,
            "positive": positive,
            "neutral": total - positive - negative,
            "negative": negative,
        },
        "items": items,
    }


# How many low-rated sessions to pull LangSmith scores for. Each session is one
# list_runs + one list_feedback round-trip, so this bounds the endpoint's latency;
# 20 is plenty to see a stable pattern for a demo dataset.
_DRIVERS_MAX_SESSIONS = 20


@router.get("/drivers")
def rating_drivers(db: Session = Depends(get_db)) -> dict:
    """"Why customers rate low." Across the low-rated sessions (1-2 stars), which
    AI qualities scored weakest — the likeliest drivers of poor ratings. Returns
    each dimension's average goodness over those sessions, worst first, so the
    management view opens on the single most actionable insight."""
    negs = (
        db.query(AgentFeedback)
        .filter(AgentFeedback.stars <= 2)
        .order_by(AgentFeedback.created_at.desc(), AgentFeedback.id.desc())
        .limit(200)
        .all()
    )
    # One session may have several feedback rows; score each session once.
    seen: set[str] = set()
    session_ids: list[str] = []
    for r in negs:
        if r.session_id in seen:
            continue
        seen.add(r.session_id)
        session_ids.append(r.session_id)
    low_rated_total = len(session_ids)
    session_ids = session_ids[:_DRIVERS_MAX_SESSIONS]

    agg: dict[str, list[float]] = {}
    analyzed = 0
    for sid in session_ids:
        try:
            scores, _ = _langsmith_scores(sid)
        except Exception:  # noqa: BLE001 — one bad session never sinks the chart
            log.warning("drivers: scores failed — session=%s", sid[:8], exc_info=True)
            continue
        if not scores:
            continue
        analyzed += 1
        for key, vals in scores.items():
            if _is_rating_key(key):
                continue
            _label, good = _goodness(key, vals)
            agg.setdefault(key, []).append(good)

    drivers: list[dict] = []
    for key, goods in agg.items():
        label, _hib = _DIMENSIONS.get(key, (key.replace("_", " ").title(), True))
        avg = sum(goods) / len(goods)
        weak = sum(1 for g in goods if g < 0.7)
        drivers.append(
            {
                "key": key,
                "label": label,
                "avg_score": round(avg, 2),
                "weak_sessions": weak,
                "total_sessions": len(goods),
                "verdict": _verdict(avg),
            }
        )
    # Worst (lowest goodness) first — that is the biggest driver of low ratings.
    drivers.sort(key=lambda d: d["avg_score"])
    return {
        "sessions_analyzed": analyzed,
        "low_rated_total": low_rated_total,
        "drivers": drivers[:8],
    }


@router.get("/session/{session_id}")
def session_insight(session_id: str, db: Session = Depends(get_db)) -> dict:
    """The drill-down: the customer's ranking, the AI's per-dimension verdict
    (real LangSmith scores, plain-language), a one-line 'likely reason', and the
    transcript."""
    fb = (
        db.query(AgentFeedback)
        .filter(AgentFeedback.session_id == session_id)
        .order_by(AgentFeedback.id.desc())
        .first()
    )
    turns = (
        db.query(ConversationTurn)
        .filter(ConversationTurn.session_id == session_id)
        .order_by(ConversationTurn.id)
        .all()
    )
    transcript = [
        {
            "speaker": t.speaker.value,
            "text": t.text,
            "lang": t.lang.value if t.lang else None,
        }
        for t in turns
        if t.text and not (t.text or "").startswith("call:")
    ]
    channel = "call" if any((t.text or "").startswith("call:") for t in turns) else "chat"

    # Session timing, straight from the transcript rows (ConversationTurn.ts):
    # first turn = when it started, last turn = when it ended, and the gap is the
    # conversation length. Shown on the drill-down so management sees WHEN and HOW
    # LONG, not only the verdict.
    ts_vals = [t.ts for t in turns if t.ts is not None]
    started_at = min(ts_vals).isoformat() if ts_vals else None
    ended_at = max(ts_vals).isoformat() if ts_vals else None
    duration_seconds = (
        int((max(ts_vals) - min(ts_vals)).total_seconds()) if len(ts_vals) >= 2 else None
    )
    message_count = len(transcript)

    scores: dict[str, list[float]] = {}
    run_count = 0
    try:
        scores, run_count = _langsmith_scores(session_id)
    except Exception:  # noqa: BLE001 — the portal degrades, it never 500s
        log.warning("langsmith session scores failed — session=%s", session_id[:8], exc_info=True)

    dimensions: list[dict] = []
    for key, vals in scores.items():
        if _is_rating_key(key):
            continue
        # Voice-only metrics are meaningless on a chat — hide them there.
        if channel != "call" and key in _VOICE_ONLY:
            continue
        # Normalise to GOODNESS (1.0 = best) so the % the portal shows always
        # reads the intuitive way — a low hallucination/audio-issue rate is high
        # accuracy/audio quality.
        label, good = _goodness(key, vals)
        dimensions.append(
            {"key": key, "label": label, "score": round(good, 2), "verdict": _verdict(good)}
        )
    dimensions.sort(key=lambda d: (d["verdict"] != "bad", d["verdict"] != "warn", d["label"]))

    rating = None
    if fb is not None:
        rating = {
            "stars": fb.stars,
            "comment": fb.comment,
            "client_name": fb.client_name or "Guest",
            "mobile": _mask_mobile(fb.mobile),
            "booking_ref": fb.booking_ref,
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        }

    return {
        "session_id": session_id,
        "channel": channel,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "message_count": message_count,
        "rating": rating,
        "dimensions": dimensions,
        "runs_evaluated": run_count,
        "why": _why(fb.stars if fb else None, dimensions),
        "transcript": transcript,
    }
