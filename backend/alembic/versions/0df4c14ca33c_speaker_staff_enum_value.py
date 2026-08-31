"""speaker 'staff' enum value

Human takeover (2026-08-31): an admin who takes over an escalated conversation
types real transcript turns, and those rows need a speaker that is neither
`agent` (a person's words, not Aisha's — read-aloud and the console turn counts
must skip them) nor `system` (they are bubbles in the thread, not muted
markers). See Speaker.staff in app/models.py.

POSTGRES ONLY. SAEnum compiles to a native enum type there, and a native enum
does not learn new values from create_all — it needs ALTER TYPE. On SQLite the
column is a VARCHAR and nothing is required.

Revision ID: 0df4c14ca33c
Revises: 2c6cebb4487b
Create Date: 2026-08-31

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0df4c14ca33c'
down_revision: Union[str, Sequence[str], None] = '2c6cebb4487b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name == "postgresql":
        # IF NOT EXISTS so a database that already picked the value up from a
        # reset-demo re-seed does not fail the migration run.
        op.execute("ALTER TYPE speaker ADD VALUE IF NOT EXISTS 'staff'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot drop a value from a native enum. A stray unused value is
    # harmless, so the downgrade deliberately leaves it in place.
    pass
