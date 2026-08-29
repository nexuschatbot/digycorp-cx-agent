"""Live smoke test for the Haiku sub-agent binding (Phase 9).

    python -m evals.subagent_smoke --live --cap 0.50

WHAT IT PROVES, AND WHAT IT DOES NOT. It runs a handful of real briefs through
the real cascade — select, order, dispatch, Haiku, tools — and checks that
findings come back and thread forward. It is NOT a quality eval: three briefs say
nothing about routing accuracy, and the routing view exists for that.

THE CAP IS CHECKED BEFORE EACH BRIEF, NOT AFTER. Same discipline as
evals/baseline.py, and for the same reason: a ceiling discovered afterwards is
not a ceiling. Haiku is cheap enough that this should cost cents, which is
exactly when a guard is easiest to leave out and least missed until it is not.

NOTHING IS WRITTEN. Sub-agents hold read-only tools, so this cannot create a
booking or hold a slot however the models behave.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# Haiku 4.5, USD per million tokens. Stated rather than fetched — the same
# assumption baseline.py makes, and it is an assumption.
PRICE_IN_PER_M = 1.00
PRICE_OUT_PER_M = 5.00

BRIEFS = [
    # One per agent, chosen so the cascade has to do real work rather than
    # answer from the brief text.
    {"brief": "what seven seaters do you have and what do they cost"},
    {"brief": "what times are free at your Deira branch on Saturday"},
    {"brief": "what would the monthly instalments be on the X50"},
]


class _Meter:
    def __init__(self) -> None:
        self.usd = 0.0
        self.calls = 0

    def add(self, usage) -> None:
        self.calls += 1
        self.usd += (
            getattr(usage, "input_tokens", 0) * PRICE_IN_PER_M / 1_000_000
            + getattr(usage, "output_tokens", 0) * PRICE_OUT_PER_M / 1_000_000
        )

    @property
    def per_call(self) -> float:
        return self.usd / self.calls if self.calls else 0.01


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="actually call the API")
    ap.add_argument("--cap", type=float, default=0.50)
    args = ap.parse_args()

    if not args.live:
        print("Dry run. Briefs that would be sent:")
        for b in BRIEFS:
            print(f"  · {b['brief']}")
        print(f"\nRun for real with:  python -m evals.subagent_smoke --live --cap {args.cap}")
        return 0

    from app import config
    from app.db import SessionLocal
    from app.orchestrator import judge as judge_mod
    from app.orchestrator import select, subagent, turn

    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    meter = _Meter()

    # Wrap the client so every call is metered before the next brief starts.
    real_client = subagent._client
    def metered_client():
        client = real_client()
        create = client.messages.create

        def _create(**kw):
            resp = create(**kw)
            if getattr(resp, "usage", None):
                meter.add(resp.usage)
            return resp

        client.messages.create = _create
        return client

    subagent._client = metered_client

    db = SessionLocal()
    session_id = "smoke-subagent"
    results = []

    print(f"\nmodel: {config.CLAUDE_MODEL}   cap: ${args.cap:.2f}\n")

    for i, brief in enumerate(BRIEFS, 1):
        headroom = max(meter.per_call * 6, 0.02)
        if meter.usd + headroom > args.cap:
            print(f"  [cap] stopping before brief {i} — ${meter.usd:.4f} spent")
            break

        print(f"[{i}] {brief['brief']}")
        chosen = select.select(brief["brief"], judge=judge_mod.judge)
        print(f"    routed to {chosen.agent} via {chosen.layer}"
              + (f" (margin {chosen.margin:.3f})" if chosen.margin else ""))

        started = time.perf_counter()
        result = turn.execute(
            [brief],
            subagent.make_runner(db, session_id),
            judge=judge_mod.judge,
            deadline_seconds=40.0,
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        for s in result.steps:
            keys = sorted(k for k in s.findings if not k.startswith("_"))
            print(f"    {s.agent}: {s.status}  {elapsed}ms  findings={keys}")
            if s.findings.get("_note"):
                print(f"      note: {s.findings['_note'][:100]}")
            if s.detail:
                print(f"      detail: {s.detail[:100]}")
            results.append({
                "brief": brief["brief"], "agent": s.agent, "status": s.status,
                "findings": {k: v for k, v in s.findings.items()},
                "layer": s.layer, "latency_ms": elapsed,
            })
        print(f"    running ${meter.usd:.4f}\n")

    ok = sum(1 for r in results if r["status"] == "ok")
    print("=" * 62)
    print(f"  steps {len(results)}   ok {ok}   model calls {meter.calls}")
    print(f"  SPENT ${meter.usd:.4f} of ${args.cap:.2f} cap")
    print("=" * 62)

    out = "evals/subagent_smoke_out.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"model": config.CLAUDE_MODEL, "spent_usd": round(meter.usd, 5),
                   "calls": meter.calls, "results": results}, fh, indent=2, default=str)
    print(f"  written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
