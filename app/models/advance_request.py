"""A worker asking for money before payday, and the contractor's answer.

Kept separate from `transactions` on purpose. A transaction is money that has
moved; a request is a conversation that may end in nothing. Folding the two
together would either put un-approved amounts into the balance or lose the
record of what was refused.

When a request is approved the contractor's endpoint writes the matching
`Transaction` and stores its id here, so the ledger row and the request that
caused it can always be traced to each other.
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import AdvanceRequestStatus

if TYPE_CHECKING:
    from app.models.transaction import Transaction
    from app.models.user import User


class AdvanceRequest(Base):
    __tablename__ = "advance_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Free text, and optional: a worker on a noisy site should not be blocked
    # from asking because they cannot type an explanation.
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # A short recording attached to this row, as a storage key (never a URL —
    # see app/services/storage.py). Optional: most entries are typed.
    audio_note_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[AdvanceRequestStatus] = mapped_column(
        SAEnum(
            AdvanceRequestStatus,
            values_callable=lambda e: [m.value for m in e],
            name="advance_request_status",
        ),
        nullable=False,
        default=AdvanceRequestStatus.PENDING,
        index=True,
    )

    # The ledger row an approval created. SET NULL rather than CASCADE:
    # deleting the transaction must not erase the record of the request.
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    # When the contractor answered. Null while pending.
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker: Mapped["User"] = relationship(
        back_populates="advance_requests", foreign_keys=[worker_id]
    )
    transaction: Mapped[Optional["Transaction"]] = relationship(
        foreign_keys=[transaction_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AdvanceRequest id={self.id} worker_id={self.worker_id} "
            f"amount={self.amount} status={self.status}>"
        )
