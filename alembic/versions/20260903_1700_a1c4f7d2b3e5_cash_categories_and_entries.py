"""cash categories and entries

Adds the contractor's own cash box: customisable categories, and dated money
in and out filed against them. Worker wages are untouched - they stay in
`transactions`.

Revision ID: a1c4f7d2b3e5
Revises: 9e9af5998f7d
Create Date: 2026-09-03 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.migration_support import is_postgres

# revision identifiers, used by Alembic.
revision: str = 'a1c4f7d2b3e5'
down_revision: Union[str, Sequence[str], None] = '9e9af5998f7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'cash_categories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('contractor_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('direction', sa.Enum('in', 'out', name='cash_direction'), nullable=False),
        sa.Column('icon', sa.String(length=40), nullable=True),
        sa.Column('color', sa.String(length=9), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['contractor_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('contractor_id', 'name', name='uq_cash_categories_owner_name'),
    )
    op.create_index(
        op.f('ix_cash_categories_contractor_id'), 'cash_categories', ['contractor_id'], unique=False
    )
    op.create_index(
        op.f('ix_cash_categories_direction'), 'cash_categories', ['direction'], unique=False
    )

    op.create_table(
        'cash_entries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('contractor_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('direction', sa.Enum('in', 'out', name='cash_direction'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        # The mobile offline queue's idempotency key.
        sa.Column('client_ref', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['cash_categories.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['contractor_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_ref'),
    )
    op.create_index(
        op.f('ix_cash_entries_category_id'), 'cash_entries', ['category_id'], unique=False
    )
    op.create_index(
        op.f('ix_cash_entries_contractor_id'), 'cash_entries', ['contractor_id'], unique=False
    )
    op.create_index(op.f('ix_cash_entries_date'), 'cash_entries', ['date'], unique=False)
    op.create_index(
        op.f('ix_cash_entries_direction'), 'cash_entries', ['direction'], unique=False
    )
    op.create_index(op.f('ix_cash_entries_site_id'), 'cash_entries', ['site_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Tables are dropped without dropping their indexes first: MySQL refuses to
    drop an index that backs a foreign key ("needed in a foreign key
    constraint"), and dropping the table removes its indexes on every engine.
    """
    op.drop_table('cash_entries')
    op.drop_table('cash_categories')
    # A PostgreSQL enum outlives its table; left behind, it would make a
    # re-upgrade fail on "type already exists". No-op on MySQL.
    if is_postgres():
        op.execute('DROP TYPE IF EXISTS cash_direction')
