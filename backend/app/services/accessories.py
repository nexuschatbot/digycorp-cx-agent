"""The priced accessory catalogue (2026-08-31).

WHY THIS EXISTS AT ALL. The `accessories` table has been seeded since T0.7 —
five rows, straight from Elite's "List of accessories and pricing" sheet — and
until now NOTHING READ IT. No tool, no service, no endpoint: `grep -rl Accessory
app/tools app/services app/api` returned nothing. A customer who asked the price
of the alloy wheel upgrade was told we did not have it, while the row sat in the
database with 4500 in it. The model was built and never wired up.

PRICES COME FROM HERE AND NOWHERE ELSE, which is the reason models.py put
accessories in the database rather than in a knowledge markdown in the first
place. Two price sources is how a customer is quoted one number in chat and
another on the invoice.

model_id IS NULLABLE AND NULL MEANS "FITS ANYTHING". Filtering by model must
therefore return the model's own accessories AND the universal ones — a filter
that dropped the universal rows would hide four of Elite's five products the
moment a customer mentioned a car.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Accessory


def _row(a: Accessory) -> dict:
    return {
        # THE SKU IS CARRIED BUT NEVER SPOKEN — see models.py::Accessory. It is
        # here because promotions.md names accessories by code ("Free window
        # tinting (ACC-002)"), and matching that to a product on the name string
        # is exactly the fragile join the code exists to avoid.
        "part_code": a.part_code,
        "name": a.name_en,
        "description": a.description_en,
        "price_aed": a.price_aed,
        # True when Elite ties this part to one model rather than selling it
        # across the range. Stated rather than implied by the filter, so an
        # agent can say "fits your X70" instead of guessing.
        "model_specific": a.model_id is not None,
    }


def catalogue(db: Session, *, model_id: int | None = None) -> dict:
    """Every accessory Elite sells, or the ones that fit one model.

    Never raises. An empty catalogue is returned as an empty list and said so,
    not as an error: "we publish no accessories" is a real answer.
    """
    stmt = select(Accessory).order_by(Accessory.part_code, Accessory.id)
    if model_id is not None:
        stmt = stmt.where(
            or_(Accessory.model_id == model_id, Accessory.model_id.is_(None))
        )

    rows = [_row(a) for a in db.scalars(stmt).all()]
    return {
        "accessories": rows,
        "count": len(rows),
        "filtered_to_model_id": model_id,
        # EVERY FIGURE IS A PART PRICE, NOT A FITTED PRICE. Elite supplied the
        # cost of the accessory; nothing in the sheet says whether fitting is
        # included, so the agent must not imply that it is.
        "prices_exclude_fitting": True,
    }
