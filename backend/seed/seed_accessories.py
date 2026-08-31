"""Seed the `accessories` table from seed/accessories.json.

    python seed/seed_accessories.py

IN THE DATABASE RATHER THAN A KNOWLEDGE MD because these carry prices, and
prices live in exactly one place (the plan's workbook-split decision). Two price
sources is how a customer is quoted one number in chat and another on the invoice.

`model_id` IS NULL ON EVERY ROW: none of the five accessories Elite listed is
tied to a model. The column exists for the day one is.

`description_en` IS THE CUSTOMER-FACING HALF — "20-inch sport alloy rims, set of
4, direct-fit" is what makes the 4,500 mean something. Without it the agent can
quote a name and a price and nothing else, and inventing the rest is the exact
grounding failure the guardrails exist to prevent.

`part_code` is NEVER SPOKEN. It exists so a re-import joins on ACC-004 rather
than on the string "Paint Protection Film (PPF)", parentheses and all.

NO ARABIC, for either field — Elite supplied none. An Arabic conversation gets a
price and an English sentence, which is a gap to take back to Elite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Accessory  # noqa: E402

FIXTURE = Path(__file__).with_name("accessories.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    Accessory.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(Accessory).delete()
        for r in rows:
            session.add(
                Accessory(
                    id=r["id"],
                    part_code=r["part_code"],
                    name_en=r["name_en"],
                    description_en=r["description_en"],
                    # NULL, not a machine translation: a wrong Arabic part name
                    # is worse than none, and the column is nullable for this.
                    name_ar=r["name_ar"],
                    price_aed=r["price_aed"],
                    model_id=r["model_id"],
                )
            )
        session.commit()
        print(f"Seeded {len(rows)} accessories into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
