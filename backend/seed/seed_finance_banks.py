"""Seed the `finance_banks` table from seed/finance_banks.json.

    python seed/seed_finance_banks.py

EVERY ROW ELITE SUPPLIED IS MARKED "Sample", and `is_sample` carries that into
the database rather than leaving it as a fact somebody has to remember. Nothing
here may be presented as a quotation — services/finance computes an INDICATIVE
instalment and the agent says so (rule 13).

`financing_type` and `notes` from the workbook have NO COLUMN. The distinction
that matters — Shariah-compliant or not — is stored as `islamic`, which is what
customers filter on; the notes are the same "Sample dealer-panel bank" boilerplate
on every row, which `is_sample` already carries. They stay in the JSON for
reference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import FinanceBank  # noqa: E402

FIXTURE = Path(__file__).with_name("finance_banks.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    FinanceBank.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(FinanceBank).delete()
        for r in rows:
            session.add(
                FinanceBank(
                    id=r["id"],
                    name=r["name"],
                    rate_percent=r["rate_percent"],
                    min_down_payment_percent=r["min_down_payment_percent"],
                    max_tenure_months=r["max_tenure_months"],
                    islamic=r["islamic"],
                    is_sample=r["is_sample"],
                )
            )
        session.commit()
        print(f"Seeded {len(rows)} finance banks into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
