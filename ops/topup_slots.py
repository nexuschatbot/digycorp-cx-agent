#!/usr/bin/env python3
"""Top the app's slot inventory back up to a rolling horizon. Idempotent.

WHAT THIS IS FOR. Inventory is a WINDOW, and a window expires. On 14 Aug 2026 the
app's ran to the 13th and Salesforce's to the 9th, so on the morning of the 14th
nothing was bookable at all — while the client was actively testing. That is the
failure this exists to stop happening again, on a schedule rather than when
somebody notices.

APPEND-ONLY, AND ONLY FORWARD. It reads the furthest future slot we already hold
and generates from the NEXT day onward. Nothing existing is touched, nothing is
deleted, and running it twice in a day is a no-op the second time.

WHY NOT `generate-slots --force`. Because --force appends a whole fresh window
starting TODAY, on top of the one already there — which is exactly the
double-generation the service's own comment records as having cost a
live-severity bug (two 9 AM chips for the same car, and a chip label that hides
which slot_id a tap pins). The `start_day` argument is the safe door, and this
script exists to make sure that is the door that gets used.

Run INSIDE the backend container:
    docker exec -i elite-backend-1 python3 - < topup_slots.py

Env: TARGET_DAYS (default 14) — how far ahead inventory should reach.
     CONFIRM=1 to write; dry-run otherwise.
"""
import os
import sys
from datetime import date, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Slot
from app.services import slots as slots_service

TARGET_DAYS = int(os.environ.get("TARGET_DAYS", "14"))
CONFIRM = os.environ.get("CONFIRM") == "1"

today = date.today()
want_through = today + timedelta(days=TARGET_DAYS)

db = SessionLocal()
try:
    furthest = db.scalar(select(func.max(Slot.start)))
    have_through = furthest.date() if furthest else today - timedelta(days=1)

    print(f"today={today}  inventory reaches {have_through}  target {want_through}")

    if have_through >= want_through:
        print(f"OK: already {(have_through - today).days} days ahead — nothing to do.")
        sys.exit(0)

    start_day = have_through + timedelta(days=1)
    days = (want_through - start_day).days + 1
    print(f"generating {days} day(s) from {start_day}")

    if not CONFIRM:
        print("DRY-RUN — set CONFIRM=1 to write.")
        sys.exit(0)

    # force=True is required because the table is non-empty, and it is SAFE here
    # only because start_day is past everything we hold: the guard it overrides
    # protects against re-generating days that already exist, which by
    # construction cannot happen from this start_day.
    out = slots_service.generate(
        db, days=days, start_day=start_day, force=True, prebooked=0.18
    )
    if out.get("error"):
        sys.exit(f"ERROR: {out}")
    db.commit()

    now_through = db.scalar(select(func.max(Slot.start)))
    total = db.scalar(select(func.count()).select_from(Slot).where(Slot.start >= today))
    print(f"created={out.get('created')}  inventory now reaches {now_through}  "
          f"future slots={total}")
finally:
    db.close()
