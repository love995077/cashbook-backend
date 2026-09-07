"""Live stock at a site, and the deliveries that put it there.

`SiteInventory` is what is on the ground right now — 40 bags of cement. Every
`MaterialReceipt` adds to it in the same database transaction, so the two can
never disagree about what arrived.

RELATIONSHIP TO site_materials / material_logs
----------------------------------------------
Those two tables cover the same ground from the other direction: a catalogue
plus a signed movement log, from which stock is DERIVED. They have never been
wired to an endpoint. This pair is what the Goods Receipt Note flow writes to,
and it keeps the running total on the row because the app asks "how much is
left" far more often than it asks "how did it get here".

Running both is a genuine hazard: stock recorded through one is invisible to
the other. If material_logs is ever given endpoints, one of the two has to
become the source of truth and the other a view of it.

THE CHALLAN IS THE POINT
------------------------
A GRN nobody photographed is a number somebody typed. `challan_image_path` is
a storage key, not a URL — see app/services/storage.py for why that
distinction is load-bearing. The public link is derived at read time and
surfaces as `challan_image_url` on the schema.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
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
from app.models.enums import MaterialUnit

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.user import User


def _unit_column(**kwargs):
    return mapped_column(
        SAEnum(
            MaterialUnit,
            values_callable=lambda e: [m.value for m in e],
            name="material_unit",
        ),
        nullable=False,
        **kwargs,
    )


class SiteInventory(Base):
    """One item's running stock at one site."""

    __tablename__ = "site_inventory"
    __table_args__ = (
        # The upsert key. Two "Cement" rows at one site would each hold half
        # the stock and neither would answer "how many bags are left".
        UniqueConstraint("site_id", "item_name", name="uq_site_inventory_site_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Numeric(12, 3): materials arrive as 2.5 tons and 0.75 brass, not in
    # whole units.
    quantity_in_stock: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), default=Decimal("0"), server_default="0", nullable=False
    )

    # Fixed per item per site, and the reason MaterialUnit is an enum: bags
    # and tons of the same cement cannot be added together, so the unit is
    # settled once when the item is first received rather than per delivery.
    unit: Mapped[MaterialUnit] = _unit_column()

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="inventory")

    def __repr__(self) -> str:
        return (
            f"<SiteInventory id={self.id} item_name={self.item_name!r} "
            f"quantity_in_stock={self.quantity_in_stock} unit={self.unit}>"
        )


class MaterialReceipt(Base):
    """One delivery accepted at a site — the Goods Receipt Note."""

    __tablename__ = "material_receipts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Copied off the inventory row at write time rather than only referenced,
    # so a receipt still says what turned up even if the item is later renamed
    # or the inventory row is cleared out at the end of a job.
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit: Mapped[MaterialUnit] = _unit_column()

    # The inventory row this delivery topped up. SET NULL: clearing the stock
    # list must not delete the record of what was delivered.
    inventory_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("site_inventory.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Storage key for the photographed delivery challan, e.g.
    # "photos/7/2026/09/1f3c….jpg". Nullable, because a delivery arriving in
    # the rain with no paper should still be recordable — but the app pushes
    # hard for the photo, since a GRN nobody photographed is just a claim.
    challan_image_path: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    supplier: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    received_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="material_receipts")
    inventory: Mapped[Optional["SiteInventory"]] = relationship()
    receiver: Mapped[Optional["User"]] = relationship(foreign_keys=[received_by])

    def __repr__(self) -> str:
        return (
            f"<MaterialReceipt id={self.id} site_id={self.site_id} "
            f"item_name={self.item_name!r} quantity_received={self.quantity_received}>"
        )
