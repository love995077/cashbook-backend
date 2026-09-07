from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import PaymentMode, TransactionType

if TYPE_CHECKING:
    from app.models.user import User


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        SAEnum(
            TransactionType,
            values_callable=lambda e: [m.value for m in e],
            name="transaction_type",
        ),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    # How the money moved. Null on a WAGE, which is an amount earned rather
    # than an amount handed over - there is nothing to pay it with yet. Also
    # null on rows written before this column existed.
    payment_mode: Mapped[Optional[PaymentMode]] = mapped_column(
        SAEnum(
            PaymentMode,
            values_callable=lambda e: [m.value for m in e],
            name="payment_mode",
        ),
        nullable=True,
    )

    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # A short recording attached to this row, as a storage key (never a URL —
    # see app/services/storage.py). Optional: most entries are typed.
    audio_note_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker: Mapped["User"] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} worker_id={self.worker_id} "
            f"type={self.type} amount={self.amount}>"
        )
