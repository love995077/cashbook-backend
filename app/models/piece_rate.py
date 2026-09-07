"""Work paid by output rather than by the day.

A tiler laying 120 sq ft at ₹18, a bar bender doing 400 kg at ₹4. The day
rate on `users` says nothing about what they earned; this row does.

EVERY LOG WRITES A WAGE
----------------------
A piece-rate log is not a second, parallel definition of what a worker is
owed. It creates the matching `Transaction` of type WAGE in the same database
transaction and keeps its id, exactly the way an approved `AdvanceRequest`
does. Without that the worker's balance, their own screen, the payroll
settlement and the PDF statement would all quietly ignore piece work — four
places disagreeing with a fifth.

`total_amount` is stored rather than recomputed on read: it is quantity x rate
at the moment the work was accepted, and neither number is safe to re-multiply
years later after a rate has been renegotiated.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.transaction import Transaction
    from app.models.user import User


class PieceRateLog(Base):
    __tablename__ = "piece_rate_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Where the work was done. Recorded on the row rather than read off the
    # worker, for the same reason attendance carries one: a worker moves
    # between sites and this has to keep saying where the tiles went down.
    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # "Tiling, second floor bedrooms". Required: quantity and rate alone do
    # not tell anyone what was measured.
    work_description: Mapped[str] = mapped_column(String(255), nullable=False)

    # Numeric(12, 3) like material quantities — piece work is measured in
    # 12.5 sq m and 0.75 running feet, not whole units.
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    # The WAGE row this log created. SET NULL rather than CASCADE: deleting
    # the ledger entry must not erase the record of the work that earned it.
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker: Mapped["User"] = relationship(back_populates="piece_rate_logs")
    site: Mapped[Optional["Site"]] = relationship(back_populates="piece_rate_logs")
    transaction: Mapped[Optional["Transaction"]] = relationship(
        foreign_keys=[transaction_id]
    )

    def __repr__(self) -> str:
        return (
            f"<PieceRateLog id={self.id} worker_id={self.worker_id} "
            f"quantity={self.quantity} total_amount={self.total_amount}>"
        )
