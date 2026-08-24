"""Seed the `vehicle_models` table from seed/vehicle_models.json.

Recreates the `vehicle_models` table (drop + create) so the current schema —
including the columns added for the catalogue (model_code, data_provenance,
images), the seats change (display text -> seats_min/seats_max ints,
FR-BK-09/10), the fuel change (display text -> fuel_type enum), and the price
change (price_bracket_aed text -> price_aed_min/price_aed_max ints) — is
applied, then inserts the fixture rows verbatim.

Run from the backend repo root so the dev SQLite URL (`sqlite:///./elite_demo.db`)
resolves to the same database the API uses, and so the image paths stored in the
`images` column (e.g. "seed/media/vehicles/jet-g700.jpeg") resolve from here:

    python seed/seed_vehicle_models.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a plain script (`python seed/seed_vehicle_models.py`): put the
# backend repo root (this file's parent's parent) on the path so `app` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, engine  # noqa: E402
from app.models import FuelType, VehicleModel  # noqa: E402

FIXTURE = Path(__file__).with_name("vehicle_models.json")


def main() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Apply the current schema to just the vehicle_models table.
    # Ensure the table exists, then clear it -- see the note in seed_showrooms:
    # Postgres will not drop a table other tables reference, and reset-demo owns
    # schema changes by dropping the whole schema first.
    VehicleModel.__table__.create(engine, checkfirst=True)

    session = SessionLocal()
    try:
        session.query(VehicleModel).delete()
        for r in rows:
            session.add(
                VehicleModel(
                    id=r["id"],
                    model_code=r["model_code"],
                    brand=r["brand"],
                    name=r["name"],
                    body_type=r["body_type"],
                    # Seat RANGE, not a value — X70 Plus is 5-7 (FR-BK-09/10).
                    seats_min=r["seats_min"],
                    seats_max=r["seats_max"],
                    # Null bounds (JET-T1-IDM) are intentional: price not
                    # published (FR-CX-07). Do not coerce to 0 or "".
                    price_aed_min=r["price_aed_min"],
                    price_aed_max=r["price_aed_max"],
                    fuel_type=FuelType(r["fuel_type"]),
                    headline_features=r["headline_features"],
                    # Required, not .get() — a row added without Arabic should
                    # fail here rather than ship a car that goes silent in an
                    # Arabic conversation (LOC-01).
                    headline_features_ar=r["headline_features_ar"],
                    showroom_ids=r["showroom_ids"],
                    data_provenance=r["data_provenance"],
                    images=r["images"],
                )
            )
        session.commit()
        print(f"Seeded {len(rows)} vehicle models into {engine.url}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
