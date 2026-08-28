"""accessory part code and description; model interior colours

Revision ID: f114c8995e51
Revises: 7c241e4d550a
Create Date: 2026-08-28

Three columns for data Elite supplied that T0.7's tables had nowhere to put, so
it sat in seed/*.json loaded by nothing:

  accessories.part_code      — "ACC-003". A stable join key for a re-import;
                               never spoken to a customer.
  accessories.description_en — what the thing actually IS. Three of the five
                               names mean nothing without it, and the agent
                               speaks only from tool results, so without this
                               she cannot describe an accessory at all.
  vehicle_models.interior_colours — the interior palette. NOT paired with the
                               exterior colour and NOT stocked; see the column
                               comment in models.py before answering with it.

WHY THIS FILE IS NOT WHAT AUTOGENERATE WROTE. Alembic emitted
`interior_colours` as `nullable=False` with no server default, because
`mapped_column(JSON, default=list)` is a PYTHON-side default applied by
SQLAlchemy on insert — it produces no DDL default at all. Adding a NOT NULL
column with no default to a table that already holds nine vehicle models fails:
under SQLite's batch mode the table is rebuilt and every existing row would
carry NULL in a NOT NULL column.

So the column is added WITH a server default to backfill the existing rows, and
the default is then dropped in a second pass so the schema matches models.py
exactly rather than drifting by one DDL default nothing asked for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f114c8995e51'
down_revision: Union[str, Sequence[str], None] = '7c241e4d550a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("accessories", schema=None) as batch_op:
        # Both nullable: the columns describe rows that already exist, and a
        # future accessory Elite supplies without a code is a real possibility.
        batch_op.add_column(sa.Column("part_code", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("description_en", sa.Text(), nullable=True))
        # Unique so a re-import cannot land the same SKU twice under two names.
        batch_op.create_index(
            batch_op.f("ix_accessories_part_code"), ["part_code"], unique=True
        )

    # Step 1: add with a server default so existing rows backfill to an empty
    # list rather than NULL. '[]' is the JSON literal, not a SQL empty string.
    with op.batch_alter_table("vehicle_models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "interior_colours",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )

    # Step 2: drop the default now that every row has a value. Without this the
    # database carries a DDL default models.py does not declare — harmless
    # today only because compare_server_default is off, which is exactly the
    # kind of drift that is invisible until it is not.
    with op.batch_alter_table("vehicle_models", schema=None) as batch_op:
        batch_op.alter_column("interior_colours", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("vehicle_models", schema=None) as batch_op:
        batch_op.drop_column("interior_colours")

    with op.batch_alter_table("accessories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_accessories_part_code"))
        batch_op.drop_column("description_en")
        batch_op.drop_column("part_code")
