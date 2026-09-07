"""Plant and the diesel it drinks.

A mixer, a JCB, a hoist. The cost that surprises a contractor at the end of a
job is rarely the wages — those are counted every day — it is the fuel nobody
wrote down.

WHY FUEL COST IS STORED RATHER THAN DERIVED
-------------------------------------------
`diesel_litres * rate` would need a rate, and the rate changes between
deliveries and between pumps. What the site actually knows is the number on
the receipt, so that is the number kept. Litres are recorded alongside it
because they are what tells you whether a machine is drinking more than it
should — a rising cost could be either the machine or the market, and only
the litres separate the two.
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


class MachineryLog(Base):
    """One machine, one day, on one site."""

    __tablename__ = "machinery_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Free text, unlike MaterialUnit: a contractor hires whatever is available
    # that week, and an enum of machine names would need a migration per hire.
    machine_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Numeric(6, 2): a machine runs 7.5 hours, not 7. Four digits before the
    # point is a decade of continuous running - comfortably beyond any day.
    hours_operated: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    diesel_litres: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    # What the receipt says. See the module docstring for why this is stored
    # rather than computed from a rate.
    fuel_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), server_default="0", nullable=False
    )

    # Storage key for the photographed diesel receipt, never a URL — see
    # app/services/storage.py.
    receipt_image_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # SET NULL: removing the supervisor must not erase what the site burned
    # while they were running it.
    logged_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="machinery_logs")
    logger: Mapped[Optional["User"]] = relationship(foreign_keys=[logged_by])

    def __repr__(self) -> str:
        return (
            f"<MachineryLog id={self.id} machine_name={self.machine_name!r} "
            f"date={self.date} fuel_cost={self.fuel_cost}>"
        )
