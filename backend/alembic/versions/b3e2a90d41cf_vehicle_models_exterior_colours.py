"""vehicle_models exterior_colours

The per-model exterior palette from Elite's Models sheet. It had no column
because a standing note said stock rows already carried the colours — true of
availability, wrong about existence: a colour with no stock rows anywhere did
not exist in the database, and "what colours does it come in" dead-ended live
(2026-09-01). See VehicleModel.exterior_colours for the full account.

NOT NULL, added in two steps — with a server default of '[]' to backfill the
nine existing rows, then the default dropped so the schema matches models.py
exactly. The same dance, for the same reason, as interior_colours in
f114c8995e51: `mapped_column(JSON, default=list)` is a Python-side default
producing no DDL, and SQLite's batch rebuild would otherwise put NULL into a
NOT NULL column. A backfilled [] reads as "not reseeded yet"; the seeder
refuses a fixture row without a palette, so it cannot persist past a seed.

Revision ID: b3e2a90d41cf
Revises: 0df4c14ca33c
Create Date: 2026-09-01 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e2a90d41cf'
down_revision: Union[str, Sequence[str], None] = '0df4c14ca33c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('vehicle_models', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'exterior_colours',
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table('vehicle_models', schema=None) as batch_op:
        batch_op.alter_column('exterior_colours', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('vehicle_models', schema=None) as batch_op:
        batch_op.drop_column('exterior_colours')
