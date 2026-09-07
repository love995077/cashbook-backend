"""worker trade and payment mode

Two columns, both nullable, both added to tables that already hold rows:

  users.trade                 what a worker does on site. Left NULL for the
                              existing crew rather than backfilled with
                              "general labour", which would be asserting
                              something nobody recorded.

  transactions.payment_mode   how money changed hands. Left NULL on history,
                              and stays NULL on wages, which are earned rather
                              than handed over.

Revision ID: d5a1e7c93b26
Revises: c3f8b2d7e194
Create Date: 2026-09-04 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5a1e7c93b26'
down_revision: Union[str, Sequence[str], None] = 'c3f8b2d7e194'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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

PAYMENT_MODE = sa.Enum('cash', 'upi', 'bank_transfer', name='payment_mode')


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, so no server_default and no backfill is needed: every existing
    # row simply reads as "not recorded".
    op.add_column('users', sa.Column('trade', WORKER_TRADE, nullable=True))
    op.create_index(op.f('ix_users_trade'), 'users', ['trade'], unique=False)

    op.add_column(
        'transactions', sa.Column('payment_mode', PAYMENT_MODE, nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transactions', 'payment_mode')
    op.drop_index(op.f('ix_users_trade'), table_name='users')
    op.drop_column('users', 'trade')
