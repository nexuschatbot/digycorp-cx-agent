"""Writing one turn_trace row. Fail-safe by construction.

THIS MUST NEVER TAKE A TURN DOWN. A trace is telemetry; a turn is a customer
waiting for an answer. Every public function here swallows its own errors and
logs them, the same contract services/salesforce.py holds for the same reason —
a table that is full, locked, or missing a column has no business ending a
conversation about a test drive.

THE NULLS ARE THE MEASUREMENT. A turn settled by the pre-filter has no
confidence and no margin, and storing 1.0 there to avoid a NULL would destroy the
statistic this table exists to produce: `WHERE routed_by = 'classifier'` is what
the model actually cost, and `router_confidence IS NULL` is what it did not.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import TurnTrace

_log = logging.getLogger(__name__)


def _live_versions() -> dict | None:
    """Which prompt version each agent is running, or None if unknowable.

    Never raises: telemetry must not be able to break a turn, and this is the
    one field that reaches into the prompt layer.
    """
    try:
        from app.agents import loader

        return loader.active_versions()
    except Exception:  # noqa: BLE001
        return None


def record(
    db: Session,
    *,
    trace_id: str,
    session_id: str,
    routing: Any = None,
    customer_id: int | None = None,
    channel: str | None = None,
    user_message: str | None = None,
    detected_lang: str | None = None,
    previous_agent: str | None = None,
    prompt_versions: dict | None = None,
    tool_calls: list | None = None,
    agent_response: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    router_latency_ms: int | None = None,
    latency_ms: int | None = None,
    sf_api_calls: int | None = None,
    cache_hits: int | None = None,
) -> int | None:
    """Persist one turn. Returns the row id, or None if anything went wrong."""
    try:
        decision = getattr(routing, "decision", None) if routing else None
        routed_to = getattr(routing, "agent", None) if routing else None

        row = TurnTrace(
            trace_id=trace_id,
            session_id=session_id,
            customer_id=customer_id,
            channel=channel,
            user_message=user_message,
            detected_lang=detected_lang,
            routed_to=routed_to,
            previous_agent=previous_agent,
            # DEFAULTS TO THE LIVE VERSIONS rather than requiring every caller
            # to pass them: a turn that forgot would record null and look like
            # a pre-Phase-2 turn, which is a different and misleading claim.
            prompt_versions=prompt_versions if prompt_versions is not None else _live_versions(),
            routed_by=getattr(routing, "layer", None) if routing else None,
            router_reason=(getattr(routing, "reason", None) or None) if routing else None,
            # Only a classifier turn has these. See the module docstring.
            router_confidence=decision.confidence if decision else None,
            router_margin=decision.margin if decision else None,
            router_second_best=decision.second_best if decision else None,
            switched=bool(
                routing and previous_agent and routed_to and routed_to != previous_agent
            ),
            clarified=bool(getattr(routing, "clarify", False)) if routing else False,
            tool_calls=tool_calls or [],
            agent_response=agent_response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            router_latency_ms=router_latency_ms,
            latency_ms=latency_ms,
            sf_api_calls=sf_api_calls,
            cache_hits=cache_hits,
        )
        db.add(row)
        db.commit()
        return row.id
    except Exception:
        _log.warning("turn_trace write failed", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def misroute_candidates(db: Session, *, limit: int = 50) -> list[TurnTrace]:
    """Turns worth a human eye: a switch, a clarify, or a thin margin.

    Doc §9's weekly review is the only known catch for a misroute that "works"
    but loses the sale, and this is the shortlist it should start from.
    """
    try:
        return (
            db.query(TurnTrace)
            .filter(
                (TurnTrace.switched.is_(True))
                | (TurnTrace.clarified.is_(True))
                | (TurnTrace.router_margin < 0.15)
            )
            .order_by(TurnTrace.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        _log.warning("turn_trace query failed", exc_info=True)
        return []
