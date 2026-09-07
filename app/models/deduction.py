"""Recovering an advance a little at a time, instead of all at once.

THE PROBLEM THIS SOLVES
-----------------------
`balance = wages - advances - payments` subtracts an advance IN FULL the day
it is handed over. A worker on ₹600/day who takes ₹5,000 therefore shows a
negative balance for eight days and cannot be paid a rupee in that time,
which is precisely the situation an installment arrangement exists to avoid.

An `AutoDeduction` is the arrangement: the ₹5,000 still left the cash box and
is still an ADVANCE row in the ledger, but payroll stops holding the whole of
it against the current period and holds only the installments actually
recovered so far. The rest is carried as `balance_remaining` - a tracked debt
rather than a wall.

WHY TWO TABLES
--------------
`balance_remaining` on its own would be a mutable number with no history: no
way to answer "which days did you take ₹200 off me?", and no way to settle a
narrower period without guessing how much of the recovery fell inside it. So
each deduction taken is its own dated row in `deduction_installments`, and
`balance_remaining` is a rollup that can always be recomputed from them.

That also makes the whole thing replay-safe. Marking Tuesday's attendance
twice - a retried request, a second tap - must not take ₹400 off a worker who
worked one day, and the unique constraint on (deduction_id, date) is what
guarantees it rather than a convention every writer has to remember.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.transaction import Transaction
    from app.models.user import User


class AutoDeduction(Base):
    """One installment plan against one worker."""

    __tablename__ = "auto_deductions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    principal_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    daily_deduction: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # A rollup of the installments below, kept on the row so the app can show
    # "₹3,400 left" without summing a child table on every render. It is
    # derived, never authoritative - `recompute_balance` in
    # app/services/deductions.py is the one place allowed to move it.
    balance_remaining: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Cleared automatically when the balance reaches zero, and settable by
    # hand when a contractor writes the rest off. An inactive plan stops
    # collecting and stops affecting payroll, but keeps its history.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )

    # The ADVANCE this plan recovers. SET NULL rather than CASCADE, like
    # every other link to a ledger row here: deleting the transaction must
    # not erase the arrangement. Nullable also covers a plan opened against
    # an older advance, or against a debt that never had a row of its own.
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )

    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker: Mapped["User"] = relationship(back_populates="auto_deductions")
    transaction: Mapped[Optional["Transaction"]] = relationship(
        foreign_keys=[transaction_id]
    )
    installments: Mapped[list["DeductionInstallment"]] = relationship(
        back_populates="deduction",
        cascade="all, delete-orphan",
        order_by="DeductionInstallment.date",
    )

    @property
    def recovered_amount(self) -> Decimal:
        """How much of the principal has been taken back so far."""
        return self.principal_amount - self.balance_remaining

    def __repr__(self) -> str:
        return (
            f"<AutoDeduction id={self.id} worker_id={self.worker_id} "
            f"remaining={self.balance_remaining} active={self.is_active}>"
        )


class DeductionInstallment(Base):
    """One day's deduction actually taken off one plan."""

    __tablename__ = "deduction_installments"
    __table_args__ = (
        # The replay guard. One installment per plan per day, enforced by the
        # database rather than by every caller remembering to check.
        UniqueConstraint("deduction_id", "date", name="uq_deduction_installment_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    deduction_id: Mapped[int] = mapped_column(
        ForeignKey("auto_deductions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Usually `daily_deduction`, but the last installment of a plan is
    # whatever is left - ₹5,000 at ₹200 a day ends on a ₹200 that would
    # otherwise overshoot into a negative balance.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # The day worked, not the day the row was written. Payroll windows on
    # this, so a period settled later still recovers the right installments.
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    deduction: Mapped["AutoDeduction"] = relationship(back_populates="installments")

    def __repr__(self) -> str:
        return (
            f"<DeductionInstallment id={self.id} deduction_id={self.deduction_id} "
            f"amount={self.amount} date={self.date}>"
        )
