"""vehicle_variants.images — trim-specific photography

Elite shoots one car per model, so a "car" has always had exactly one set of
photos hanging off vehicle_models. The Dashing delivery of 2026-09-04 broke that
assumption: it arrived as two shoots, the 1.5 in Dark Green and the 1.6 in Light
Blue, and with images only on the model row there was nowhere to put the second
one — both trims would show whichever car happened to be listed first.

EMPTY IS THE ORDINARY STATE. Thirteen of the fifteen trims have no photography
of their own; only the two Dashing rows carry a value today. An empty list means
"this trim was not shot separately", never "this car has no photos" — the model
row still holds the set every card is drawn from, unchanged.

NO BACKFILL, unlike c81f4a2d6b07 before it. There is nothing to derive: which
trim a photograph shows is a fact about the shoot, readable only from Elite's
filenames, and the seed fixture is where that mapping is recorded. An existing
database upgraded by this revision is CORRECT with every row empty — every trim
keeps showing its model's car, exactly as it did yesterday. `python -m
seed.seed_vehicle_models` is what puts the Dashing pair in.

Revision ID: a7d41f0b92e5
Revises: c81f4a2d6b07
Create Date: 2026-09-04 17:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d41f0b92e5'
down_revision: Union[str, Sequence[str], None] = 'c81f4a2d6b07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Two passes, matching b3e2a90d41cf: the server_default is what lets the
    # column be NOT NULL over rows that already exist, and dropping it again
    # afterwards keeps the default in the model layer where the rest of the
    # JSON columns keep theirs.
    with op.batch_alter_table('vehicle_variants', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'images',
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table('vehicle_variants', schema=None) as batch_op:
        batch_op.alter_column('images', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('vehicle_variants', schema=None) as batch_op:
        batch_op.drop_column('images')
