"""Small cash spent at a site, logged where it is spent.

Deliberately NOT a `CashEntry`. A cash entry belongs to the contractor's own
box and is filed under a category they maintain; petty cash is the fifty
rupees for an auto fare or the crew's afternoon tea, recorded by whoever is
standing on the site with the receipt in their hand. Forcing it through the
category picker would either bury it under "Miscellaneous" or grow the
category list by one bucket per shop.

Site-scoped rather than contractor-scoped, so a contractor running three jobs
can see what each one is actually leaking on top of materials and wages.
Ownership is proved through the site, the same way `site_photos` does it.
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
    from app.models.user import User


class PettyCash(Base):
    __tablename__ = "petty_cash"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Required, unlike the note on most rows here. An unlabelled ₹200 is the
    # one entry nobody can account for a month later, and it is exactly the
    # kind this table collects.
    description: Mapped[str] = mapped_column(String(255), nullable=False)

    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    # SET NULL, not CASCADE: removing a supervisor must not erase what the
    # site spent while they were running it.
    recorded_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="petty_cash")
    recorder: Mapped[Optional["User"]] = relationship(foreign_keys=[recorded_by])

    def __repr__(self) -> str:
        return (
            f"<PettyCash id={self.id} site_id={self.site_id} "
            f"amount={self.amount} date={self.date}>"
        )
