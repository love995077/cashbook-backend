"""initial schema with sites and materials

Revision ID: 9e9af5998f7d
Revises: 
Create Date: 2026-09-03 15:53:28.424443

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.migration_support import is_postgres


# revision identifiers, used by Alembic.
revision: str = '9e9af5998f7d'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('password_hash', sa.String(length=60), nullable=False),
    sa.Column('role', sa.Enum('worker', 'contractor', name='user_role'), nullable=False),
    sa.Column('daily_wage', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('contractor_id', sa.Integer(), nullable=True),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contractor_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_contractor_id'), 'users', ['contractor_id'], unique=False)
    op.create_index(op.f('ix_users_phone'), 'users', ['phone'], unique=True)
    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    op.create_index(op.f('ix_users_site_id'), 'users', ['site_id'], unique=False)

    op.create_table('sites',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('contractor_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('address', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contractor_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contractor_id', 'name', name='uq_sites_contractor_name')
    )
    op.create_index(op.f('ix_sites_contractor_id'), 'sites', ['contractor_id'], unique=False)

    # users.site_id and sites.contractor_id reference each other, so the
    # constraint cannot be declared inside either CREATE TABLE — whichever ran
    # first would point at a table that does not exist yet. Both tables exist
    # by this point, so the loop is closed with an ALTER.
    op.create_foreign_key(
        'fk_users_site_id_sites', 'users', 'sites', ['site_id'], ['id'], ondelete='SET NULL'
    )
    op.create_table('attendance',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('worker_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.Enum('present', 'half_day', 'absent', name='attendance_status'), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('worker_id', 'date', name='uq_attendance_worker_date')
    )
    op.create_index(op.f('ix_attendance_date'), 'attendance', ['date'], unique=False)
    op.create_index(op.f('ix_attendance_site_id'), 'attendance', ['site_id'], unique=False)
    op.create_index(op.f('ix_attendance_worker_id'), 'attendance', ['worker_id'], unique=False)
    op.create_table('site_materials',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('unit', sa.Enum('bag', 'ton', 'kg', 'litre', 'piece', 'cubic_feet', 'cubic_metre', 'brass', 'square_feet', 'running_feet', name='material_unit'), nullable=False),
    sa.Column('notes', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('site_id', 'name', name='uq_site_materials_site_name')
    )
    op.create_index(op.f('ix_site_materials_site_id'), 'site_materials', ['site_id'], unique=False)
    op.create_table('transactions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('worker_id', sa.Integer(), nullable=False),
    sa.Column('type', sa.Enum('wage', 'advance', 'payment', name='transaction_type'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['worker_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_transactions_date'), 'transactions', ['date'], unique=False)
    op.create_index(op.f('ix_transactions_type'), 'transactions', ['type'], unique=False)
    op.create_index(op.f('ix_transactions_worker_id'), 'transactions', ['worker_id'], unique=False)
    op.create_table('material_logs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=False),
    sa.Column('type', sa.Enum('consumption', 'delivery', name='material_log_type'), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('notes', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['site_materials.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_material_logs_date'), 'material_logs', ['date'], unique=False)
    op.create_index(op.f('ix_material_logs_material_id'), 'material_logs', ['material_id'], unique=False)
    op.create_index(op.f('ix_material_logs_site_id'), 'material_logs', ['site_id'], unique=False)
    op.create_index(op.f('ix_material_logs_type'), 'material_logs', ['type'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema.

    The autogenerated version dropped every index before its table. MySQL
    refuses that — an index backing a foreign key cannot be dropped while the
    constraint exists ("needed in a foreign key constraint"). Dropping the
    table takes its indexes with it, so the explicit drop_index calls are both
    unnecessary and wrong here.

    Order is the reverse of creation, and the users <-> sites cycle is broken
    first.
    """
    op.drop_constraint('fk_users_site_id_sites', 'users', type_='foreignkey')
    op.drop_table('material_logs')
    op.drop_table('transactions')
    op.drop_table('site_materials')
    op.drop_table('attendance')
    # sites.contractor_id references users, so sites goes first.
    op.drop_table('sites')
    op.drop_table('users')
    # A PostgreSQL enum outlives its table; left behind, it would make a
    # re-upgrade fail on "type already exists". No-op on MySQL.
    if is_postgres():
        op.execute('DROP TYPE IF EXISTS material_log_type')
        op.execute('DROP TYPE IF EXISTS transaction_type')
        op.execute('DROP TYPE IF EXISTS material_unit')
        op.execute('DROP TYPE IF EXISTS attendance_status')
        op.execute('DROP TYPE IF EXISTS user_role')
