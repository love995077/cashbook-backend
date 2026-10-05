"""site-first ledger: every money row says which site it belongs to

  transactions.site_id    INTEGER NULL -> sites.id ON DELETE SET NULL, indexed

and two backfills, so the site reports start out complete rather than empty.

WHY
---
The app is moving to a site-first layout: pick a site, and its labour, its
expenses and its reports are that site's alone. Expenses already carried a
site (`cash_entries.site_id`); wages, advances and payments did not, so the
only way to put one on a site's report was to look at where the worker is
posted TODAY. That moves a mason's whole history to whichever job they are on
now and drops it from every report the moment they are deactivated - exactly
what a site report must not do.

BACKFILL, MOST SPECIFIC EVIDENCE FIRST
--------------------------------------
  1. attendance.site_id, where it is NULL, from the worker's current posting.
     Rows written by the single-mark route (which is also what the offline
     queue replays) never recorded a site. Where the worker is posted now is
     the best evidence there is of where those days were worked.
  2. A day's WAGE takes its attendance row's site.
  3. A piece-rate WAGE takes its log's site.
  4. Anything else - advances, payments, hand-typed wages - takes the
     worker's current posting.

Each step only fills NULLs, so re-running it is harmless, and nothing that
already says where it happened is overwritten.

Revision ID: 8f2a6c1d9e47
Revises: 3b7d9e2a4c61
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8f2a6c1d9e47"
down_revision: Union[str, Sequence[str], None] = "3b7d9e2a4c61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK = "fk_transactions_site_id_sites"


def upgrade() -> None:
    op.add_column("transactions", sa.Column("site_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_transactions_site_id"), "transactions", ["site_id"], unique=False)
    op.create_foreign_key(FK, "transactions", "sites", ["site_id"], ["id"], ondelete="SET NULL")

    # Correlated subqueries rather than UPDATE ... JOIN / UPDATE ... FROM: the
    # one form both MySQL and PostgreSQL accept.
    op.execute(
        """
        UPDATE attendance
           SET site_id = (SELECT u.site_id FROM users u WHERE u.id = attendance.worker_id)
         WHERE site_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE transactions
           SET site_id = (SELECT a.site_id FROM attendance a
                           WHERE a.transaction_id = transactions.id)
         WHERE site_id IS NULL
           AND id IN (SELECT a2.transaction_id FROM attendance a2
                       WHERE a2.transaction_id IS NOT NULL)
        """
    )
    op.execute(
        """
        UPDATE transactions
           SET site_id = (SELECT MIN(p.site_id) FROM piece_rate_logs p
                           WHERE p.transaction_id = transactions.id)
         WHERE site_id IS NULL
           AND id IN (SELECT p2.transaction_id FROM piece_rate_logs p2
                       WHERE p2.transaction_id IS NOT NULL)
        """
    )
    op.execute(
        """
        UPDATE transactions
           SET site_id = (SELECT u.site_id FROM users u WHERE u.id = transactions.worker_id)
         WHERE site_id IS NULL
        """
    )


def downgrade() -> None:
    """Drop the column. The attendance site backfill is left in place: it only
    filled rows that said nothing, and saying where a day was worked is not
    something the older schema objected to."""
    op.drop_constraint(FK, "transactions", type_="foreignkey")
    op.drop_index(op.f("ix_transactions_site_id"), table_name="transactions")
    op.drop_column("transactions", "site_id")
