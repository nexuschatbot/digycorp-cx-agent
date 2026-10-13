"""Operator CLI. Inventory generation lives here and ONLY here.

    python -m app.cli reset-demo                       # <- demo morning, one command
    python -m app.cli generate-slots --days 14 --prebooked 0.18 [--reset] [--force]
    python -m app.cli top-up-slots [--days 14]
    python -m app.cli create-demo-booking [--mobile ...] [--doorstep]

DEMO MORNING IS ONE COMMAND: `reset-demo`. It does the whole ritual — schema,
seed fixtures, slot window, demo booking, AND clearing the agent checkpoint
store. The individual commands remain for targeted work between test runs, but
running them by hand is how the checkpoint step gets forgotten: checkpoints live
in their own sqlite file, so resetting the app DB alone leaves the agent
remembering conversations whose transcripts no longer exist.

Generation is deliberately not a tool: the agent reads availability, it never
creates inventory. Both this CLI and (later) the REST console call the same
service (AD-I).

create-demo-booking performs NO INSERT of its own. It goes through
find_or_create -> lock -> booking.create, so the demo booking passes every guard
a real one does: it holds a real lock, gets a generated TD-YYYY-NNNN, writes its
own notification rows, and lands in the audit trail. A hand-written INSERT would
produce a row that looks right and proves nothing — and would be the first thing
to diverge silently when a guard changes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from app.config import AGENT_CHECKPOINT_PATH
from app.db import Base, SessionLocal, engine
from app.models import LocationType, Slot, SlotStatus, VehicleModel

# Import models so every table is registered on Base.metadata before drop_all /
# the migration runs in reset-demo — otherwise drop_all would silently reset
# only a subset.
from app import migrate, models  # noqa: F401
from seed import (
    seed_accessories,
    seed_customers,
    seed_finance_banks,
    seed_memory_profiles,
    seed_showrooms,
    seed_stock,
    seed_vehicle_models,
)
from app.services import booking as booking_service
from app.services import customers as customers_service
from app.services import slots as slots_service

# Ravi Sharma, from seed/customers.json — the seeded returning customer with
# recorded consent, used by the reschedule/cancel journeys and by S-07's recall.
DEMO_MOBILE = "+971505550103"

# Pinned so reset-demo is repeatable. Ravi has memory consent, so his demo
# booking also drives the derived memory writer — leaving the car to "whichever
# slot happens to be earliest" would change what Aisha recalls between resets,
# which is the one thing a rehearsed S-07 cannot tolerate. Matches the
# preferred_models entry seeded for him in seed/memory_profiles.json.
DEMO_VEHICLE_CODE = "JET-T2"

# The audit trail records WHERE an action came from. This is not a conversation,
# so it does not get a uuid that would make a CLI action indistinguishable from a
# customer chat in the console. An honest origin marker beats a plausible lie.
DEMO_SESSION = "cli:create-demo-booking"


def _clear_checkpoints() -> list[str]:
    """Delete the LangGraph checkpoint store, WAL and shm alongside it.

    THE TRAP THIS EXISTS TO CLOSE. The checkpointer is its OWN sqlite file
    (AGENT_CHECKPOINT_PATH), separate from the business DB — so wiping the app
    data alone leaves every session's tool_use/tool_result history intact. A
    browser still holding that session_id in localStorage then rehydrates an
    EMPTY visual thread while the agent silently remembers the whole prior
    conversation: on stage it looks like the agent inventing context from
    nowhere, which is the one failure mode rule 2 exists to make impossible.

    Deleting the -wal and -shm siblings matters: new_sqlite_checkpointer opens
    the file in WAL mode, so committed state can be sitting in the -wal when the
    main file is removed, and sqlite would recover it into the fresh file.
    """
    removed: list[str] = []
    base = Path(AGENT_CHECKPOINT_PATH)
    for path in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def _print_demo_memory() -> None:
    """Show what Aisha will actually recall about the demo customer (S-07).

    The demo booking derives preferred_models / showroom / daypart from the slot
    it really booked, so those three can differ from what seed/memory_profiles
    .json loaded a moment earlier. Printing the result makes that visible on
    demo morning instead of surprising you on stage — rehearse against what this
    prints, not against the fixture.
    """
    db = SessionLocal()
    try:
        identified = customers_service.find_or_create(db, mobile=DEMO_MOBILE)
        profile = identified.get("memory")
        if not profile:
            print("  memory: nothing recalled (check memory consent in customers.json)")
            return
        showroom = profile["preferred_showroom"]
        models = ", ".join(m["name"] for m in profile["preferred_models"]) or "—"
        print(
            f"  memory Aisha will recall for {DEMO_MOBILE}:\n"
            f"    models   : {models}\n"
            f"    showroom : {showroom['name'] if showroom else '—'}\n"
            f"    daypart  : {profile['preferred_daypart'] or '—'}\n"
            f"    notes    : {profile['notes'] or '—'}"
        )
    finally:
        db.close()


def _cmd_purge_voice(args: argparse.Namespace) -> int:
    """Delete expired voice recordings (retention, see services/voice)."""
    from app.db import SessionLocal
    from app.services import voice as voice_service

    db = SessionLocal()
    try:
        count = voice_service.purge_expired(db)
        db.commit()
    finally:
        db.close()
    print(f"Purged {count} expired voice note(s).")
    return 0


def _drop_everything() -> None:
    """Drop every table, on SQLite AND Postgres.

    `Base.metadata.drop_all` is enough on SQLite, which does not enforce foreign
    keys by default, but it FAILS on Postgres:

        DependentObjectsStillExist: cannot drop table showrooms because other
        objects depend on it
        DETAIL: constraint slots_showroom_id_fkey on table slots ...

    SQLAlchemy normally topologically sorts the drops, but it cannot when the
    model graph contains a dependency cycle -- it warns and falls back to an
    arbitrary order, and Postgres then rejects the first parent it meets.

    So on Postgres we drop the SCHEMA with CASCADE instead. That is safe here
    precisely because reset-demo is a DEMO RESET, not a migration: it is the one
    command whose whole job is to throw the data away. Real schema evolution now
    goes through Alembic (app/migrate.py); this is the deliberate blunt path, and
    it is strictly more thorough than drop_all, which only removes tables the
    CURRENT metadata knows about and silently leaves behind anything renamed or
    deleted in an earlier version.

    NOTE: this is a demo/reset path only. It is never reachable from the API.
    """
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        Base.metadata.drop_all(bind=engine)
        # AND alembic's own bookkeeping table, which drop_all does NOT touch
        # because it is not part of Base.metadata. Leaving it behind is a trap
        # with teeth: the database would still be stamped at head while holding
        # no tables at all, so the `upgrade head` that follows would correctly
        # conclude there is nothing to do and reset-demo would finish with an
        # empty schema and no error. DROP SCHEMA CASCADE already covers this on
        # the Postgres branch above.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _sync_id_sequences() -> None:
    """Advance each table's id sequence past its seeded rows — POSTGRES ONLY.

    The seed fixtures INSERT customers/vehicles/showrooms/memory_profiles with
    EXPLICIT ids (1..N). On Postgres that does NOT move the table's id sequence,
    which still points at 1 — so the first time a NEW row is auto-assigned an id
    (a live attendee identifying as a new customer) Postgres hands out id=1 and
    the INSERT dies with `duplicate key value ... (id)=(1) already exists`.

    SQLite has no such sequence — INTEGER PRIMARY KEY tracks MAX(rowid) itself —
    so this is a no-op there, and the bug is invisible in local dev. That is
    exactly why it stayed hidden until the Postgres deploy: every demo customer
    is pre-seeded, so no auto-id customer was ever created until a real WhatsApp
    number showed up. Called at the end of seeding so reset-demo leaves a DB that
    can actually take a new booking on stage.
    """
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect as sa_inspect

    with engine.begin() as conn:
        for table in sa_inspect(engine).get_table_names():
            seq = conn.execute(
                text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table}
            ).scalar()
            if not seq:  # no serial id column (association tables etc.)
                continue
            max_id = conn.execute(
                text(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
            ).scalar()
            # setval to MAX(id): the next nextval() returns MAX(id)+1. Floor at 1
            # so an empty table stays valid (setval rejects 0).
            conn.execute(text("SELECT setval(:s, :v)"), {"s": seq, "v": max(max_id, 1)})


def _cmd_reset_demo(args: argparse.Namespace) -> int:
    """The whole demo-morning ritual, in one command that cannot forget a piece.

    Order is deliberate: schema, then reference data, then inventory, then the
    demo booking (which consumes inventory), then checkpoints last so nothing
    re-creates them behind us.
    """
    print("Resetting the demo environment.\n")

    # 1. Clean slate. Drop everything, then MIGRATE UP rather than create_all:
    #    reset-demo must exercise the same path production boots through, or the
    #    one command everybody runs would be the one that never tests the
    #    migrations. A truncate-by-hand also misses whatever table was added
    #    last week, which is why this drops rather than deletes.
    print("[1/6] Dropping all tables and migrating to head…")
    _drop_everything()
    migrate.upgrade_to_head()

    # 2. Reference data from the REVIEWED seed fixtures. These are shipped data,
    #    never regenerated casually (CLAUDE.md working conventions) — this only
    #    re-LOADS them.
    print("[2/6] Seeding customers, vehicles, showrooms from seed/*.json…")
    seed_showrooms.main()
    # ORDER IS AN FK ORDER, not a preference. Variants FK to models (seeded
    # together from one fixture), and stock FKs to BOTH variants and showrooms —
    # SQLite would insert dangling rows without complaint, so the order is the
    # only thing enforcing it on the dev database.
    seed_vehicle_models.main()
    seed_stock.main()
    seed_finance_banks.main()
    seed_accessories.main()
    seed_customers.main()

    # 3. Memory profiles (FR-MEM, S-07). AFTER customers — the rows carry
    #    customer_id FKs and SQLite will not stop us seeding them out of order.
    #    Its own numbered step rather than folded into [2/6] so an FK or
    #    consent-drift failure is visible on demo morning instead of buried.
    print("\n[3/6] Seeding memory profiles…")
    seed_memory_profiles.main()

    # 3b. Advance Postgres id sequences past the explicit-id seed rows. WITHOUT
    #     this, the first NEW customer created on stage (a real WhatsApp number,
    #     not a seeded one) collides on a duplicate id and the booking crashes.
    #     No-op on SQLite. Runs BEFORE the demo booking below, which itself
    #     creates auto-id rows.
    _sync_id_sequences()

    # 4. Inventory. The rolling window goes stale, so demo morning regenerates it.
    print(f"\n[4/6] Generating {args.days} days of slots…")
    gen = argparse.Namespace(
        days=args.days, prebooked=args.prebooked, reset=True, force=True, start=None
    )
    if _cmd_generate_slots(gen) != 0:
        return 1

    # 5. The demo booking, through the REAL write guards (never an INSERT), so a
    #    broken guard fails here at your desk instead of on stage.
    #
    #    vehicle_code is PINNED rather than left to "whatever slot is earliest".
    #    The demo customer is Ravi, who has memory consent, so this booking also
    #    runs the derived memory writer — and an unpinned car would make what
    #    Aisha recalls in S-07 change between resets.
    print("\n[5/6] Creating the demo booking…")
    demo = argparse.Namespace(
        mobile=DEMO_MOBILE,
        vehicle_code=DEMO_VEHICLE_CODE,
        doorstep=False,
        min_hours=25,
        address="Villa 12, Jumeirah 1, Dubai",
    )
    if _cmd_create_demo_booking(demo) != 0:
        return 1
    _print_demo_memory()

    # 6. Checkpoints LAST — see _clear_checkpoints for why this step exists.
    print("\n[6/6] Clearing agent checkpoints…")
    removed = _clear_checkpoints()
    print(f"  removed: {', '.join(removed) if removed else 'nothing (already clean)'}")

    print(
        "\nDemo environment ready.\n"
        "Browsers still holding an old session_id will find an empty transcript "
        "and rotate to a fresh session automatically."
    )
    return 0


def _cmd_generate_slots(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        result = slots_service.generate(
            db,
            days=args.days,
            prebooked=args.prebooked,
            reset=args.reset,
            force=args.force,
            start_day=date.fromisoformat(args.start) if args.start else None,
        )
    finally:
        db.close()

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        print(result.get("detail", ""), file=sys.stderr)
        return 1

    print(
        f"Generated {result['total']} slots over {result['days']} days "
        f"({result['window_start']} .. {result['window_end']})"
    )
    print(
        f"  open {result['open']} | booked {result['booked']} "
        f"({result['prebooked_ratio']:.1%} pre-booked)"
    )
    print()
    print(f"  {'showroom':<34} {'cars':>4} {'open':>6} {'booked':>7} {'total':>6}")
    print(f"  {'-' * 34} {'-' * 4} {'-' * 6} {'-' * 7} {'-' * 6}")
    for s in result["per_showroom"]:
        print(
            f"  [{s['showroom_id']}] {s['name']:<30.30} {s['vehicles']:>4} "
            f"{s['open']:>6} {s['booked']:>7} {s['total']:>6}"
        )
    d = result["doorstep"]
    print(
        f"  {'doorstep — ' + d['service_area']:<34} {d['vehicles']:>4} "
        f"{d['open']:>6} {d['booked']:>7} {d['total']:>6}"
    )
    return 0


def _cmd_top_up_slots(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        result = slots_service.top_up(db, horizon_days=args.days, prebooked=args.prebooked)
    finally:
        db.close()
    if result["added"]:
        print(
            f"Added {result['added']} slots "
            f"({result['window_start']} .. {result['window_end']})"
        )
    else:
        print(f"Window already reaches {result['window_end']}; nothing to add")
    return 0


def _pick_slot(
    db, *, doorstep: bool, vehicle_code: str | None, now: datetime, min_hours: int
) -> Slot | None:
    """Earliest open slot at least `min_hours` away, optionally for one model.

    Read-only. `min_hours` defaults to 25 rather than 0 for a concrete reason:
    the earliest open slot is usually TODAY, and booking a slot two hours out
    means both the T-24h and T-2h reminders are already overdue and get skipped
    (see services/notifications.py). The demo booking would then exist with no
    reminders at all — nothing for the WhatsApp simulator feed to show, and
    nothing for the reschedule-from-a-reminder journey (S-09) to act on.
    """
    stmt = (
        select(Slot)
        .where(
            Slot.status == SlotStatus.open,
            Slot.start > now + timedelta(hours=min_hours),
            Slot.location_type
            == (LocationType.doorstep if doorstep else LocationType.showroom),
        )
        .order_by(Slot.start, Slot.id)
    )
    if vehicle_code:
        stmt = stmt.join(VehicleModel, VehicleModel.id == Slot.vehicle_id).where(
            VehicleModel.model_code == vehicle_code.upper()
        )
    return db.scalars(stmt).first()


def _cmd_create_demo_booking(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        now = datetime.now()

        identified = customers_service.find_or_create(db, mobile=args.mobile)
        if "error" in identified:
            print(f"ERROR: {identified['error']} — {identified.get('hint', '')}", file=sys.stderr)
            return 1
        customer = identified["customer"]

        slot = _pick_slot(
            db,
            doorstep=args.doorstep,
            vehicle_code=args.vehicle_code,
            now=now,
            min_hours=args.min_hours,
        )
        if slot is None:
            print(
                f"ERROR: no open slot at least {args.min_hours}h away. Run "
                "`python -m app.cli generate-slots --reset` first.",
                file=sys.stderr,
            )
            return 1

        # Through the real lock, not a status poke: create_booking's first write
        # guard checks lock OWNERSHIP, so a demo booking that skipped this would
        # be testing a path no customer ever takes.
        locked = slots_service.lock(db, session_id=DEMO_SESSION, slot_id=slot.id)
        if not locked.get("locked"):
            print(f"ERROR: could not lock slot {slot.id}: {locked.get('reason')}", file=sys.stderr)
            return 1

        # Rule 9 precondition 3, satisfied EXPLICITLY rather than exempted. The
        # demo booking is not a conversation, so there is no reminder to give and
        # nothing here pretends otherwise — but it must still pass the same guard
        # a customer booking passes, or the CLI stops proving the guards work and
        # a broken one surfaces on stage instead of at your desk.
        #
        # Note what is NOT here: no bypass parameter on booking.create, and no
        # demo-session exemption inside the guard. Either would be a door left
        # open for the next caller that finds the rule inconvenient.
        acked = slots_service.acknowledge_licence_reminder(db, session_id=DEMO_SESSION)
        if not acked.get("acknowledged"):
            print(
                f"ERROR: could not acknowledge the licence reminder: {acked.get('reason')}",
                file=sys.stderr,
            )
            return 1

        result = booking_service.create(
            db,
            session_id=DEMO_SESSION,
            customer_id=customer["customer_id"],
            vehicle_id=slot.vehicle_id,
            slot_id=slot.id,
            doorstep_address=args.address if args.doorstep else None,
            now=now,
        )
        if "error" in result:
            db.rollback()
            print(f"ERROR: {result['error']} — {result.get('detail', '')}", file=sys.stderr)
            return 1

        # The service flushes; the caller owns the transaction (services/audit.py).
        db.commit()
    finally:
        db.close()

    b = result["booking"]
    notes = result["notifications"]
    print(f"Created {b['booking_ref']} for customer {b['customer_id']} ({args.mobile})")
    print(f"  {b['vehicle_name']} — {b['start_iso']} — {b['location_type']}")
    if b["location_type"] == "doorstep":
        print(f"  address: {b['doorstep_address']}")
    else:
        print(f"  showroom: [{b['showroom_id']}] {b['showroom_name']}")
    print()
    print(f"  {'notification':<14} {'send_at':<20} status")
    print(f"  {'-' * 14} {'-' * 20} {'-' * 9}")
    # Trim to seconds for display only — the stored send_at keeps full precision.
    for n in notes["scheduled"]:
        print(f"  {n['kind']:<14} {n['send_at'][:19]:<20} scheduled")
    for n in notes["skipped"]:
        print(f"  {n['kind']:<14} {n['send_at'][:19]:<20} SKIPPED ({n['reason']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Elite demo operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser(
        "generate-slots",
        help="Generate the rolling test-drive inventory window.",
        description=(
            "Re-runnable: regenerate on demo morning, or between test runs that "
            "consume inventory. --reset aborts if any booking references a slot."
        ),
    )
    gen.add_argument("--days", type=int, default=14, help="Rolling window length (default: 14).")
    gen.add_argument(
        "--prebooked",
        type=float,
        default=0.18,
        help=(
            "Fraction marked booked, deterministically, so 'that time's taken' "
            "(S-05) happens naturally in testing (default: 0.18)."
        ),
    )
    gen.add_argument("--reset", action="store_true", help="Delete existing slots first.")
    gen.add_argument(
        "--force",
        action="store_true",
        help="With --reset, delete slots even if bookings reference them (orphans them).",
    )
    gen.add_argument("--start", help="Window start as ISO date (default: today).")
    gen.set_defaults(func=_cmd_generate_slots)

    top = sub.add_parser(
        "top-up-slots",
        help="Extend the slot window so it reaches --days ahead. Never duplicates.",
        description=(
            "Appends inventory only for days after the last day that already has "
            "slots. A no-op when the window is already long enough. The API runs "
            "this daily when SLOT_TOPUP_ENABLED=true."
        ),
    )
    top.add_argument("--days", type=int, default=14, help="Horizon from today (default: 14).")
    top.add_argument(
        "--prebooked",
        type=float,
        default=0.18,
        help="Fraction of the NEW slots pre-booked (default: 0.18).",
    )
    top.set_defaults(func=_cmd_top_up_slots)

    demo = sub.add_parser(
        "create-demo-booking",
        help="Create the demo booking through the real booking service.",
        description=(
            "Books the earliest open slot for the seeded demo customer, via "
            "find_or_create -> lock -> create_booking. No INSERT: the booking "
            "passes the same write guards as a customer's, and its reference is "
            "GENERATED (FR-BK-08), not chosen. Run generate-slots first."
        ),
    )
    demo.add_argument(
        "--mobile",
        default=DEMO_MOBILE,
        help=f"Customer mobile, any UAE format (default: {DEMO_MOBILE}, Ravi Sharma).",
    )
    demo.add_argument(
        "--vehicle-code",
        help="Restrict to one model, e.g. JET-T2-IDM (default: whichever is free soonest).",
    )
    demo.add_argument(
        "--doorstep",
        action="store_true",
        help="Book a doorstep slot instead of a showroom one (FR-SL-07).",
    )
    demo.add_argument(
        "--min-hours",
        type=int,
        default=25,
        help=(
            "Only consider slots at least this many hours away (default: 25). "
            "Below 24 the T-24h reminder is already overdue and gets skipped, "
            "leaving the demo booking with no reminder feed. Pass 0 to book the "
            "very next slot regardless."
        ),
    )
    demo.add_argument(
        "--address",
        default="Villa 12, Jumeirah 1, Dubai",
        help="Doorstep address. Used with --doorstep only; never stored otherwise.",
    )
    demo.set_defaults(func=_cmd_create_demo_booking)

    reset = sub.add_parser(
        "reset-demo",
        help="THE demo-morning command: full clean slate, ready to present.",
        description=(
            "Drops and recreates every table, re-seeds customers/vehicles/"
            "showrooms from seed/*.json, regenerates the rolling slot window, "
            "creates the demo booking through the real write guards, and CLEARS "
            "THE AGENT CHECKPOINT STORE. That last step is the one people forget "
            "by hand: checkpoints live in their own sqlite file, so wiping the "
            "app DB alone leaves the agent remembering conversations whose "
            "transcripts are gone. Run this, and only this, before a demo."
        ),
    )
    reset.add_argument("--days", type=int, default=14, help="Rolling window length (default: 14).")
    reset.add_argument(
        "--prebooked",
        type=float,
        default=0.18,
        help="Fraction pre-booked so 'that time's taken' (S-05) happens naturally.",
    )
    reset.set_defaults(func=_cmd_reset_demo)

    purge = sub.add_parser(
        "purge-voice",
        help="Delete voice recordings past their retention horizon.",
        description=(
            "Hard-deletes stored voice notes older than VOICE_RETENTION_HOURS "
            "(default 48h). Transcription already sweeps opportunistically on "
            "every recording, so this exists for the case that sweep cannot "
            "cover: a demo box left idle for days, where nobody records and "
            "therefore nothing triggers the sweep, and the recordings sit there "
            "past the retention we documented. 'Forget me' does not wait for "
            "either path — it deletes at once. Transcripts are NOT touched: the "
            "conversation is the transcript, and only the audio expires."
        ),
    )
    purge.set_defaults(func=_cmd_purge_voice)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
