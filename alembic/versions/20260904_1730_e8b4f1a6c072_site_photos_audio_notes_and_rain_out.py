"""site photos, audio notes and rain-out attendance

Three changes:

  site_photos                  new table: one progress photo or material
                               receipt, filed against a site and a date.

  audio_note_path              new nullable column on transactions,
                               advance_requests and attendance. Holds a
                               storage key, not a URL - see
                               app/services/storage.py for why.

  attendance.status            the enum gains 'rain_out'.

THE ENUM CHANGE IS THE ONE TO WATCH
-----------------------------------
MySQL stores an ENUM's members in the column definition, so adding one is a
MODIFY COLUMN over the whole table. It is safe in the sense that no existing
row changes - 'rain_out' is appended after the current members, so the ordinal
of every stored value is untouched - but on a large attendance table it will
copy the table and hold a metadata lock while it does.

The downgrade rewrites any 'rain_out' row to 'absent' BEFORE narrowing the
enum. Without that step MySQL would silently coerce those rows to '' (or fail
in strict mode), and the fact that it rained would be lost rather than
approximated. It is a lossy downgrade either way, which is the honest price of
removing a status that has been used.

Revision ID: e8b4f1a6c072
Revises: d5a1e7c93b26
Create Date: 2026-09-04 17:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.migration_support import add_enum_value, is_postgres

# revision identifiers, used by Alembic.
revision: str = 'e8b4f1a6c072'
down_revision: Union[str, Sequence[str], None] = 'd5a1e7c93b26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_WITH_RAIN_OUT = sa.Enum(
    'present', 'half_day', 'absent', 'rain_out', name='attendance_status'
)
STATUS_WITHOUT_RAIN_OUT = sa.Enum(
    'present', 'half_day', 'absent', name='attendance_status'
)

# Every table that carries a voice note, and the column each one follows.
AUDIO_TABLES = ('transactions', 'advance_requests', 'attendance')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'site_photos',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        # Storage-relative key, e.g. "photos/7/2026/09/1f3c….jpg".
        sa.Column('file_path', sa.String(length=255), nullable=False),
        # The day the photo is ABOUT, not the day it was uploaded.
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.Column('content_type', sa.String(length=60), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        # SET NULL: removing a user must not delete the site's visual record.
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_site_photos_site_id'), 'site_photos', ['site_id'], unique=False)
    op.create_index(op.f('ix_site_photos_date'), 'site_photos', ['date'], unique=False)
    op.create_index(
        op.f('ix_site_photos_uploaded_by'), 'site_photos', ['uploaded_by'], unique=False
    )

    # Nullable, so no backfill and no server_default is needed.
    for table in AUDIO_TABLES:
        op.add_column(
            table, sa.Column('audio_note_path', sa.String(length=255), nullable=True)
        )

    if is_postgres():
        # The members live on the TYPE, not the column, so the column-level
        # MODIFY below would change nothing. Appending is the PostgreSQL
        # equivalent and, like the MySQL one, rewrites no existing row.
        add_enum_value('attendance_status', 'rain_out')
    else:
        op.alter_column(
            'attendance',
            'status',
            existing_type=STATUS_WITHOUT_RAIN_OUT,
            type_=STATUS_WITH_RAIN_OUT,
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Tables are dropped without dropping their indexes first: MySQL refuses to
    drop an index that backs a foreign key ("needed in a foreign key
    constraint"), and dropping the table removes its indexes on every engine.
    """
    # Lossy, and deliberately explicit about it: a rained-out day becomes a
    # plain absence rather than being silently blanked by the narrowing below.
    op.execute("UPDATE attendance SET status = 'absent' WHERE status = 'rain_out'")
    # PostgreSQL cannot drop a member from an enum type. The value is left on
    # the type, unused, which is harmless - no row holds it any more.
    if not is_postgres():
        op.alter_column(
            'attendance',
            'status',
            existing_type=STATUS_WITH_RAIN_OUT,
            type_=STATUS_WITHOUT_RAIN_OUT,
            existing_nullable=False,
        )

    for table in reversed(AUDIO_TABLES):
        op.drop_column(table, 'audio_note_path')

    op.drop_table('site_photos')
