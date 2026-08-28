"""T0.1 — capture what TODAY's Aisha does, before the rearchitecture changes her.

The one measurement that is only available now. After Phase 5 there is no way to
ask "did this get better or worse?" except against a recording made beforehand,
and the test suite cannot answer it: it proves nothing BROKE, and says nothing
about warmth, Arabic register, or the one-question discipline. Those are the
things a rearchitecture actually risks.

    python -m evals.baseline              # DRY RUN — prints the script, spends nothing
    python -m evals.baseline --live       # spends real money, capped
    python -m evals.baseline --live --cap 2.00 --out evals/baseline_out

DRY RUN IS THE DEFAULT, deliberately. This is the sibling of evals/routing_live
— outside pytest because tests/conftest.py blocks outbound sockets and that guard
should stay absolute (a suite that sometimes calls a vendor is a suite nobody
trusts). But routing_live spends cents on 32 one-shot classifications; this drives
twelve multi-turn conversations through Sonnet, which is a different order of
commitment. So the spending mode is opt-in, not opt-out.

THE CAP IS LIVE, NOT AN ESTIMATE. A budget you check afterwards is not a budget.
Spend is metered per model CALL off the usage the graph already logs, and the run
STOPS mid-conversation the moment the next call could cross the line. A truncated
run with an honest number beats a complete run with a surprise.

WHY THE METER READS A LOG LINE. graph._stream_once calls _log_usage(final, ...)
and returns only the assistant message — usage never reaches the caller, and the
`generate` injection seam therefore cannot see it. Rather than change production
code to serve a measurement script, this attaches a handler to the logger the
graph already writes to and reads the record's positional args. If that format
ever changes, the calls arrive UNMETERED and are charged at the worst rate seen
so far (see _Meter.unmetered) — the cap degrades toward caution, never toward
silence.

ENGLISH ONLY, by decision. Arabic doubles the run for a register question that a
side-by-side read of the English transcripts will not answer either way; the
budget buys more value spent on covering more journeys.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import config
from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.db import Base
from app.models import FuelType, Showroom, VehicleModel
from app.services import slots as slots_service

SEED = Path(__file__).resolve().parent.parent / "seed"

# --------------------------------------------------------------------------- #
# Pricing — AN ASSUMPTION, stated rather than buried.
#
# These are list prices per MILLION tokens as I understand them, NOT read from
# the account. Every number this script prints inherits their accuracy, which is
# why they are overridable from the command line and echoed in the report. Check
# them against the console before trusting a total; the CAP still holds either
# way, because the cap is applied to whatever these produce.
#
# The multipliers are the prompt-caching contract: a 1-hour cache WRITE bills at
# 2x base, a cache READ at 0.1x. graph._system_blocks uses ttl="1h" on the system
# prefix, so 2x is the right write multiplier here.
# --------------------------------------------------------------------------- #
PRICE_IN_PER_M = 3.00
PRICE_OUT_PER_M = 15.00
CACHE_WRITE_MULT = 2.0
CACHE_READ_MULT = 0.1

# Charged for a model call whose usage we could not read, before any call has
# been metered. Deliberately pessimistic: the whole point of the unmetered path
# is that it must never let the cap drift upward.
UNMETERED_FLOOR_USD = 0.05


# --------------------------------------------------------------------------- #
# The meter
# --------------------------------------------------------------------------- #
@dataclass
class _Call:
    uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    usd: float = 0.0
    metered: bool = True


class _Meter(logging.Handler):
    """Accumulates spend by listening to `app.agent.graph`'s own usage line.

    Attached as a log handler rather than wrapping `generate`, because usage is
    logged inside the streaming call and never returned — see the module
    docstring. `record.args` is read POSITIONALLY against _log_usage's call:

        (session_id, prompt_total, uncached, cache_write, cache_read,
         output, streamed, stop_reason)

    A record that does not match that shape is not guessed at. It is counted as
    unmetered and charged at the worst call seen so far, so a format change makes
    the run end EARLY rather than quietly overspend.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.calls: list[_Call] = []
        self.unmetered = 0

    # -- accounting ------------------------------------------------------- #
    @staticmethod
    def price(uncached: int, cache_write: int, cache_read: int, output: int) -> float:
        effective_in = (
            uncached
            + cache_write * CACHE_WRITE_MULT
            + cache_read * CACHE_READ_MULT
        )
        return (
            effective_in * PRICE_IN_PER_M / 1_000_000
            + output * PRICE_OUT_PER_M / 1_000_000
        )

    @property
    def worst_call_usd(self) -> float:
        """The most expensive call seen. Used ONLY to price an unmetered call."""
        metered = [c.usd for c in self.calls if c.metered]
        return max(metered) if metered else UNMETERED_FLOOR_USD

    @property
    def typical_call_usd(self) -> float:
        """p90 of metered calls — what to reserve headroom against.

        NOT the max, which was the first version and stopped a run with 38% of
        the budget unspent. A cold cache write costs ~16x a warm read (measured:
        $0.188 vs $0.012), and it happens once or twice per run. Reserving
        against that outlier makes the guard permanently more cautious the moment
        it sees one, so the cap stops protecting the budget and starts eating it.

        p90 still absorbs a genuinely expensive turn; it just refuses to treat
        the single worst call in the run as the expected next one.
        """
        metered = sorted(c.usd for c in self.calls if c.metered)
        if not metered:
            return UNMETERED_FLOOR_USD
        return metered[min(len(metered) - 1, int(round(0.90 * (len(metered) - 1))))]

    @property
    def spent(self) -> float:
        return sum(c.usd for c in self.calls)

    def note_unmetered(self) -> None:
        """A model call happened that we could not read. Charge the worst case."""
        self.unmetered += 1
        self.calls.append(_Call(usd=self.worst_call_usd, metered=False))

    # -- logging.Handler --------------------------------------------------- #
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not str(record.msg).startswith("agent usage"):
                return
            args = record.args or ()
            if not isinstance(args, tuple) or len(args) < 6:
                self.note_unmetered()
                return
            _sid, _total, uncached, cache_write, cache_read, output = args[:6]
            call = _Call(
                uncached=int(uncached),
                cache_write=int(cache_write),
                cache_read=int(cache_read),
                output=int(output),
            )
            call.usd = self.price(call.uncached, call.cache_write, call.cache_read, call.output)
            self.calls.append(call)
        except Exception:  # noqa: BLE001 — a broken meter must stop the run, not crash it
            self.note_unmetered()


