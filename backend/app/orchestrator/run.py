"""Dispatch: run the plan, thread results forward, report per step (T3.3).

WHAT THIS DOES AND DELIBERATELY DOES NOT DO. It selects, orders and RUNS
read-only agents, collecting what each returns. It writes nothing — holds,
bookings, reschedules and cancels are Phase 4's, in write.py, where they can be
serialised and audited in one place.

RESULTS THREAD FORWARD, WHICH IS THE WHOLE REASON ORDER MATTERS. Sales produces
a vehicle_id; TestDrive is handed it. Without that, "a 7-seater and book me in
on Saturday" becomes two unrelated answers and the customer is asked which car
they meant, immediately after telling us.

EVERY STEP REPORTS A STATUS, AND A FAILURE IS NOT THE END OF THE TURN. The plan's
rule is "answer what worked, name what didn't": a Finance step that times out
must not discard the Sales answer already in hand. So each step comes back
`ok` / `failed` / `blocked` / `skipped`, and Aisha composes from findings AND
statuses — never silence, never a whole-turn error when half the facts are
already there.

FAN-OUT IS BY WAVE, not one-at-a-time. Two briefs that need nothing from each
other run together; a brief that needs a predecessor waits for it. That mapping
comes from order.independent_groups, so this module never decides ordering
itself.

"TOGETHER" MEANT "IN WAVE ORDER" UNTIL 2026-08-29, and the difference cost
whole seconds. The waves were right and a nested for-loop walked them one step
at a time anyway — a promise in this docstring that nothing enforced. It now
runs a wave concurrently, but ONLY for a runner that advertises `thread_safe`,
because the thing that made it serial was never the ordering: it was the shared
database session. See make_runner in subagent.py.

NOTHING HERE CALLS AN API BY ITSELF. Agents are injected — `runner(step, ctx)` —
so the caller decides what running an agent means. Tests pass a stub and exercise
the whole cascade offline; Phase 5 passes the real Haiku sub-graphs.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field

from app.orchestrator import order as _order
from app.orchestrator import select as _select

log = logging.getLogger("app.orchestrator.run")

# Per-step ceiling. The plan's AGENT_DEADLINE_SECONDS is stamped once for the
# whole cascade, so one slow sub-agent eats the composition budget and the turn
# dies with no reply at all. A step gets its own slice; what is left is Aisha's.
DEFAULT_STEP_TIMEOUT = 20.0


@dataclass
class StepResult:
    brief: str
    agent: str | None
    status: str                     # ok | failed | blocked | unroutable
    findings: dict = field(default_factory=dict)
    latency_ms: int = 0
    layer: str | None = None        # how the agent was chosen
    score: float | None = None
    margin: float | None = None
    detail: str = ""
    # What this step ASKED to have done. Requests, never results — the
    # orchestrator decides whether any of them happen, and the booking state
    # machine decides whether they are allowed yet. Lifted off `findings` so a
    # proposal can never reach Aisha's compose payload looking like a fact she
    # may report to the customer.
    proposals: list[dict] = field(default_factory=list)
    # WHAT THE TOOLS RETURNED, VERBATIM — see _EVIDENCE below. Lifted off
    # `findings` for the same reason `proposals` is: it is not something the
    # sub-agent WROTE, and mixing the two would let a model-authored string sit
    # in a field Aisha is told to treat as fact from the tools.
    evidence: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class TurnResult:
    steps: list[StepResult] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    entities: dict = field(default_factory=dict)

    @property
    def any_ok(self) -> bool:
        return any(s.ok for s in self.steps)

    @property
    def partial(self) -> bool:
        """Some worked and some did not — the case Aisha needs wording for."""
        return self.any_ok and any(not s.ok for s in self.steps)


def plan_turn(
    briefs: list[dict],
    *,
    known: set[str] | None = None,
    judge=None,
) -> tuple[_order.Plan, list[StepResult]]:
    """Select an agent per brief and order them. Returns (plan, unroutable).

    Briefs are `{"brief": str, "depends_on": [int]}` — Aisha's shape. A brief no
    agent owns does NOT abort the turn: it comes back unroutable and the others
    still run.
    """
    steps: list[_order.Step] = []
    unroutable: list[StepResult] = []

    for i, item in enumerate(briefs):
        text = (item.get("brief") or "").strip()
        chosen = _select.select(text, judge=judge)
        if chosen.agent is None:
            unroutable.append(
                StepResult(
                    brief=text, agent=None, status="unroutable",
                    layer=chosen.layer, score=chosen.score, margin=chosen.margin,
                    detail=chosen.reason,
                )
            )
            continue
        steps.append(
            _order.Step(
                brief=text,
                agent=chosen.agent,
                depends_on=tuple(item.get("depends_on") or ()),
                index=i,
            )
        )
        # Carry the selection detail so the trace can explain the decision.
        steps[-1].selection = chosen  # type: ignore[attr-defined]

    return _order.resolve(steps, known=known), unroutable


def execute(
    briefs: list[dict],
    runner,
    *,
    known: set[str] | None = None,
    judge=None,
    step_timeout: float = DEFAULT_STEP_TIMEOUT,
    max_workers: int = 1,
    on_tick=None,
) -> TurnResult:
    """Run every brief through its agent. Never raises.

    `runner(step, context) -> dict` is injected: it is what "running an agent"
    means, and keeping it out of here is what lets the whole cascade be tested
    without a model.

    `context` carries the entities produced so far, which is how a result
    threads forward — TestDrive receives the vehicle_id Sales just produced.
    """
    plan, unroutable = plan_turn(briefs, known=known, judge=judge)
    result = TurnResult(steps=list(unroutable), disagreements=plan.disagreements)
    result.entities = {k: None for k in (known or ())}

    # A step that can never run is reported with WHAT IT NEEDED, so Aisha can
    # ask the one question that unblocks it instead of apologising vaguely.
    for step, missing in plan.blocked:
        result.steps.append(
            StepResult(
                brief=step.brief, agent=step.agent, status="blocked",
                layer=getattr(getattr(step, "selection", None), "layer", None),
                detail=f"needs {', '.join(missing)}",
            )
        )

    def _run_one(step, entities: dict) -> StepResult:
        """One step, start to finish. Never raises — a failure is a StepResult.

        Runs on a worker thread when the wave is widened, so it touches nothing
        outside its arguments: `entities` is a per-wave snapshot, and the
        StepResult is folded back by the caller on the orchestrating thread.
        """
        started = time.monotonic()
        sel = getattr(step, "selection", None)
        try:
            findings = runner(step, entities) or {}
            status, detail = "ok", ""
        except TimeoutError:
            findings, status = {}, "failed"
            detail = f"timed out after {step_timeout:g}s"
            log.warning("step %s timed out", step.agent)
        except Exception as exc:  # noqa: BLE001 — one step, not the turn
            findings, status = {}, "failed"
            detail = f"{type(exc).__name__}: {exc}"
            log.exception("step %s failed", step.agent)

        evidence = findings.pop("_evidence", []) if isinstance(findings, dict) else []
        proposals = findings.pop("_proposals", []) if isinstance(findings, dict) else []
        if proposals and status != "ok":
            # A step that failed asked for nothing: its request rests on
            # facts it never established.
            log.warning("discarding %d proposal(s) from failed %s step",
                        len(proposals), step.agent)
            proposals = []

        latency_ms = int((time.monotonic() - started) * 1000)
        # NO BRIEF TEXT — same rule as trace_steps: the brief carries the
        # customer's profile. What is logged is the DECISION and the cost.
        log.info(
            "step %s %s in %dms (layer=%s entities=%s proposals=%d)%s",
            step.agent, status, latency_ms,
            getattr(sel, "layer", None),
            sorted(k for k in findings if not k.startswith("_")) if isinstance(findings, dict) else [],
            len(proposals), f" — {detail}" if detail else "",
        )

        return StepResult(
            brief=step.brief,
            agent=step.agent,
            status=status,
            findings=findings,
            proposals=proposals,
            evidence=evidence,
            latency_ms=latency_ms,
            layer=getattr(sel, "layer", None),
            score=getattr(sel, "score", None),
            margin=getattr(sel, "margin", None),
            detail=detail,
        )

    waves = _order.independent_groups(plan)
    cascade_started = time.monotonic()

    # HOW WIDE A WAVE MAY GO. A wave is by definition steps that need nothing
    # from each other, so running them together is what the ordering was FOR —
    # but only a runner that says it is thread-safe gets it. subagent.make_runner
    # sets that flag when it was given a session factory, and without one the
    # steps would share a SQLAlchemy Session across threads, which does not
    # raise reliably: it interleaves queries on one connection and returns
    # wrong rows. So the default here is 1, and every caller that has not
    # thought about sessions keeps exactly the behaviour it had.
    width = max(1, int(max_workers))
    if width > 1 and not getattr(runner, "thread_safe", False):
        log.info("runner is not thread-safe — running waves one step at a time")
        width = 1

    log.info("cascade start: %d step(s) in %d wave(s) — widths=%s, max_parallel=%d",
             sum(len(w) for w in waves), len(waves), [len(w) for w in waves], width)

    for wave in waves:
        # ONE SNAPSHOT PER WAVE, not per step. Steps inside a wave are
        # independent, so none of them may read another's entities — taking the
        # snapshot here makes that explicit instead of leaving it to the order
        # they happen to run in, and it is what lets them run at once.
        snapshot = dict(result.entities)
        n = min(len(wave), width)

        if n <= 1:
            done = [_run_one(step, snapshot) for step in wave]
        else:
            log.info("wave of %d running %d at a time", len(wave), n)
            with ThreadPoolExecutor(max_workers=n, thread_name_prefix="cascade") as pool:
                futures = [pool.submit(_run_one, step, snapshot) for step in wave]
                # PUMPED FROM THIS THREAD, and that is the point. Sub-agent
                # tool_events draw the customer's cards; the parent stream
                # writer is not documented to be thread-safe, so workers queue
                # their events and this loop is what hands them over — one
                # thread, in order, while the wave is still running.
                pending = set(futures)
                while pending:
                    _, pending = futures_wait(pending, timeout=0.05)
                    if on_tick is not None:
                        on_tick()
                # Wave order, NOT completion order: the transcript and every
                # test that reads result.steps must not depend on which
                # specialist happened to answer first.
                done = [f.result() for f in futures]

        if on_tick is not None:
            on_tick()

        result.steps.extend(done)
        # Only a SUCCESSFUL step contributes entities. A failed Sales step
        # must not leave a vehicle_id behind for TestDrive to trust.
        for produced in done:
            if produced.ok:
                for key in _order.produces(produced.agent):
                    if key in produced.findings:
                        result.entities[key] = produced.findings[key]

    log.info("cascade done in %dms — %d ok, %d not",
             int((time.monotonic() - cascade_started) * 1000),
             sum(1 for s_ in result.steps if s_.ok),
             sum(1 for s_ in result.steps if not s_.ok))
    return result


def trace_steps(result: TurnResult) -> list[dict]:
    """The per-step record for turn_trace.steps (T3.5).

    NO BRIEF TEXT. The brief carries the conversation profile — family size,
    area, budget — which is personal data, and this column is written on every
    turn. What lands here is the DECISION: which agent, chosen how, how
    confident, how long, and whether it worked.
    """
    return [
        {
            "agent": s.agent,
            "status": s.status,
            "layer": s.layer,
            "score": round(s.score, 4) if s.score is not None else None,
            "margin": round(s.margin, 4) if s.margin is not None else None,
            "latency_ms": s.latency_ms,
            "detail": s.detail or None,
        }
        for s in result.steps
    ]
