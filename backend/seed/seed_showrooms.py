"""Seed the `showrooms` table from seed/showrooms.json.

Recreates the `showrooms` table (drop + create) so the current schema —
including hours_by_day (FR-SL-02) and city — is applied, then inserts the
fixture rows verbatim.

Run from the backend repo root so the dev SQLite URL (`sqlite:///./elite_demo.db`)
resolves to the same database the API uses:

    python seed/seed_showrooms.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a plain script (`python seed/seed_showrooms.py`): put the
# backend repo root (this file's parent's parent) on the path so `app` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Showroom  # noqa: E402

FIXTURE = Path(__file__).with_name("showrooms.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Ensure the table exists, then clear it -- rather than DROP + CREATE.
    #
    # Postgres refuses to drop `showrooms` while slots, waitlist and
    # memory_profiles hold foreign keys into it; SQLite does not enforce that,
    # which is why the old drop/create survived until the first real deployment.
    #
    # Schema CHANGES are picked up by `reset-demo`, which drops the whole schema
    # before calling this -- per CLAUDE.md, the DB is disposable and reset-demo
    # is the canonical path. This function's job is to (re)LOAD the fixtures.
    Showroom.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(Showroom).delete()
        for r in rows:
            session.add(
                Showroom(
                    id=r["id"],
                    brand=r["brand"],
                    name=r["name"],
                    emirate=r["emirate"],
                    # Locality slug — the only place "Al Ain" exists as DATA.
                    # Without it, ranking a city match means substring-matching
                    # `name`, i.e. display text doing a data field's job.
                    city=r["city"],
                    address=r["address"],
                    # Required, not .get(): an Arabic conversation showing a
                    # Latin-script address is exactly the LOC-01 failure this
                    # column exists to prevent, so a fixture row missing it
                    # should fail loudly at seed time rather than on stage.
                    address_ar=r["address_ar"],
                    map_link=r["map_link"],
                    # Console dashboard map pins (2026-07-23). `.get()`, unlike
                    # address_ar above, because a missing coordinate is a
                    # RECOVERABLE absence: the pin is simply not drawn, and the
                    # branch still appears in every list and every booking. A
                    # missing Arabic address has no such fallback, which is why
                    # only that one fails the seed loudly.
                    #
                    # ⚠️ APPROXIMATE, PENDING ELITE SIGN-OFF — district
                    # positions, not surveyed entrances. See models.Showroom.
                    latitude=r.get("latitude"),
                    longitude=r.get("longitude"),
                    phone=r["phone"],
                    hours=r["hours"],
                    # Structured per-weekday hours drive slot generation
                    # (FR-SL-02, S-22); `hours` above is display text only.
                    hours_by_day=r["hours_by_day"],
                )
            )
        session.commit()
        print(f"Seeded {len(rows)} showrooms into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
