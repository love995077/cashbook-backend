from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.attendance import Attendance
    from app.models.inventory import MaterialReceipt, SiteInventory
    from app.models.invoice import Invoice
    from app.models.machinery import MachineryLog
    from app.models.material import MaterialLog, SiteMaterial
    from app.models.petty_cash import PettyCash
    from app.models.piece_rate import PieceRateLog
    from app.models.site_photo import SitePhoto
    from app.models.tool import Tool
    from app.models.user import User


class Site(Base):
    """A place work happens — "Udaipur Villa", "City Mall".

    Owned by one contractor. Workers, attendance and materials all hang off a
    site so a contractor running several jobs can keep them apart.
    """

    __tablename__ = "sites"
    __table_args__ = (
        # Two sites with the same name under one contractor would be
        # indistinguishable in a dropdown.
        UniqueConstraint("contractor_id", "name", name="uq_sites_contractor_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contractor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The supervisor running this site day to day. SET NULL rather than
    # CASCADE: removing the person must not remove the job they were running.
    #
    # Nullable and unenforced at the database level - the check that this id
    # belongs to a SUPERVISOR under this contractor lives in the endpoint,
    # because a FK cannot express "and their role is supervisor".
    supervisor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Closed sites stay in the database so their history survives; they are
    # filtered out of pickers rather than deleted.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contractor: Mapped["User"] = relationship(
        back_populates="sites", foreign_keys=[contractor_id]
    )
    supervisor: Mapped[Optional["User"]] = relationship(foreign_keys=[supervisor_id])
    workers: Mapped[list["User"]] = relationship(
        back_populates="site", foreign_keys="User.site_id"
    )
    attendances: Mapped[list["Attendance"]] = relationship(back_populates="site")
    materials: Mapped[list["SiteMaterial"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    material_logs: Mapped[list["MaterialLog"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    photos: Mapped[list["SitePhoto"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    petty_cash: Mapped[list["PettyCash"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    tools: Mapped[list["Tool"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    inventory: Mapped[list["SiteInventory"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    material_receipts: Mapped[list["MaterialReceipt"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    machinery_logs: Mapped[list["MachineryLog"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    # SET NULL on the column, so these survive the site being deleted and are
    # deliberately NOT cascaded away with it - the wage they created is still
    # in the worker's ledger.
    piece_rate_logs: Mapped[list["PieceRateLog"]] = relationship(back_populates="site")

    def __repr__(self) -> str:
        return f"<Site id={self.id} name={self.name!r} contractor_id={self.contractor_id}>"
