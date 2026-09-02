"""The only place a commercial write happens (T4.2).

THE SPLIT IS BY KIND, NOT BY LAYER, and getting that right is the whole design.
A write that is an act of DIALOGUE — who you are, a yes, a licence
acknowledgement, a remembered preference, goodbye — belongs to whoever is
talking, and only Aisha talks. A write that is an act of COMMERCE — holding
inventory, creating, moving or cancelling a booking — belongs here, where it can
be serialised and audited in one place.

The rule is "only the orchestrator BOOKS", never "only the orchestrator writes",
which was never achievable: 12 of 22 tools write, and most are triggered by
someone saying something.

SUB-AGENTS PROPOSE; THEY DO NOT EXECUTE. TestDrive's card lists hold_slot,
create_booking, reschedule_booking and cancel_booking under `proposes`, not
`tools` — it returns them as DATA and this module decides whether they happen.
That is what makes it safe to run a sub-agent on Haiku.

THE SERVICE-LAYER GUARDS ARE UNCHANGED BENEATH THIS, deliberately. create_booking
still refuses without the licence acknowledgement; lock ownership is still
enforced by the slot service; a recap-yes is still not messaging consent. This
module is a gate in FRONT of those guards, never a replacement for them — a
second write path that happened to skip one is exactly the defect the whole
architecture exists to prevent.

ONE TRANSACTION, ONE CORRELATION ID. Every write here runs inside the caller's
transaction and is stamped with a single correlation id, so the choke-point audit
row and the service's semantic row group as one action in the console rather
than reading as double-logging.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from app.tools import COMMERCIAL_WRITE_TOOLS

log = logging.getLogger("app.orchestrator.write")

# What a sub-agent is allowed to ASK for. Deliberately the same set as
# COMMERCIAL_WRITE_TOOLS plus the proposal alias `hold_slot`, which is what the
# card calls the action that `lock_slot` performs.
PROPOSABLE: frozenset[str] = COMMERCIAL_WRITE_TOOLS | {"hold_slot"}

# Proposal name -> the tool that actually performs it.
_ACTION_TO_TOOL = {
    "hold_slot": "lock_slot",
    "lock_slot": "lock_slot",
    "create_booking": "create_booking",
    "reschedule_booking": "reschedule_booking",
    "cancel_booking": "cancel_booking",
}


@dataclass(frozen=True)
class Proposal:
    """What a sub-agent asked for. Data, not an instruction."""

    action: str
    args: dict = field(default_factory=dict)
    agent: str | None = None


@dataclass
class WriteResult:
    action: str
    status: str                 # executed | refused | failed
    result: dict = field(default_factory=dict)
    detail: str = ""
    correlation_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "executed"


def _refuse(proposal: Proposal, why: str) -> WriteResult:
    log.warning("refused %s from %s: %s", proposal.action, proposal.agent, why)
    return WriteResult(action=proposal.action, status="refused", detail=why)


def execute(
    db,
    session_id: str,
    proposal: Proposal,
    *,
    customer_id: int | None = None,
    allow: set[str] | None = None,
    correlation_id: str | None = None,
) -> WriteResult:
    """Perform one proposed commercial write, or refuse it. Never raises.

    `allow` is the booking state machine's gate: the set of actions valid RIGHT
    NOW. Passed in rather than consulted here, so the machine that owns the
    state is the thing that decides what may happen next, and this module stays
    a choke point rather than becoming a second copy of the flow.

    DOES NOT COMMIT. The caller owns the transaction — an audit row must live or
    die with the write it describes.
    """
    action = (proposal.action or "").strip()

    if action not in PROPOSABLE:
        # Includes anything a sub-agent invented. A proposal is a REQUEST, and
        # an unrecognised one is refused rather than attempted.
        return _refuse(proposal, f"{action!r} is not a proposable commercial action")

    if allow is not None and action not in allow:
        # THE STATE MACHINE'S REFUSAL, and it is not an error — it is the flow
        # working. "create_booking before the licence acknowledgement" is a
        # step out of order, and the answer is to do the missing step.
        return _refuse(
            proposal, f"{action} is not valid at this point (allowed: {sorted(allow)})"
        )

    tool_name = _ACTION_TO_TOOL[action]
    correlation_id = correlation_id or uuid.uuid4().hex

    from app.tools import TOOL_FNS

    fn = TOOL_FNS.get(tool_name)
    if fn is None:
        return _refuse(proposal, f"no such tool: {tool_name}")

    args = dict(proposal.args or {})
    # TRUSTED CONTEXT IS INJECTED, NEVER TAKEN FROM THE PROPOSAL. A sub-agent
    # that could name its own customer_id could write for somebody else, which
    # is the single most dangerous thing a model could get wrong here. Any
    # value it supplied is discarded, not merged.
    for reserved in ("db", "session_id", "customer_id"):
        args.pop(reserved, None)

    try:
        if customer_id is not None:
            result = fn(db, session_id, customer_id=customer_id, **args)
        else:
            result = fn(db, session_id, **args)
    except Exception as exc:  # noqa: BLE001 — one write, not the turn
        log.exception("commercial write %s failed", tool_name)
        return WriteResult(
            action=action, status="failed",
            detail=f"{type(exc).__name__}: {exc}", correlation_id=correlation_id,
        )

    if isinstance(result, dict) and result.get("error"):
        # A SERVICE GUARD SAID NO — the licence reminder, lock ownership, a slot
        # already taken. Reported as data so Aisha can say what happened, and
        # NOT converted into an exception: this is the system working.
        #
        # THE REMEDIATION RIDES ALONG (found live, 2026-08-31). The services
        # write their refusals in two parts — a slug ("identity_not_verified")
        # and a `detail` sentence saying what to DO about it. Passing only the
        # slug starved Aisha into guessing: told just "refused:
        # identity_not_verified", she announced the customer's own held slot
        # had been taken. The sentence names the next step; she follows it.
        return WriteResult(
            action=action, status="refused",
            result=result,
            detail=str(result.get("detail") or result.get("error")),
            correlation_id=correlation_id,
        )

    log.info("commercial write %s executed (corr=%s)", tool_name, correlation_id)
    return WriteResult(
        action=action, status="executed",
        result=result if isinstance(result, dict) else {},
        correlation_id=correlation_id,
    )


def execute_all(
    db,
    session_id: str,
    proposals: list[Proposal],
    *,
    customer_id: int | None = None,
    allow: set[str] | None = None,
    allow_for=None,
) -> list[WriteResult]:
    """Several proposals under ONE correlation id, in order.

    STOPS AT THE FIRST REFUSAL. Proposals from one turn are a sequence — hold
    then book — and carrying on after a hold was refused would create a booking
    against a slot nobody holds.

    `allow_for` RE-DERIVES THE GATE BEFORE EACH PROPOSAL, and it exists because
    a static `allow` snapshot was a bug (found live 2026-09-02). A sub-agent may
    legitimately propose `hold_slot` then `create_booking` in one turn — its
    card lists both — and the booking state machine's `allowed` set is derived
    from the slot's live state. Read ONCE before the batch, that snapshot still
    said "hold stage, create_booking not valid" on the second proposal even
    though the first had just locked the slot in this very session, so a
    complete booking was refused and the customer told to phone in. `allow_for`
    (a zero-arg callable returning the current allowed set) is consulted per
    proposal instead — execute() does not commit, but it flushes, so the state
    machine reading the same session sees the pending hold. `allow` stays the
    fallback for callers that pass a fixed set (and every existing test).
    """
    correlation_id = uuid.uuid4().hex
    out: list[WriteResult] = []
    for proposal in proposals:
        current_allow = allow
        if allow_for is not None:
            try:
                current_allow = allow_for()
            except Exception:  # noqa: BLE001 — a bad refresh falls back to `allow`
                log.warning("allow_for refresh failed; using the prior gate", exc_info=True)
        res = execute(
            db, session_id, proposal,
            customer_id=customer_id, allow=current_allow, correlation_id=correlation_id,
        )
        out.append(res)
        if not res.ok:
            for skipped in proposals[len(out):]:
                out.append(
                    WriteResult(
                        action=skipped.action, status="refused",
                        detail=f"skipped: {proposal.action} did not succeed",
                        correlation_id=correlation_id,
                    )
                )
            break
    return out
