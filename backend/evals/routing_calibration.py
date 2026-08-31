"""T3.6: calibrate agent selection. Offline — local embeddings only, NO API.

    python evals/routing_calibration.py

WHY THIS IS COMMITTED RATHER THAN THROWN AWAY. app/orchestrator/select.py's
MIN_MARGIN is 0.02 because THIS SCRIPT SAID SO — measured, not chosen. A
threshold whose evidence has been deleted is indistinguishable from a guess, and
the plan's original 0.80 was exactly that.

WHAT IT MEASURED (2026-08-28, multilingual-e5-small int8, 32 card utterances):

    domain only            75.0%   <- best representation
    utterances only        71.9%
    domain + utterances    65.6%   <- adding utterances makes it WORSE
    concatenated           68.8%

    absolute >= 0.80  routes 56% of traffic, 83% of those correct
    margin   >= 0.02  routes 22% of traffic, 100% of those correct

Sharpening the card domains afterwards moved overall accuracy to 71.9% but
raised the confident fast lane to ~37% coverage at 100% — which is the number
that matters, because only the confident lane routes without the judge.

RE-RUN IT WHEN REAL BRIEFS EXIST. These 32 utterances are short fragments Aisha
would never write; her briefs are fuller sentences and may separate better.
turn_trace.steps records the live scores for exactly this purpose.


PROTOCOL: LEAVE-ONE-OUT. Each utterance is scored against card representations
built WITHOUT it, so nothing can match itself. That measures generalisation
rather than memorisation, which is the only thing a threshold can be set on.
"""
import numpy as np
import yaml
from pathlib import Path

from app.retrieval import embed

CARDS = {}
for agent in ("sales", "testdrive", "finance"):
    fm = yaml.safe_load(
        Path(f"app/agents/{agent}/v1.md").read_text().split("---")[1]
    )
    CARDS[agent] = {"domain": fm["domain"].strip(), "utterances": fm["utterances"]}

AGENTS = list(CARDS)


def represent(mode, agent, exclude=None):
    """Text(s) representing one card under a given strategy."""
    c = CARDS[agent]
    utts = [u for u in c["utterances"] if u != exclude]
    if mode == "domain":
        return [c["domain"]]
    if mode == "utterances":
        return utts
    if mode == "domain+utterances":
        return [c["domain"]] + utts
    if mode == "concat":
        return [c["domain"] + " " + " ".join(utts)]
    raise ValueError(mode)


def score(mode, qv, agent, exclude):
    texts = represent(mode, agent, exclude)
    V = embed.embed_passages(texts)
    return float(np.max(np.asarray(V) @ np.asarray(qv)))


def evaluate(mode):
    rows = []
    for agent in AGENTS:
        for utt in CARDS[agent]["utterances"]:
            qv = embed.embed_query(utt)
            scores = {a: score(mode, qv, a, exclude=utt) for a in AGENTS}
            best = max(scores, key=scores.get)
            ordered = sorted(scores.values(), reverse=True)
            rows.append({
                "agent": agent, "utt": utt, "correct": best == agent,
                "top": ordered[0], "second": ordered[1],
                "margin": ordered[0] - ordered[1],
                "correct_score": scores[agent],
            })
    return rows


print(f"{sum(len(c['utterances']) for c in CARDS.values())} utterances, "
      f"{len(AGENTS)} cards, leave-one-out\n")

results = {}
for mode in ("domain", "utterances", "domain+utterances", "concat"):
    rows = evaluate(mode)
    acc = sum(r["correct"] for r in rows) / len(rows)
    tops = [r["top"] for r in rows]
    margins = [r["margin"] for r in rows]
    ok_margins = [r["margin"] for r in rows if r["correct"]]
    bad_margins = [r["margin"] for r in rows if not r["correct"]]
    results[mode] = rows
    print(f"{mode:20} acc {acc:5.1%}  top-score range {min(tops):.3f}-{max(tops):.3f}"
          f"  margin mean {np.mean(margins):.4f}")
    if bad_margins:
        print(f"{'':20} correct-margin mean {np.mean(ok_margins):.4f} | "
              f"WRONG-margin mean {np.mean(bad_margins):.4f}")

best_mode = max(results, key=lambda m: sum(r["correct"] for r in results[m]))
print(f"\nbest representation: {best_mode}\n")

rows = results[best_mode]
print("Would an ABSOLUTE threshold work? (the plan's 0.80 placeholder)")
for t in (0.75, 0.80, 0.82, 0.85, 0.87):
    routed = [r for r in rows if r["top"] >= t]
    correct = [r for r in routed if r["correct"]]
    print(f"  >= {t:.2f}: routes {len(routed):2}/{len(rows)} "
          f"({len(routed)/len(rows):5.1%}), of which correct "
          f"{len(correct)/len(routed) if routed else 0:5.1%}")

print("\nWould a RELATIVE margin work? (gap between 1st and 2nd)")
for m in (0.0, 0.005, 0.01, 0.02, 0.03, 0.05):
    routed = [r for r in rows if r["margin"] >= m]
    correct = [r for r in routed if r["correct"]]
    print(f"  margin >= {m:.3f}: routes {len(routed):2}/{len(rows)} "
          f"({len(routed)/len(rows):5.1%}), of which correct "
          f"{len(correct)/len(routed) if routed else 0:5.1%}")

print("\nMisroutes under the best representation:")
for r in rows:
    if not r["correct"]:
        print(f"  {r['agent']:10} <- {r['utt'][:52]!r:56} margin {r['margin']:.4f}")