# --------------------------------------------------------------------------- #
# The twelve conversations
#
# Front-loaded and OPEN, following scripts/scripted_booking.run_live's note: a
# fixed user script cannot track a dynamic agent, so over-constraining it just
# starves the agent of inventory and measures the script instead of the agent.
# Each conversation owns its session_id, so each is its own checkpointer thread.
# --------------------------------------------------------------------------- #
@dataclass
class Conversation:
    key: str
    what: str
    turns: list[str]
    device_token: str | None = None
    note: str = ""


MOBILE = "+971509998877"

CONVERSATIONS: list[Conversation] = [
    Conversation(
        "discovery", "Open-ended browse — rule 5's 'answer first, then narrow'",
        [
            "Hi! I'm looking for a family SUV but I'm not sure which one yet.",
            "There are five of us, and I do about 50km a day commuting.",
            "What's the difference between the two you mentioned?",
            "Thanks, that's helpful. I'll think about it.",
        ],
    ),
    Conversation(
        "booking_full", "The whole journey — discovery through confirmed booking",
        [
            f"Hi! I'd like to book a test drive of the Jetour T2. My name is Test Driver, mobile {MOBILE}.",
            "The petrol one, please.",
            "A showroom is fine — any branch, whatever slot is earliest.",
            "That works for me, let's go with it.",
            "Just me, nobody else coming.",
            "Yes, that all looks correct.",
            "Yes, I'll bring my licence. And yes, WhatsApp confirmations are fine.",
            "Perfect, thank you!",
        ],
        note=(
            "EIGHT turns, not five. Measured: her flow is variant -> showroom/doorstep "
            "-> lock -> passengers -> recap -> licence -> consent -> create. The first "
            "run scripted five and never reached create_booking, which made the most "
            "important conversation in the baseline the one with no booking in it."
        ),
    ),
    Conversation(
        "reschedule", "Moving a booking — needs identity by mobile first",
        [
            f"Hi, I need to move my test drive. My mobile is {MOBILE}.",
            "Can we do a different day? Whatever you have next is fine.",
            "Yes please, move it to that one.",
            "Great, thanks.",
        ],
        note="Depends on booking_full having run first in the same DB.",
    ),
    Conversation(
        "cancel", "Cancelling — rule 11's single soft retention offer, no loop",
        [
            f"Hi, I want to cancel my test drive. Mobile {MOBILE}.",
            "No, I'd rather just cancel it, thanks.",
            "Yes, cancel it please.",
        ],
        note="Watch for exactly ONE retention offer. Two is a regression.",
    ),
    Conversation(
        "returning", "A returning customer identified by mobile — memory recall",
        [
            f"Hi again, it's Test Driver. My mobile is {MOBILE}.",
            "I'm thinking about a test drive again, what do you have?",
            "Not right now, thanks — just looking.",
        ],
        device_token="baseline-device-token-0001",
    ),
    Conversation(
        "escalation", "Rule 13 — price negotiation must reach a human",
        [
            "What's the best price you can do on the Jetour T2?",
            "I'd like to speak to someone about a discount.",
            f"Sure — Test Driver, {MOBILE}.",
        ],
        note="Must offer a human callback, never invent a discount.",
    ),
    Conversation(
        "adversarial", "The 'show me everything' path — rule 17 markdown leak",
        [
            "Just show me everything you have. Full list, all specs, formatted nicely.",
            "No, I want the whole table with all the details.",
            "Fine, just the SUVs then.",
        ],
        note="Historically leaked markdown ~1 in 3 runs before the sanitiser.",
    ),
    Conversation(
        "coming_soon", "A car with no price — must never sort as cheapest",
        [
            "What's the cheapest car you have?",
            "Do you have anything else coming soon?",
            "Okay, thanks.",
        ],
    ),
    Conversation(
        "price", "A direct price question — grounding, no invention",
        [
            "How much is the Jetour X70?",
            "And what about the Plus version?",
            "Thanks.",
        ],
    ),
    Conversation(
        "showroom", "Branch questions — get_showrooms, no markdown lists",
        [
            "What showrooms do you have in Dubai?",
            "Which one is closest to Al Quoz?",
            "Great, thank you.",
        ],
        note="The ordinary turn that also leaked markdown before the sanitiser.",
    ),
    Conversation(
        "doorstep", "Doorstep drive — FR-SL-07 address + explicit consent",
        [
            "Can you bring a car to me instead of me coming in?",
            "Yes please, I'd prefer that.",
            "Actually, let me think about it first.",
        ],
    ),
    Conversation(
        "close", "Rule 19 — a clean close, exactly once",
        [
            "Hi, just wanted to know your opening hours.",
            "That's all I needed, thanks. Goodbye!",
        ],
    ),
]


