"""free-text worker post, optional phone and PIN

Three column changes on `users`, all of them widening:

  users.trade           ENUM(13 values) -> VARCHAR(50). A real crew turns up
                        with posts the enum does not have - shuttering
                        carpenter, chowkidar, bar-bending helper - and a post
                        that needs a migration before it can be typed gets
                        recorded as something else, or not at all.

                        Countability is preserved on the way in instead:
                        `_normalise_trade` in app/schemas/user.py lowercases
                        and underscores whatever is typed, so "Mason" and
                        "mason" remain one value. The enum's own values were
                        already in that form, so every existing row is valid
                        free text as it stands and NO data conversion is
                        needed - MySQL widens ENUM to VARCHAR by writing each
                        row's label, which is the string we already want.

  users.phone           NOT NULL -> NULL. The only thing required to put
                        someone on the crew is a name. The UNIQUE index stays:
                        MySQL permits any number of NULLs in one, so it keeps
                        doing its real job - no two accounts share a number -
                        without forcing one to exist.

  users.password_hash   NOT NULL -> NULL. No PIN means no sign-in, not a
                        rejected worker. app/routers/auth.py refuses a NULL
                        digest before it reaches bcrypt.

None of the three narrows anything, so no existing row can fail to migrate.

The downgrade is NOT lossless and will fail on data this migration made
possible - see the note on `downgrade`.

Revision ID: b7e2c9d41a83
Revises: 225a41e86ef9
Create Date: 2026-09-10 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.migration_support import enum_type, ensure_enum, is_postgres

# revision identifiers, used by Alembic.
revision: str = 'b7e2c9d41a83'
down_revision: Union[str, Sequence[str], None] = '225a41e86ef9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The enum as it stands in the live database, needed to put it back.
WORKER_TRADE = sa.Enum(
    'mason',
    'electrician',
    'plumber',
    'carpenter',
    'painter',
    'welder',
    'bar_bender',
    'plasterer',
    'tiler',
    'operator',
    'driver',
    'supervisor',
    'general_labour',
    name='worker_trade',
)


def upgrade() -> None:
    """Upgrade schema."""
    # ENUM -> VARCHAR. MySQL rewrites each row as its label, which is already
    # the normalised form the app now stores, so nothing needs converting.
    #
    # PostgreSQL will not cast an enum to text implicitly, so it is told how:
    # `trade::text` is the label, which is the same string MySQL writes. The
    # type is then dropped, because nothing references it any more.
    if is_postgres():
        op.alter_column(
            'users',
            'trade',
            existing_type=enum_type('worker_trade', *WORKER_TRADE.enums),
            type_=sa.String(length=50),
            existing_nullable=True,
            postgresql_using='trade::text',
        )
        op.execute('DROP TYPE IF EXISTS worker_trade')
    else:
        op.alter_column(
            'users',
            'trade',
            existing_type=WORKER_TRADE,
            type_=sa.String(length=50),
            existing_nullable=True,
        )

    # Dropping NOT NULL. The unique index on `phone` is left exactly as it is;
    # altering nullability does not disturb it.
    op.alter_column(
        'users',
        'phone',
        existing_type=sa.String(length=20),
        nullable=True,
    )

    op.alter_column(
        'users',
        'password_hash',
        existing_type=sa.String(length=60),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    Deliberately unguarded. Reinstating NOT NULL fails if any worker was booked
    without a phone number or a PIN, and narrowing back to the enum fails on
    any custom post - which is the whole point of the upgrade. Both are real
    data this schema can no longer hold, and silently deleting or defaulting it
    to make the downgrade succeed would be worse than the error.

    To go back, first decide what those workers and posts should become, write
    that decision as an UPDATE, then run this.
    """
    op.alter_column(
        'users',
        'password_hash',
        existing_type=sa.String(length=60),
        nullable=False,
    )
    op.alter_column(
        'users',
        'phone',
        existing_type=sa.String(length=20),
        nullable=False,
    )
    if is_postgres():
        ensure_enum('worker_trade', *WORKER_TRADE.enums)
        op.alter_column(
            'users',
            'trade',
            existing_type=sa.String(length=50),
            type_=enum_type('worker_trade', *WORKER_TRADE.enums),
            existing_nullable=True,
            postgresql_using='trade::worker_trade',
        )
    else:
        op.alter_column(
            'users',
            'trade',
            existing_type=sa.String(length=50),
            type_=WORKER_TRADE,
            existing_nullable=True,
        )
