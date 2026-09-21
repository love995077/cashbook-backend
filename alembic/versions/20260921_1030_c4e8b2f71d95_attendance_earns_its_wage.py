"""attendance earns its wage

One nullable column and one foreign key on `attendance`:

  attendance.transaction_id   -> transactions.id, ON DELETE SET NULL, UNIQUE

WHY
---
Money lives in `transactions`. Every figure a contractor reads - what is still
owed on Home, `net_payable` on the settlement screen, a worker's own balance -
is built by summing WAGE rows.

A piece-rate worker earned theirs: the log writes a WAGE beside itself. A
worker on a DAY RATE earned nothing at all. Marking them present wrote an
`attendance` row and stopped, so a crew could work a full week and the app
would report that they were owed zero. That is the "worker to pay is wrong"
bug, and it is not an arithmetic error anywhere - it is two systems that never
met.

This column is the join. Marking a day writes the day's wage and links it
here, so correcting the day to absent can take exactly that wage back and
unmarking the day can remove it - see app/services/wages.py.

WHY A STORED LINK RATHER THAN A LOOKUP
--------------------------------------
Matching a wage by (worker_id, date) would be indistinguishable from one the
contractor typed by hand on the same day, so editing attendance could silently
delete money somebody entered deliberately. The explicit link is the only
thing that says "this row created that row".

UNIQUE because one day earns one wage. Two attendance rows pointing at the
same transaction would mean one payment counted for two days, and the
constraint makes that unrepresentable rather than merely unlikely.

SET NULL rather than CASCADE: deleting a wage must not delete the record that
somebody turned up. (`DELETE /contractor/transactions/{id}` refuses an
attendance-backed wage outright, so this is the backstop, not the path.)

EXISTING DATA
-------------
Every existing row gets NULL, which reads as "this day earned nothing" and is
exactly true of history written before wages were linked. Nothing is
back-filled on purpose: inventing wages for past days would move balances the
contractor has already settled and paid against, and a wrong number in the
books is worse than an absent one. Days marked from here on earn normally; to
put an old day on the books, correct it in the app and it earns as it saves.

The column is nullable and unconstrained-by-default, so no existing row can
fail to migrate.

Revision ID: c4e8b2f71d95
Revises: b7e2c9d41a83
Create Date: 2026-09-21 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8b2f71d95"
down_revision: Union[str, Sequence[str], None] = "b7e2c9d41a83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "fk_attendance_transaction_id"
UNIQUE = "uq_attendance_transaction_id"


def upgrade() -> None:
    op.add_column(
        "attendance",
        sa.Column("transaction_id", sa.Integer(), nullable=True),
    )
    # Named explicitly rather than left to the dialect's convention, so the
    # downgrade can drop it by name on any backend.
    op.create_unique_constraint(UNIQUE, "attendance", ["transaction_id"])
    op.create_foreign_key(
        CONSTRAINT,
        "attendance",
        "transactions",
        ["transaction_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop the link.

    Lossless in the sense that matters - no wage is deleted, and `attendance`
    and `transactions` both survive intact. What is lost is only the knowledge
    of WHICH wage a given day earned, so re-upgrading leaves older days
    unlinked and a later correction to one of them writes a second wage beside
    the first. Reconcile before re-applying if a downgrade ever runs against
    data written after the upgrade.
    """
    op.drop_constraint(CONSTRAINT, "attendance", type_="foreignkey")
    op.drop_constraint(UNIQUE, "attendance", type_="unique")
    op.drop_column("attendance", "transaction_id")
