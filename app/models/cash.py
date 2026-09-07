"""The contractor's own cash box: categorised money in and money out.

Worker wages live in `transactions` and are always tied to one worker. This
file covers everything that is not: the advance the site owner pays in, the
diesel bought on the way to the job, a month of scaffolding rental.

Two tables:

  CashCategory  the contractor's own list of buckets ("Fuel", "Site Advance").
                Seeded with sensible defaults on first use and editable after,
                which is what "customizable categories" means in practice.

  CashEntry     one dated amount in one of those buckets.

`direction` is stored on BOTH. On the category it is what the picker filters
by; on the entry it is a copy taken at write time, so renaming or deleting a
category later cannot silently flip a year of history from spend to income.
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
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import CashDirection

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.user import User


def _direction_column(**kwargs):
    return mapped_column(
        SAEnum(
            CashDirection,
            values_callable=lambda e: [m.value for m in e],
            name="cash_direction",
        ),
        nullable=False,
        **kwargs,
    )


class CashCategory(Base):
    """One spending or income bucket belonging to one contractor."""

    __tablename__ = "cash_categories"
    __table_args__ = (
        # Two "Fuel" buckets under one contractor would split the same spend
        # across two bars in the chart.
        UniqueConstraint("contractor_id", "name", name="uq_cash_categories_owner_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contractor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    direction: Mapped[CashDirection] = _direction_column(index=True)

    # An Ionicons name and a hex colour, so the app can draw the category
    # without keeping its own lookup table that drifts from the server's.
    icon: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(9), nullable=True)

    # True for the seeded starter set. Kept so the seeder can tell whether a
    # contractor has been set up before without counting rows.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Retired categories keep their history and drop out of the picker.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contractor: Mapped["User"] = relationship(foreign_keys=[contractor_id])
    entries: Mapped[list["CashEntry"]] = relationship(back_populates="category")

    def __repr__(self) -> str:
        return f"<CashCategory id={self.id} name={self.name!r} direction={self.direction}>"


class CashEntry(Base):
    """One dated amount of money in or out of the contractor's cash box."""

    __tablename__ = "cash_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contractor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL rather than CASCADE: deleting a category must not delete the
    # money that was spent under it. Such an entry still reports under its
    # stored direction, just without a label.
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cash_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    direction: Mapped[CashDirection] = _direction_column(index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The mobile app's offline queue key. UNIQUE, so replaying a queued write
    # whose response was lost returns the existing row instead of charging the
    # contractor twice — see mobile/src/offline/queue.ts.
    client_ref: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contractor: Mapped["User"] = relationship(foreign_keys=[contractor_id])
    category: Mapped[Optional["CashCategory"]] = relationship(back_populates="entries")
    site: Mapped[Optional["Site"]] = relationship()

    def __repr__(self) -> str:
        return (
            f"<CashEntry id={self.id} direction={self.direction} "
            f"amount={self.amount} date={self.date}>"
        )
