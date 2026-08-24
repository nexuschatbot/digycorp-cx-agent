"""Seed the `customers` table from seed/customers.json.

Recreates the `customers` table (drop + create) so the current schema — including
the columns added for the customer profile (title_ar, gender, nationality,
emirate, email) — is applied, then inserts the fixture rows verbatim.

Run from the backend repo root so the dev SQLite URL (`sqlite:///./elite_demo.db`)
resolves to the same database the API uses:

    python seed/seed_customers.py

Timestamps in the JSON carry the +04:00 (Asia/Dubai) offset. The model's
DateTime columns are naive, matching the rest of the app (func.now() etc.), so
each value is stored as its local UAE wall-clock time with the offset dropped —
the printed date/time is preserved exactly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running as a plain script (`python seed/seed_customers.py`): put the
# backend repo root (this file's parent's parent) on the path so `app` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Customer, Language  # noqa: E402

FIXTURE = Path(__file__).with_name("customers.json")


def _parse_dt(value: str | None) -> datetime | None:
    """ISO string (possibly tz-aware) -> naive local datetime, or None."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)  # store local UAE wall-clock, naive
    return dt


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Apply the current schema to just the customers table.
    # Ensure the table exists, then clear it -- see the note in seed_showrooms:
    # Postgres will not drop a table other tables reference, and reset-demo owns
    # schema changes by dropping the whole schema first.
    Customer.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(Customer).delete()
        for r in rows:
            session.add(
                Customer(
                    id=r["id"],
                    title=r["title"],
                    title_ar=r["title_ar"],
                    name_en=r["name_en"],
                    name_ar=r["name_ar"],
                    gender=r["gender"],
                    nationality=r["nationality"],
                    emirate=r["emirate"],
                    mobile=r["mobile"],
                    email=r["email"],
                    language_pref=Language(r["language_pref"]),
                    memory_consent=r["memory_consent"],
                    memory_consent_at=_parse_dt(r["memory_consent_at"]),
                    whatsapp_consent=r["whatsapp_consent"],
                    whatsapp_consent_at=_parse_dt(r["whatsapp_consent_at"]),
                    winback_flag=r["winback_flag"],
                    created_at=_parse_dt(r["created_at"]),
                    updated_at=_parse_dt(r["updated_at"]),
                )
            )
        session.commit()
        print(f"Seeded {len(rows)} customers into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
