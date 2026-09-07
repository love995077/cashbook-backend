"""advance requests and attendance disputes

Two additions for worker self-service:

  advance_requests        a worker asking for money before payday, and the
                          contractor's answer. Approvals write a row into
                          `transactions` and point back at it here.

  attendance.is_disputed  a worker's flag that a day's record does not match
                          the day they worked. Backfilled false for every
                          existing row.

Revision ID: c3f8b2d7e194
Revises: a1c4f7d2b3e5
Create Date: 2026-09-04 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3f8b2d7e194'
down_revision: Union[str, Sequence[str], None] = 'a1c4f7d2b3e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'advance_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column(
            'status',
            sa.Enum('pending', 'approved', 'rejected', name='advance_request_status'),
            nullable=False,
        ),
        # The ledger row an approval created. SET NULL, not CASCADE: deleting
        # the transaction must not erase the record of the request.
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_advance_requests_worker_id'), 'advance_requests', ['worker_id'], unique=False
    )
    op.create_index(
        op.f('ix_advance_requests_status'), 'advance_requests', ['status'], unique=False
    )

    # server_default backfills the rows already in the table; it is dropped
    # immediately afterwards so the column matches the model, which sets the
    # default in Python.
    op.add_column(
        'attendance',
        sa.Column('is_disputed', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )
    # MySQL rewrites the whole column definition on ALTER, so the type and
    # nullability have to be restated or they would be lost.
    op.alter_column(
        'attendance',
        'is_disputed',
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=None,
    )
    op.create_index(
        op.f('ix_attendance_is_disputed'), 'attendance', ['is_disputed'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_attendance_is_disputed'), table_name='attendance')
    op.drop_column('attendance', 'is_disputed')
    op.drop_index(op.f('ix_advance_requests_status'), table_name='advance_requests')
    op.drop_index(op.f('ix_advance_requests_worker_id'), table_name='advance_requests')
    op.drop_table('advance_requests')
