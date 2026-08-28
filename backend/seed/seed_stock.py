"""Seed the `stock` table from seed/stock.json.

    python seed/seed_stock.py

RUN AFTER seed_vehicle_models AND seed_showrooms: every row FKs to a variant
and a showroom, and SQLite will happily insert rows pointing at neither.

WHAT THE GRAIN IS. One row per (variant, showroom, exterior colour) — 66
combinations across 5 branches, 330 rows. Interior colour is NOT part of it:
Elite's unit counts are per exterior colour, and the Interior Colors column of
their sheet is a separate model-level palette that happens to be typed down the
same block of rows. Reading it as a pairing would invent claims they never made.

ZERO-UNIT ROWS ARE KEPT, 38 of them. "None of that colour at that branch" is a
FACT Elite supplied, and it is the answer to a question customers actually ask.
Dropping the row would turn a known no into an unknown.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Stock  # noqa: E402

FIXTURE = Path(__file__).with_name("stock.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    Stock.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(Stock).delete()
        for r in rows:
            session.add(
                Stock(
                    variant_id=r["variant_id"],
                    showroom_id=r["showroom_id"],
                    colour=r["colour"],
                    units=r["units"],
                )
            )
        session.commit()
        total = sum(r["units"] for r in rows)
        print(f"Seeded {len(rows)} stock rows ({total} units) into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
