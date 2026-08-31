"""Stage 1 visible proof — a Jetour T2 booked end to end through the LangGraph loop.

Two modes, same graph:

  python -m scripts.scripted_booking          # DETERMINISTIC — scripted model,
                                               # no ANTHROPIC_API_KEY needed
  python -m scripts.scripted_booking --live    # LIVE — the real agent node
                                               # (client.messages.stream, AD-A),
                                               # driven by a hardcoded USER-turn
                                               # list; needs ANTHROPIC_API_KEY

Both build an in-memory DB seeded from the real seed/*.json fixtures with a fresh
generated slot window, run the booking, and print the booking reference plus the
audit trail grouped by correlation id — so you can see, for one create_booking,
the choke-point `tool_call` and the service `intent`/`notification` rows carrying
the SAME id. This is the graph + trusted-context injection proven before any SSE.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.db import Base
from app.models import (
    AuditEvent,
    Booking,
    FuelType,
    LocationType,
    Showroom,
    Slot,
    SlotStatus,
    VehicleModel,
)
from app.services import slots as slots_service

SEED = Path(__file__).resolve().parent.parent / "seed"
T2 = 2  # Jetour T2
MOBILE = "+971509998877"
SESSION = "sess-demo-t2"


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
            showroom_ids=r["showroom_ids"], data_provenance=r["data_provenance"], images=r["images"]))
    db.commit()
    slots_service.generate(db, days=7, prebooked=0.0, start_day=date.today() + timedelta(days=1))
    return db


def _earliest_t2_showroom_slot(db: Session) -> Slot:
    return db.scalars(
        select(Slot).where(
            Slot.status == SlotStatus.open,
            Slot.location_type == LocationType.showroom,
            Slot.vehicle_id == T2,
        ).order_by(Slot.start, Slot.id)
    ).first()


# --------------------------------------------------------------------------- #
# Deterministic scripted model (no API key)
# --------------------------------------------------------------------------- #
class ScriptedModel:
    def __init__(self, steps: list[dict]) -> None:
        self.steps, self.i = steps, 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        msg = self.steps[self.i]
        self.i += 1
        return msg


def _tu(tid: str, tool: str, /, **inp) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}


def _txt(s: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": s}]}


def run_scripted(db: Session) -> dict:
    slot = _earliest_t2_showroom_slot(db)
    steps = [
        _tu("u1", "search_vehicles", name="Jetour T2"),
        _tu("u2", "get_available_slots", vehicle_id=T2, location_type="showroom"),
        _tu("u3", "lock_slot", slot_id=slot.id),
        _tu("u4", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _tu("u5", "record_consent", messaging=True),          # customer_id injected
        _tu("u6", "create_booking", vehicle_id=T2, slot_id=slot.id),  # customer_id injected
        _txt("You're all set — your Jetour T2 test drive is booked."),
    ]
    graph = build_graph(new_sqlite_checkpointer())
    config = build_config(db, SESSION, generate=ScriptedModel(steps))
    return graph.invoke(
        {"messages": [{"role": "user", "content": "Details on the Jetour T2, and I'd like a test drive."}],
         "iterations": 0, "customer_id": None},
        config,
    )


def run_live(db: Session) -> dict:
    """Real agent node (AD-A streaming), driven by hardcoded USER turns.

    Each user turn is a fresh graph.invoke on the SAME thread_id — the
    checkpointer carries the full tool_use/tool_result history between turns, so
    the model keeps its context exactly as a live chat would.
    """
    graph = build_graph(new_sqlite_checkpointer())
    # Front-loaded, responsive turns: a fixed user script can't perfectly track a
    # dynamic agent, so keep preferences OPEN (any showroom, earliest slot) — the
    # agent is correctly grounded and will only offer slots that really exist, so
    # over-constraining a fixed script just starves it of inventory.
    user_turns = [
        f"Hi! I'd like to book a test drive of the Jetour T2 petrol. My name is Test Driver, mobile {MOBILE}.",
        "Any showroom is fine — please just take the earliest available slot, whatever it is.",
        "That slot is perfect, let's go with it. It's just me, nothing special needed.",
        "Yes, WhatsApp reminders are fine. Please go ahead and confirm the booking.",
        "Perfect, thank you!",
    ]
    final = None
    seen_tool_events = 0
    for turn in user_turns:
        print(f"\n\033[36m[user]\033[0m {turn}")
        final = graph.invoke(
            {"messages": [{"role": "user", "content": turn}], "iterations": 0},
            build_config(db, SESSION),
        )
        # Show the tools the agent invoked this turn + a short result summary —
        # the same tool/input/output the Stage-2 SSE `tool_event` frame will carry.
        seen_tool_events = _print_tool_trace(final["messages"], seen_tool_events)
        last = final["messages"][-1]
        text = " ".join(b["text"] for b in last.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        if text:
            print(f"\033[32m[aisha]\033[0m {text}")
    return final


def _print_tool_trace(messages: list, already: int) -> int:
    """Print tool_use -> tool_result pairs not yet shown (returns new count)."""
    pairs: list[tuple[dict, str]] = []
    results: dict[str, str] = {}
    for m in messages:
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                results[b.get("tool_use_id", "")] = b.get("content", "")
    for m in messages:
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                pairs.append((b, results.get(b.get("id", ""), "")))
    for block, raw in pairs[already:]:
        out = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "slots" in parsed:
                out = f"{{slots: {len(parsed['slots'])} returned}}"
            elif isinstance(parsed, dict):
                out = json.dumps({k: parsed[k] for k in list(parsed)[:4]})
        except Exception:
            pass
        print(f"    \033[33m[tool]\033[0m {block['name']}({json.dumps(block.get('input', {}))}) -> {out[:120]}")
    return len(pairs)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _report(db: Session) -> int:
    booking = db.scalars(select(Booking).order_by(Booking.id.desc())).first()
    if booking is None:
        print("\n\033[31mNO BOOKING CREATED — the run did not reach create_booking.\033[0m")
        return 1

    print("\n" + "=" * 70)
    print(f"  BOOKING: {booking.ref}   status={booking.status.value}   "
          f"customer_id={booking.customer_id}   vehicle_id={booking.vehicle_id}")
    print("=" * 70)

    events = list(db.scalars(select(AuditEvent).where(AuditEvent.session == SESSION).order_by(AuditEvent.id)))
    groups: dict[str, list[AuditEvent]] = defaultdict(list)
    loose: list[AuditEvent] = []
    for e in events:
        cid = e.payload.get("correlation_id")
        (groups[cid] if cid else loose).append(e)

    print(f"\n  AUDIT TRAIL — {len(events)} events, grouped by correlation id:\n")
    for cid, evs in groups.items():
        facets = ", ".join(f"{e.type.value}" + (f'({e.payload.get("tool") or e.payload.get("intent") or e.payload.get("kind") or e.payload.get("axis")})') for e in evs)
        marker = "  <-- one write, two+ facets" if len(evs) > 1 else ""
        print(f"    {cid[:12]}…  {facets}{marker}")
    for e in loose:
        print(f"    (no cid)      {e.type.value}")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="use the real Anthropic agent node (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args()

    db = _seed_db()
    print(f"Seeded in-memory DB. Mode: {'LIVE (real model)' if args.live else 'DETERMINISTIC (scripted model)'}")
    if args.live:
        run_live(db)
    else:
        run_scripted(db)
    return _report(db)


if __name__ == "__main__":
    sys.exit(main())
