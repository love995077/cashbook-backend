"""ledger integrity: frozen day rates, idempotent money, dated deactivation

Three columns and, on PostgreSQL only, four indexes:

  attendance.wage_rate      NUMERIC(10,2) NULL. The day rate a day is worth,
                            frozen when it is marked. See below.
  transactions.client_ref   VARCHAR(64) NULL UNIQUE. The app's idempotency key,
                            as cash_entries.client_ref has always had.
  users.deactivated_on      DATE NULL. The first day a removed worker no
                            longer works here.

  lower(name) uniques       on sites, cash_categories, tools and
                            site_inventory - PostgreSQL only. See the end.

WHY attendance.wage_rate
------------------------
Every wage a day earned was recomputed from users.daily_wage whenever the day
was touched, and a resubmitted grid, a rain-out and a correction all touch
it. So a raise in October silently repriced every September day the next time
one of them was resubmitted - the opposite of "days already worked keep the
rate they were worked at". Freezing the rate on the day makes that true. See
app/services/wages.py.

BACKFILL - WHAT IS AND IS NOT INVENTED
--------------------------------------
  * A day with a linked WAGE gets the rate that WAGE was written at. Usually
    that is the worker's current rate (checked: rate x weight == the wage);
    where it is not, it is the wage divided back out, which is exact for a
    full day and within a paisa for an odd-paise half day.
  * A day belonging to a worker on PIECE RATE gets 0 - "not paid by the day".
    Their log pays them; giving the day a rate later would pay twice.
  * Every other day is left NULL - "no rate known". That includes every
    earning day marked before attendance earned wages at all. Nothing here
    invents a wage for those: `app/services/integrity.py` reports them, and
    `POST /contractor/integrity/wages/reconcile` prices them at the worker's
    current rate - showing the per-worker effect first, and linking a wage
    typed by hand for the same day instead of doubling it. That is a decision
    for whoever owns the books, not for a schema migration.

users.deactivated_on is left NULL for workers already inactive, which the app
reads as "no new days at all" - the strictest reading, and the one that cannot
put a wage on the books that was not there before.

THE CASE-INSENSITIVE INDEXES
----------------------------
MySQL's collation (utf8mb4_unicode_ci) compares strings without regard to
case, so its unique constraints already refused "Cement" beside "cement",
"Fuel" beside "fuel". PostgreSQL compares exactly, and moving the data there
would quietly drop that guarantee: two stock rows for one material, each
holding part of the count. A unique index on (owner, lower(name)) restores
it. MySQL needs nothing - its constraint already behaves this way.

They are expression indexes, which the models cannot declare portably, so
alembic/env.py leaves indexes ending in `_ci` out of autogenerate. They are
owned here.

Revision ID: 3b7d9e2a4c61
Revises: c4e8b2f71d95
Create Date: 2026-10-05 10:00:00.000000

"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.migration_support import is_postgres

revision: str = "3b7d9e2a4c61"
down_revision: Union[str, Sequence[str], None] = "c4e8b2f71d95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CLIENT_REF_UNIQUE = "uq_transactions_client_ref"

#: (index name, table, owner column, name column)
CI_INDEXES = (
    ("uq_sites_contractor_lower_name_ci", "sites", "contractor_id", "name"),
    ("uq_cash_categories_owner_lower_name_ci", "cash_categories", "contractor_id", "name"),
    ("uq_tools_site_lower_name_ci", "tools", "site_id", "name"),
    ("uq_site_inventory_site_lower_item_ci", "site_inventory", "site_id", "item_name"),
)

CENTS = Decimal("0.01")
WEIGHTS = {"present": Decimal("1"), "half_day": Decimal("0.5")}


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def upgrade() -> None:
    op.add_column(
        "attendance", sa.Column("wage_rate", sa.Numeric(precision=10, scale=2), nullable=True)
    )
    op.add_column("transactions", sa.Column("client_ref", sa.String(length=64), nullable=True))
    op.create_unique_constraint(CLIENT_REF_UNIQUE, "transactions", ["client_ref"])
    op.add_column("users", sa.Column("deactivated_on", sa.Date(), nullable=True))

    _backfill_wage_rates()

    if is_postgres():
        for name, table, owner, column in CI_INDEXES:
            op.execute(
                f"CREATE UNIQUE INDEX {name} ON {table} ({owner}, lower({column}))"
            )


def _backfill_wage_rates() -> None:
    """Give every day the rate it can be shown to have been worth. See above."""
    bind = op.get_bind()
    attendance = sa.table(
        "attendance",
        sa.column("id", sa.Integer),
        sa.column("worker_id", sa.Integer),
        sa.column("status", sa.String),
        sa.column("transaction_id", sa.Integer),
        sa.column("wage_rate", sa.Numeric(10, 2)),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("daily_wage", sa.Numeric(10, 2)),
        sa.column("wage_type", sa.String),
    )
    transactions = sa.table(
        "transactions",
        sa.column("id", sa.Integer),
        sa.column("amount", sa.Numeric(10, 2)),
    )

    rows = bind.execute(
        sa.select(
            attendance.c.id,
            attendance.c.status,
            attendance.c.transaction_id,
            users.c.daily_wage,
            users.c.wage_type,
            transactions.c.amount,
        )
        .select_from(
            attendance.join(users, users.c.id == attendance.c.worker_id).outerjoin(
                transactions, transactions.c.id == attendance.c.transaction_id
            )
        )
    ).all()

    updates: list[dict] = []
    for row_id, status, transaction_id, daily_wage, wage_type, amount in rows:
        # Enum columns come back as their label on both engines.
        status = getattr(status, "value", status)
        wage_type = getattr(wage_type, "value", wage_type)

        rate = None
        if transaction_id is not None and amount is not None and status in WEIGHTS:
            share = WEIGHTS[status]
            current = Decimal(daily_wage) if daily_wage is not None else None
            if current is not None and _money(current * share) == Decimal(amount):
                rate = _money(current)
            else:
                rate = _money(Decimal(amount) / share)
        elif wage_type == "piece_rate":
            rate = Decimal("0.00")

        if rate is not None:
            updates.append({"row_id": row_id, "rate": rate})

    if updates:
        bind.execute(
            attendance.update()
            .where(attendance.c.id == sa.bindparam("row_id"))
            .values(wage_rate=sa.bindparam("rate")),
            updates,
        )


def downgrade() -> None:
    if is_postgres():
        for name, _table, _owner, _column in CI_INDEXES:
            op.execute(f"DROP INDEX IF EXISTS {name}")
    op.drop_column("users", "deactivated_on")
    op.drop_constraint(CLIENT_REF_UNIQUE, "transactions", type_="unique")
    op.drop_column("transactions", "client_ref")
    op.drop_column("attendance", "wage_rate")