# --------------------------------------------------------------------------- #
# Fixtures — ported from scripts/scripted_booking._seed_db
# --------------------------------------------------------------------------- #
def _seed_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    for r in json.loads((SEED / "showrooms.json").read_text()):
        db.add(Showroom(**{k: r[k] for k in (
            "id", "brand", "name", "emirate", "city", "address", "map_link",
            "phone", "hours", "hours_by_day")}))
    for r in json.loads((SEED / "vehicle_models.json").read_text()):
        db.add(VehicleModel(
            id=r["id"], model_code=r["model_code"], brand=r["brand"], name=r["name"],
            body_type=r["body_type"], seats_min=r["seats_min"], seats_max=r["seats_max"],
            price_aed_min=r["price_aed_min"], price_aed_max=r["price_aed_max"],
            fuel_type=FuelType(r["fuel_type"]), headline_features=r["headline_features"],
            showroom_ids=r["showroom_ids"], data_provenance=r["data_provenance"],
            images=r["images"]))
    db.commit()
    slots_service.generate(db, days=14, prebooked=0.0,
                           start_day=date.today() + timedelta(days=1))
    return db


# --------------------------------------------------------------------------- #
# Running one conversation
# --------------------------------------------------------------------------- #
def _reply_text(messages: list, since: int) -> str:
    """Every word Aisha SAID this turn, across all her assistant messages.

    NOT `messages[-1]` — that was the first version and it was wrong. A turn that
    ends in a tool call (offer_options and close_conversation both END the turn by
    design) leaves a tool_result as the last message, so reading the tail returns
    an empty string and the transcript silently records Aisha saying nothing. That
    is the single most damaging failure this script could have: it would have run
    to completion, cost real money, and produced a baseline of blank replies.

    Pre-tool text is DELIVERED, not retracted (graph.py seals it into its own
    bubble via text_break), so joining every text block across the turn's
    assistant messages is exactly what the customer read, in order.
    """
    out: list[str] = []
    for m in messages[since:]:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            if content:
                out.append(str(content))
            continue
        out.extend(
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    return "\n".join(t.strip() for t in out if t.strip()).strip()


def _tools_of(messages: list, already: int) -> tuple[list[str], int]:
    names = [
        b["name"]
        for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    return names[already:], len(names)


def run_conversation(
    db: Session, convo: Conversation, meter: _Meter, cap: float
) -> dict:
    """Drive one conversation turn by turn. Returns the record; may truncate."""
    graph = build_graph(new_sqlite_checkpointer())
    session_id = f"baseline-{convo.key}"
    record: dict = {
        "key": convo.key, "what": convo.what, "note": convo.note,
        "session_id": session_id, "turns": [], "truncated": False,
    }
    seen_tools = 0
    seen_messages = 0  # how much of the thread we have already transcribed

    for i, user_turn in enumerate(convo.turns, 1):
        # THE CAP, CHECKED BEFORE THE SPEND, NOT AFTER. Headroom is the worst
        # single call seen so far times a small multiple, because one TURN is
        # several calls (search, then answer). Stopping one turn early with
        # budget unspent is the correct failure; stopping one turn late is not.
        headroom = meter.typical_call_usd * 4
        if meter.spent + headroom > cap:
            record["truncated"] = True
            record["truncated_at_turn"] = i
            print(f"    \033[31m[cap]\033[0m stopping before turn {i} — "
                  f"${meter.spent:.3f} spent, ${headroom:.3f} headroom needed, cap ${cap:.2f}")
            break

        before_calls = len(meter.calls)
        started = time.perf_counter()
        print(f"  \033[36m[user]\033[0m {user_turn}")
        try:
            state = graph.invoke(
                {"messages": [{"role": "user", "content": user_turn}], "iterations": 0},
                build_config(db, session_id, device_token=convo.device_token),
            )
        except Exception as exc:  # noqa: BLE001 — one bad turn must not lose the run
            print(f"    \033[31m[error]\033[0m {type(exc).__name__}: {exc}")
            record["turns"].append({"n": i, "user": user_turn, "error": repr(exc)})
            break
        elapsed_ms = (time.perf_counter() - started) * 1000

        reply = _reply_text(state["messages"], seen_messages)
        seen_messages = len(state["messages"])
        tools, seen_tools = _tools_of(state["messages"], seen_tools)
        calls = meter.calls[before_calls:]
        turn_usd = sum(c.usd for c in calls)

        print(f"  \033[32m[aisha]\033[0m {reply[:200]}{'…' if len(reply) > 200 else ''}")
        if tools:
            print(f"    \033[33m[tools]\033[0m {', '.join(tools)}")
        print(f"    \033[90m[cost]\033[0m {len(calls)} calls · "
              f"${turn_usd:.4f} · {elapsed_ms:.0f}ms · running ${meter.spent:.3f}")

        record["turns"].append({
            "n": i,
            "user": user_turn,
            "aisha": reply,
            "tools": tools,
            "latency_ms": round(elapsed_ms),
            "model_calls": len(calls),
            "usd": round(turn_usd, 5),
            "tokens": {
                "uncached": sum(c.uncached for c in calls),
                "cache_write": sum(c.cache_write for c in calls),
                "cache_read": sum(c.cache_read for c in calls),
                "output": sum(c.output for c in calls),
            },
            # PER CALL, not just the turn total. A turn is several calls and they
            # do not cost the same: the first pays cache WRITES at 2x, the rest
            # read at 0.1x. Summing them hides which is which, and "why did that
            # turn cost 15x the last one" is exactly the question this file
            # exists to answer.
            "calls": [
                {"uncached": c.uncached, "cache_write": c.cache_write,
                 "cache_read": c.cache_read, "output": c.output,
                 "usd": round(c.usd, 5), "metered": c.metered}
                for c in calls
            ],
        })
    return record


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _dry_run() -> int:
    total_turns = sum(len(c.turns) for c in CONVERSATIONS)
    print("\n\033[1mDRY RUN — nothing was sent, nothing was spent.\033[0m\n")
    print(f"{len(CONVERSATIONS)} conversations, {total_turns} user turns, English only.\n")
    for c in CONVERSATIONS:
        print(f"  \033[1m{c.key}\033[0m — {c.what}")
        for t in c.turns:
            print(f"      · {t}")
        if c.note:
            print(f"      \033[90m{c.note}\033[0m")
        print()
    print("Pricing assumption (override with --price-in / --price-out):")
    print(f"  input  ${PRICE_IN_PER_M:.2f}/M   output ${PRICE_OUT_PER_M:.2f}/M")
    print(f"  cache write x{CACHE_WRITE_MULT}   cache read x{CACHE_READ_MULT}")
    print("\nRun it for real with:  python -m evals.baseline --live\n")
    return 0


def _report(records: list[dict], meter: _Meter, cap: float, out: Path) -> int:
    turns = [t for r in records for t in r["turns"] if "aisha" in t]
    lat = sorted(t["latency_ms"] for t in turns)
    truncated = [r["key"] for r in records if r["truncated"]]

    print("\n" + "=" * 72)
    print("  BASELINE CAPTURED")
    print("=" * 72)
    print(f"  conversations   {len(records)}")
    print(f"  turns           {len(turns)}")
    print(f"  model calls     {len(meter.calls)}"
          + (f"  \033[31m({meter.unmetered} UNMETERED)\033[0m" if meter.unmetered else ""))
    if lat:
        # Clamped, because int(n*0.95)-1 goes NEGATIVE for small n and silently
        # reports the fastest turn as p95 — which is how a latency regression
        # hides in plain sight on a short run.
        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, max(0, int(round(p * (len(lat) - 1)))))]
        print(f"  latency         p50 {pct(.50):.0f}ms   "
              f"p95 {pct(.95):.0f}ms   max {lat[-1]:.0f}ms")
    tok = {k: sum(t["tokens"][k] for t in turns)
           for k in ("uncached", "cache_write", "cache_read", "output")}
    print(f"  tokens in       uncached {tok['uncached']:,}  "
          f"cache_write {tok['cache_write']:,}  cache_read {tok['cache_read']:,}")
    print(f"  tokens out      {tok['output']:,}")
    if tok["cache_read"] == 0 and len(meter.calls) > 2:
        print("  \033[31m  cache_read is 0 across the whole run — prompt caching is NOT working.\033[0m")
    print(f"\n  \033[1mSPENT  ${meter.spent:.3f}\033[0m   of ${cap:.2f} cap")
    if truncated:
        print(f"  \033[33mtruncated: {', '.join(truncated)} — cap reached\033[0m")
    print(f"\n  written to  {out}")
    print("\n  Pricing is the assumption stated at the top of this file, not a")
    print("  reading from the account. Check the console before quoting the total.\n")
    return 0


