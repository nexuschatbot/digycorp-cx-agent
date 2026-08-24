"""Seed the `memory_profiles` table from seed/memory_profiles.json (FR-MEM, S-07).

What a returning customer's stored preferences look like BEFORE they say a word.
Without this there is nothing for rule 12's reactive recall to recall, and S-07
has no data behind it.

ONLY CONSENTED CUSTOMERS APPEAR HERE, and main() refuses to seed a row for anyone
whose customers.json entry has memory_consent false. A profile without consent is
precisely the state the gate in services/memory.py exists to make impossible, so
a fixture that created one would be seeding a bug. tests/test_memory_seed.py
asserts the same thing against the file itself.

IDS, NEVER NAMES. preferred_showroom_id is an FK and preferred_models holds
vehicle_ids, because a stored English showroom name could not be spoken in an
Arabic turn (LOC-01) and would go stale on a rename. The agent phrases them at
speaking time from the joined names memory._profile_dict returns.

RUN AFTER seed_customers.py — the rows carry customer_id FKs, and SQLite does not
enforce them by default, so seeding out of order leaves orphans that fail silently
rather than loudly. `python -m app.cli reset-demo` always runs both, in order.

    python seed/seed_memory_profiles.py

RAVI (customer 3, the S-07 case) CARRIES ONLY `notes`, AND THAT IS DELIBERATE.
`reset-demo` creates his demo booking, and booking.create then derives
preferred_models / preferred_showroom_id / preferred_daypart from the slot it
actually booked. Seeding those three for him would be writing values that get
overwritten seconds later — and they cannot be made to agree in advance: the
booked slot is "the earliest open one at least 25h out", so its daypart moves
with the calendar even though the CAR is pinned (cli.DEMO_VEHICLE_CODE). A
fixture that claims "evening" while the booking derives "afternoon" is exactly
the drift that makes a rehearsed S-07 fail on stage.

So the two writers converge by construction rather than by coincidence: only one
of them writes each column. `notes` is never touched by the derived writer, so it
is the one thing worth seeding for him, and it survives verbatim. reset-demo
prints the resulting profile — rehearse against what that prints.

Customers 1 and 4 have no demo booking, so nothing derives over them and their
structured columns are seeded in full.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a plain script: put the backend repo root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Customer, MemoryProfile  # noqa: E402

FIXTURE = Path(__file__).with_name("memory_profiles.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Ensure the table exists, then clear it -- see the note in seed_showrooms:
    # Postgres will not drop a table other tables reference, and reset-demo owns
    # schema changes by dropping the whole schema first.
    MemoryProfile.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(MemoryProfile).delete()
        consented = {
            c.id for c in session.query(Customer).filter(Customer.memory_consent.is_(True))
        }
        skipped: list[int] = []

        for r in rows:
            if r["customer_id"] not in consented:
                # Loud, not silent: a fixture drifting out of step with
                # customers.json is a data bug worth seeing on demo morning.
                skipped.append(r["customer_id"])
                continue
            session.add(
                MemoryProfile(
                    # `or []` not `r[...]`: a null in the fixture means "the
                    # derived writer owns this column", and the JSON column
                    # default does not apply to an explicit None.
                    customer_id=r["customer_id"],
                    preferred_models=r["preferred_models"] or [],
                    preferred_showroom_id=r["preferred_showroom_id"],
                    preferred_daypart=r["preferred_daypart"],
                    notes=r["notes"],
                )
            )
        session.commit()

        seeded = len(rows) - len(skipped)
        print(f"Seeded {seeded} memory profiles into {engine.url}")
        if skipped:
            print(
                f"  SKIPPED (no memory consent in customers.json): {skipped}\n"
                "  A profile without consent is the state the gate exists to prevent."
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
