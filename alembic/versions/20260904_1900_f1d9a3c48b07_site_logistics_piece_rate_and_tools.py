"""site logistics, piece-rate payroll, installments and tool tracking

Phase 10. One column and eight tables:

  users.wage_type              new NOT NULL enum, defaulted to 'daily'.

  petty_cash                   small cash spent at a site — fares, tea, a
                               packet of nails. Kept out of `cash_entries`
                               so it does not either bury itself under
                               "Miscellaneous" or grow the contractor's
                               category list by one bucket per shop.

  piece_rate_logs              work paid by output. Every row also writes a
                               WAGE into `transactions` and keeps its id, so
                               the balance, payroll and the PDF statement all
                               see piece work.

  auto_deductions              an advance being recovered day by day.
  deduction_installments       the days it was actually recovered on.

  tools                        what a site owns.
  tool_assignments             who has one, until it comes back.

  site_inventory               live stock at a site.
  material_receipts            the deliveries that put it there, with the
                               challan photo.

THE COLUMN IS THE ONE TO WATCH
------------------------------
`users.wage_type` is NOT NULL on a table that already has rows, so it needs a
default that is true of all of them. 'daily' is: every worker on every crew
was being paid by the day when this ran, because there was no other way to be
paid. That is a fact about them rather than a convenient guess, which is why
this backfills silently instead of asking.

MySQL stores an ENUM in the column definition, so this is an ADD COLUMN with
a DEFAULT — one metadata lock and one table copy on a large `users`. That is
the whole cost; every other change here creates a new table and touches
nothing existing.

WHAT THIS DOES NOT TOUCH
------------------------
`site_materials` and `material_logs` cover the same ground as
`site_inventory` / `material_receipts` from the other direction — a catalogue
plus a signed movement log, from which stock is derived. They have never been
wired to an endpoint and are deliberately LEFT ALONE here rather than dropped:
removing tables is not this migration's call to make. See the module docstring
in app/models/inventory.py for why running both is a hazard worth closing.

Revision ID: f1d9a3c48b07
Revises: e8b4f1a6c072
Create Date: 2026-09-04 19:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1d9a3c48b07'
down_revision: Union[str, Sequence[str], None] = 'e8b4f1a6c072'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WAGE_TYPE = sa.Enum('daily', 'piece_rate', name='wage_type')
TOOL_STATUS = sa.Enum('out', 'returned', 'lost', name='tool_assignment_status')

# Must match MaterialUnit in app/models/enums.py, member for member. The
# existing `site_materials.unit` column already carries this type; MySQL
# stores enums per column rather than as a shared type, so repeating it here
# creates nothing and collides with nothing.
MATERIAL_UNIT = sa.Enum(
    'bag',
    'ton',
    'kg',
    'litre',
    'piece',
    'cubic_feet',
    'cubic_metre',
    'brass',
    'square_feet',
    'running_feet',
    name='material_unit',
)

NOW = sa.text('now()')


def _timestamps() -> list[sa.Column]:
    """created_at / updated_at, as every other table in this schema has them."""
    return [
        sa.Column('created_at', sa.DateTime(), server_default=NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=NOW, nullable=False),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    # --- how a worker is paid ------------------------------------------------
    op.add_column(
        'users',
        sa.Column(
            'wage_type',
            WAGE_TYPE,
            nullable=False,
            # Backfills every existing row. See the note above on why 'daily'
            # is a fact rather than a guess.
            server_default='daily',
        ),
    )
    op.create_index(op.f('ix_users_wage_type'), 'users', ['wage_type'], unique=False)

    # --- petty cash ----------------------------------------------------------
    op.create_table(
        'petty_cash',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        # NOT NULL, unlike most notes here: an unlabelled ₹200 is the entry
        # nobody can account for a month later, and this table collects those.
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('recorded_by', sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        # SET NULL: removing a supervisor must not erase what the site spent
        # while they were running it.
        sa.ForeignKeyConstraint(['recorded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_petty_cash_site_id'), 'petty_cash', ['site_id'])
    op.create_index(op.f('ix_petty_cash_date'), 'petty_cash', ['date'])
    op.create_index(op.f('ix_petty_cash_recorded_by'), 'petty_cash', ['recorded_by'])

    # --- piece-rate work -----------------------------------------------------
    op.create_table(
        'piece_rate_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('work_description', sa.String(length=255), nullable=False),
        # (12, 3) like material quantities: piece work is measured in 12.5 sq m
        # and 0.75 running feet, not whole units.
        sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('rate', sa.Numeric(precision=10, scale=2), nullable=False),
        # Stored rather than recomputed: quantity x rate at the moment the work
        # was accepted, which is not safe to re-multiply after a renegotiation.
        sa.Column('total_amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        # SET NULL: deleting the ledger entry must not erase the record of the
        # work that earned it.
        sa.ForeignKeyConstraint(
            ['transaction_id'], ['transactions.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_piece_rate_logs_worker_id'), 'piece_rate_logs', ['worker_id'])
    op.create_index(op.f('ix_piece_rate_logs_site_id'), 'piece_rate_logs', ['site_id'])
    op.create_index(op.f('ix_piece_rate_logs_date'), 'piece_rate_logs', ['date'])

    # --- installment plans ---------------------------------------------------
    op.create_table(
        'auto_deductions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('principal_amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('daily_deduction', sa.Numeric(precision=10, scale=2), nullable=False),
        # A rollup of deduction_installments, kept here so the app can show
        # "₹3,400 left" without summing a child table on every render.
        sa.Column('balance_remaining', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['transaction_id'], ['transactions.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_auto_deductions_worker_id'), 'auto_deductions', ['worker_id'])
    op.create_index(op.f('ix_auto_deductions_is_active'), 'auto_deductions', ['is_active'])

    op.create_table(
        'deduction_installments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('deduction_id', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        # The day WORKED, not the day the row was written: payroll windows on
        # this, so a period settled later still recovers the right days.
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ['deduction_id'], ['auto_deductions.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # The replay guard. Marking Tuesday twice must not take ₹400 off a
        # worker who worked one day, and this is what guarantees it rather
        # than a convention every caller has to remember.
        sa.UniqueConstraint('deduction_id', 'date', name='uq_deduction_installment_day'),
    )
    op.create_index(
        op.f('ix_deduction_installments_deduction_id'),
        'deduction_installments',
        ['deduction_id'],
    )
    op.create_index(
        op.f('ix_deduction_installments_worker_id'),
        'deduction_installments',
        ['worker_id'],
    )
    op.create_index(
        op.f('ix_deduction_installments_date'), 'deduction_installments', ['date']
    )

    # --- tools ---------------------------------------------------------------
    op.create_table(
        'tools',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        # Three identical drills are one row with a count of 3: nobody labels
        # them, and the only question asked is "is one free".
        sa.Column('inventory_count', sa.Integer(), nullable=False),
        sa.Column('notes', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'name', name='uq_tools_site_name'),
    )
    op.create_index(op.f('ix_tools_site_id'), 'tools', ['site_id'])

    op.create_table(
        'tool_assignments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tool_id', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        # Timestamps, not dates: a tool out at 9am and back at 4pm is the whole
        # point, and a date would flatten both ends of that to "today".
        sa.Column('checkout_time', sa.DateTime(), server_default=NOW, nullable=False),
        sa.Column('return_time', sa.DateTime(), nullable=True),
        sa.Column('status', TOOL_STATUS, nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['tool_id'], ['tools.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tool_assignments_tool_id'), 'tool_assignments', ['tool_id'])
    op.create_index(
        op.f('ix_tool_assignments_worker_id'), 'tool_assignments', ['worker_id']
    )
    op.create_index(
        op.f('ix_tool_assignments_checkout_time'), 'tool_assignments', ['checkout_time']
    )
    op.create_index(op.f('ix_tool_assignments_status'), 'tool_assignments', ['status'])

    # --- live stock and deliveries -------------------------------------------
    op.create_table(
        'site_inventory',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('item_name', sa.String(length=120), nullable=False),
        sa.Column(
            'quantity_in_stock',
            sa.Numeric(precision=12, scale=3),
            server_default='0',
            nullable=False,
        ),
        sa.Column('unit', MATERIAL_UNIT, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # The upsert key. Two "Cement" rows at one site would each hold half
        # the stock and neither would answer "how many bags are left".
        sa.UniqueConstraint('site_id', 'item_name', name='uq_site_inventory_site_item'),
    )
    op.create_index(op.f('ix_site_inventory_site_id'), 'site_inventory', ['site_id'])

    op.create_table(
        'material_receipts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        # Copied off the inventory row rather than only referenced, so a
        # receipt still says what turned up if the item is later renamed.
        sa.Column('item_name', sa.String(length=120), nullable=False),
        sa.Column(
            'quantity_received', sa.Numeric(precision=12, scale=3), nullable=False
        ),
        sa.Column('unit', MATERIAL_UNIT, nullable=False),
        sa.Column('inventory_id', sa.Integer(), nullable=True),
        # A storage KEY, not a URL — see app/services/storage.py. The public
        # link is derived at read time and surfaces as `challan_image_url`.
        sa.Column('challan_image_path', sa.String(length=255), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('supplier', sa.String(length=120), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('received_by', sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        # SET NULL: clearing the stock list at the end of a job must not delete
        # the record of what was delivered to it.
        sa.ForeignKeyConstraint(
            ['inventory_id'], ['site_inventory.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['received_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_material_receipts_site_id'), 'material_receipts', ['site_id'])
    op.create_index(op.f('ix_material_receipts_date'), 'material_receipts', ['date'])
    op.create_index(
        op.f('ix_material_receipts_inventory_id'), 'material_receipts', ['inventory_id']
    )
    op.create_index(
        op.f('ix_material_receipts_received_by'), 'material_receipts', ['received_by']
    )


def downgrade() -> None:
    """Downgrade schema.

    Lossy in one place worth naming: dropping `piece_rate_logs` leaves the
    WAGE transactions those logs created standing in the ledger, with their
    note as the only remaining explanation of where the amount came from.
    That is the right way round — deleting a worker's wages to undo a schema
    change would be far worse — but the rows will read as unexplained wages.
    """
    op.drop_index(op.f('ix_material_receipts_received_by'), table_name='material_receipts')
    op.drop_index(op.f('ix_material_receipts_inventory_id'), table_name='material_receipts')
    op.drop_index(op.f('ix_material_receipts_date'), table_name='material_receipts')
    op.drop_index(op.f('ix_material_receipts_site_id'), table_name='material_receipts')
    op.drop_table('material_receipts')

    op.drop_index(op.f('ix_site_inventory_site_id'), table_name='site_inventory')
    op.drop_table('site_inventory')

    op.drop_index(op.f('ix_tool_assignments_status'), table_name='tool_assignments')
    op.drop_index(op.f('ix_tool_assignments_checkout_time'), table_name='tool_assignments')
    op.drop_index(op.f('ix_tool_assignments_worker_id'), table_name='tool_assignments')
    op.drop_index(op.f('ix_tool_assignments_tool_id'), table_name='tool_assignments')
    op.drop_table('tool_assignments')

    op.drop_index(op.f('ix_tools_site_id'), table_name='tools')
    op.drop_table('tools')

    op.drop_index(op.f('ix_deduction_installments_date'), table_name='deduction_installments')
    op.drop_index(
        op.f('ix_deduction_installments_worker_id'), table_name='deduction_installments'
    )
    op.drop_index(
        op.f('ix_deduction_installments_deduction_id'), table_name='deduction_installments'
    )
    op.drop_table('deduction_installments')

    op.drop_index(op.f('ix_auto_deductions_is_active'), table_name='auto_deductions')
    op.drop_index(op.f('ix_auto_deductions_worker_id'), table_name='auto_deductions')
    op.drop_table('auto_deductions')

    op.drop_index(op.f('ix_piece_rate_logs_date'), table_name='piece_rate_logs')
    op.drop_index(op.f('ix_piece_rate_logs_site_id'), table_name='piece_rate_logs')
    op.drop_index(op.f('ix_piece_rate_logs_worker_id'), table_name='piece_rate_logs')
    op.drop_table('piece_rate_logs')

    op.drop_index(op.f('ix_petty_cash_recorded_by'), table_name='petty_cash')
    op.drop_index(op.f('ix_petty_cash_date'), table_name='petty_cash')
    op.drop_index(op.f('ix_petty_cash_site_id'), table_name='petty_cash')
    op.drop_table('petty_cash')

    op.drop_index(op.f('ix_users_wage_type'), table_name='users')
    op.drop_column('users', 'wage_type')