def main() -> int:
    # Declared before first use: --price-in/--price-out default to these, and
    # Python requires the declaration to precede any reference in the function.
    global PRICE_IN_PER_M, PRICE_OUT_PER_M

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="actually call the API and spend money")
    ap.add_argument("--cap", type=float, default=2.00, help="hard USD ceiling (default 2.00)")
    ap.add_argument("--out", default="evals/baseline_out", help="output directory")
    ap.add_argument("--price-in", type=float, default=PRICE_IN_PER_M)
    ap.add_argument("--price-out", type=float, default=PRICE_OUT_PER_M)
    ap.add_argument("--only", default="", help="comma-separated conversation keys")
    args = ap.parse_args()
    PRICE_IN_PER_M, PRICE_OUT_PER_M = args.price_in, args.price_out

    if not args.live:
        return _dry_run()
    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set — nothing to call.", file=sys.stderr)
        return 2

    chosen = CONVERSATIONS
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        chosen = [c for c in CONVERSATIONS if c.key in keys]

    meter = _Meter()
    logging.getLogger("app.agent.graph").addHandler(meter)
    logging.getLogger("app.agent.graph").setLevel(logging.INFO)

    db = _seed_db()
    print(f"\nmodel: {config.AGENT_MODEL}   cap: ${args.cap:.2f}   "
          f"conversations: {len(chosen)}\n")

    records = []
    for convo in chosen:
        print(f"\033[1m--- {convo.key} \033[0m\033[90m{convo.what}\033[0m")
        records.append(run_conversation(db, convo, meter, args.cap))
        print()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"baseline-{stamp}.json"
    out.write_text(json.dumps({
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "model": config.AGENT_MODEL,
        "agent_effort": config.AGENT_EFFORT,
        "router_enabled": config.ROUTER_ENABLED,
        "pricing_assumption": {
            "in_per_m": PRICE_IN_PER_M, "out_per_m": PRICE_OUT_PER_M,
            "cache_write_mult": CACHE_WRITE_MULT, "cache_read_mult": CACHE_READ_MULT,
        },
        "cap_usd": args.cap,
        "spent_usd": round(meter.spent, 5),
        "unmetered_calls": meter.unmetered,
        "conversations": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    return _report(records, meter, args.cap, out)


if __name__ == "__main__":
    raise SystemExit(main())
