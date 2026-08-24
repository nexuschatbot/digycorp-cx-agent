"""Live accuracy check for the router's classifier. NOT a test — run it on purpose.

This is the one thing the offline suite cannot answer: whether Haiku actually
classifies real customer messages correctly. Everything around it — the
pre-filter, the sticky gate, the thresholds — is tested offline in
tests/test_router.py with canned classifications.

It lives OUTSIDE pytest deliberately. tests/conftest.py blocks outbound sockets
for every test in the suite, and that guard should stay absolute: a suite that
sometimes calls a vendor is a suite nobody trusts to run.

    python -m evals.routing_live            # all 32 cases
    python -m evals.routing_live --model claude-haiku-4-5

Costs cents. Prints a per-case table, the accuracy against the doc's >=92% target,
and every miss with the confidence the model gave it — a confident miss is a
prompt problem, a low-confidence miss is a threshold problem, and they are fixed
in different places.
"""

from __future__ import annotations

import argparse
import sys
import time

from app import config
from app.agent.router import prefilter, route, sticky
from evals.routing_cases import CASES

TARGET_ACCURACY = 0.92
TARGET_FALSE_SWITCH = 0.05


def settled_by_rules(case) -> str | None:
    return prefilter.decide(case.message) or sticky.decide(
        case.message, case.current_agent
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=config.CLAUDE_MODEL)
    parser.add_argument("--only-model-cases", action="store_true",
                        help="skip cases the rules settle for free")
    args = parser.parse_args()

    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set — nothing to call.", file=sys.stderr)
        return 2

    print(f"model: {args.model}\ncases: {len(CASES)}\n")
    print(f"{'':2} {'expected':<18} {'got':<18} {'conf':>5} {'via':<10} message")
    print("-" * 100)

    hits = misses = model_calls = false_switches = 0
    latencies: list[float] = []
    failures = []

    for case in CASES:
        rules = settled_by_rules(case)
        if args.only_model_cases and rules:
            continue

        started = time.perf_counter()
        result = route(
            case.message,
            current_agent=case.current_agent,
            session_facts=case.session_facts,
            live_bookings=list(case.live_bookings),
            model=args.model,
        )
        elapsed = (time.perf_counter() - started) * 1000

        via = "rules" if rules else "haiku"
        if not rules:
            model_calls += 1
            latencies.append(elapsed)

        ok = result.agent == case.expect
        hits += ok
        misses += not ok

        # A false switch: we moved off the active agent when we should not have.
        if (
            case.current_agent
            and case.expect == case.current_agent
            and result.agent != case.current_agent
        ):
            false_switches += 1

        conf = result.decision.confidence if result.decision else 1.0
        print(f"{'ok' if ok else 'XX':2} {case.expect:<18} {result.agent:<18} "
              f"{conf:>5.2f} {via:<10} {case.message[:44]}")
        if not ok:
            failures.append((case, result, conf))

    total = hits + misses
    accuracy = hits / total if total else 0.0
    switch_rate = false_switches / total if total else 0.0

    print("-" * 100)
    print(f"accuracy       {hits}/{total} = {accuracy:.1%}   (target >= {TARGET_ACCURACY:.0%})")
    print(f"false switches {false_switches}/{total} = {switch_rate:.1%}   "
          f"(target < {TARGET_FALSE_SWITCH:.0%})")
    print(f"model calls    {model_calls}/{total}  "
          f"({(total - model_calls) / total:.0%} settled free by rules)")
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        print(f"classifier     p50 {p50:.0f}ms   p95 {p95:.0f}ms")

    if failures:
        print("\nMISSES")
        for case, result, conf in failures:
            kind = "CONFIDENT miss (prompt problem)" if conf >= 0.85 else \
                   "low-confidence miss (threshold problem)"
            print(f"  {case.message!r}")
            print(f"    expected {case.expect}, got {result.agent} at {conf:.2f} — {kind}")
            print(f"    reason: {result.reason}")
            if case.note:
                print(f"    note:   {case.note}")

    return 0 if accuracy >= TARGET_ACCURACY and switch_rate < TARGET_FALSE_SWITCH else 1


if __name__ == "__main__":
    raise SystemExit(main())
