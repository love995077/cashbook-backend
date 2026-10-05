"""attendance shift (day/night)

  attendance.shift    new NOT NULL enum, defaulted to 'day'.

WHY A COLUMN AND NOT AN ENUM VALUE
----------------------------------
The obvious-looking move is to add NIGHT to `attendance_status` alongside
PRESENT / HALF_DAY / ABSENT / RAIN_OUT. It breaks the ledger. `days_worked`
scores PRESENT as 1 and HALF_DAY as 0.5; a NIGHT status would have no defined
weight, and a half day worked at night could not be expressed at all.

Status answers "how much of a day was worked". Shift answers "when". They are
independent axes and this keeps them that way. Both shifts are paid at the
same standard daily rate, so no payroll arithmetic changes.

BACKFILL
--------
NOT NULL on a table that already has rows, so `server_default='day'` fills
them. Unlike `users.wage_type` the default is KEPT rather than dropped: the
model declares the same server_default, and a shift that is simply not
recorded is meaningfully a day shift rather than a missing value.

Revision ID: 0fba0f954fb6
Revises: f1d9a3c48b07
Create Date: 2026-09-05 11:50:41.797639

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.migration_support import enum_type, ensure_enum, is_postgres


# revision identifiers, used by Alembic.
revision: str = '0fba0f954fb6'
down_revision: Union[str, Sequence[str], None] = 'f1d9a3c48b07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # add_column never creates a PostgreSQL enum type. No-op on MySQL.
    ensure_enum('work_shift', 'day', 'night')
    op.add_column(
        'attendance',
        sa.Column(
            'shift',
            enum_type('work_shift', 'day', 'night'),
            # Backfills every existing row. Every day marked before this
            # column existed was worked in daylight - a fact, not a guess.
            server_default='day',
            nullable=False,
        ),
    )
    # The roster view filters on it.
    op.create_index(op.f('ix_attendance_shift'), 'attendance', ['shift'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Lossy: which rows were night shifts is not recoverable afterwards.
    op.drop_index(op.f('ix_attendance_shift'), table_name='attendance')
    op.drop_column('attendance', 'shift')
    if is_postgres():
        op.execute('DROP TYPE IF EXISTS work_shift')
