"""slots.variant_id and bookings.variant_id — test drives get a trim

Test-drive inventory was grained (showroom, model) on the physical premise that
"a showroom genuinely has one demo car per model" (slots.generate). Three of the
nine models carry several trims — the G700 spans AED 169,000 to 240,450 across
four — so a customer booked "a G700" and nothing recorded, or could record,
which of them they were coming to drive. This is the schema half of moving that
grain down to (showroom, variant).

BOTH COLUMNS ARE NULLABLE, and neither is nullable because the value is
optional. `slots.variant_id` is NULL only for inventory written before this
revision; slots.generate always sets it, and query_available refuses to satisfy
an explicit variant filter from a NULL row, so a database that has not been
regenerated answers "no times" rather than offering a Comfort as a Flagship.
`bookings.variant_id` is NULL where it is genuinely unknown.

THE BACKFILL ONLY WRITES WHAT IT CAN DERIVE. A model with exactly one bookable
trim leaves no room for doubt, so every slot and booking of one is filled. A
model with several is left NULL rather than guessed — the slots are regenerated
by `cli slots generate --reset`, and a historic booking that never recorded its
trim does not acquire one here. THIS MIGRATION DELETES NOTHING: orphaning or
rewriting a booking to make a column tidy is exactly the trade the write
architecture exists to refuse.

`coming_soon` is the bookability predicate, not a name — the G700 Top Fire is
the one variant carrying it (tests/test_seed_variants.py), and it has no price
because it cannot be ordered, let alone driven.

Revision ID: c81f4a2d6b07
Revises: b3e2a90d41cf
Create Date: 2026-09-03 11:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c81f4a2d6b07'
down_revision: Union[str, Sequence[str], None] = 'b3e2a90d41cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# One bookable trim and no other: the only case a backfill can settle. Written
# as a correlated count rather than a join so the same statement runs on SQLite
# and Postgres, and `NOT coming_soon` rather than `= 0` for the same reason.
_SOLE_BOOKABLE_VARIANT = """
    UPDATE {table} SET variant_id = (
        SELECT MIN(v.id) FROM vehicle_variants v
         WHERE v.model_id = {table}.vehicle_id AND NOT v.coming_soon
    )
    WHERE variant_id IS NULL
      AND (SELECT COUNT(*) FROM vehicle_variants v
            WHERE v.model_id = {table}.vehicle_id AND NOT v.coming_soon) = 1
"""


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("slots", "bookings"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("variant_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                f"fk_{table}_variant_id_vehicle_variants",
                "vehicle_variants",
                ["variant_id"],
                ["id"],
            )
            batch_op.create_index(f"ix_{table}_variant_id", ["variant_id"])
        op.execute(sa.text(_SOLE_BOOKABLE_VARIANT.format(table=table)))


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("slots", "bookings"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table}_variant_id")
            batch_op.drop_constraint(
                f"fk_{table}_variant_id_vehicle_variants", type_="foreignkey"
            )
            batch_op.drop_column("variant_id")
