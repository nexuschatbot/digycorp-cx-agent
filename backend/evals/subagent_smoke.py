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

# TURNS, NOT BRIEFS — and the difference is the whole point of the rewrite.
#
# The first version sent three INDEPENDENT briefs, one per agent, each in its
# own turn.execute with no `known`. TestDrive requires a vehicle_id and Finance
# requires a variant_id, and neither existed, so order.resolve correctly refused
# both: two of the three agents came back `blocked` having never run, and the
# output read as though the cascade had been exercised. It had not. The only
# agent that was ever proved is the one that requires nothing.
#
# A real turn is a CHAIN: Aisha asks for the car and the thing that depends on
# it in one emit_briefs, with depends_on wiring the second to the first. That is
# what these are, and it is the only shape that reaches TestDrive or Finance at
# all.
TURNS = [
    {
        "what": "sales -> finance (variant_id must thread)",
        "briefs": [
            {"brief": "a seven-seater SUV under AED 130,000, petrol"},
            {"brief": "the monthly instalment on that trim over 48 months",
             "depends_on": [0]},
        ],
    },
    {
        "what": "sales -> testdrive (vehicle_id must thread)",
        "briefs": [
            {"brief": "the Jetour X50 and which trims it comes in"},
            {"brief": "test drive slots for that car at the Deira branch on Saturday",
             "depends_on": [0]},
        ],
    },
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
        print("Dry run. Turns that would be sent:")
        for t in TURNS:
            print(f"  {t['what']}")
            for j, b in enumerate(t["briefs"]):
                dep = f"  (depends_on {b['depends_on']})" if b.get("depends_on") else ""
                print(f"    [{j}] {b['brief']}{dep}")
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

    for i, spec in enumerate(TURNS, 1):
        # Headroom for a whole CHAIN now, not one brief: the cap must stop us
        # before a turn we cannot afford to finish, or we pay for a sales step
        # and abandon the dependent one that was the point of running it.
        headroom = max(meter.per_call * 6 * len(spec["briefs"]), 0.04)
        if meter.usd + headroom > args.cap:
            print(f"  [cap] stopping before turn {i} — ${meter.usd:.4f} spent")
            break

        print(f"[{i}] {spec['what']}")
        for j, b in enumerate(spec["briefs"]):
            chosen = select.select(b["brief"], judge=judge_mod.judge)
            dep = f" after [{b['depends_on'][0]}]" if b.get("depends_on") else ""
            print(f"    [{j}] {b['brief'][:58]}{dep}")
            print(f"         -> {chosen.agent} via {chosen.layer}"
                  + (f" (margin {chosen.margin:.3f})" if chosen.margin else ""))

        started = time.perf_counter()
        result = turn.execute(
            spec["briefs"],
            subagent.make_runner(db, session_id),
            judge=judge_mod.judge,
            deadline_seconds=60.0,
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        print(f"    -- {elapsed}ms, entities threaded: {sorted(result.entities)}")
        for s in result.steps:
            keys = sorted(k for k in s.findings if not k.startswith("_"))
            print(f"    {s.agent}: {s.status}  findings={keys}")
            if s.findings.get("_note"):
                print(f"      note: {s.findings['_note'][:100]}")
            if s.detail:
                print(f"      detail: {s.detail[:100]}")
            results.append({
                "turn": spec["what"], "brief": s.brief, "agent": s.agent,
                "status": s.status,
                "findings": {k: v for k, v in s.findings.items()},
                "layer": s.layer,
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
