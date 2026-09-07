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
from app.models.enums import MaterialLogType, MaterialUnit

if TYPE_CHECKING:
    from app.models.site import Site


class SiteMaterial(Base):
    """A material type tracked at one site — "Cement", counted in bags.

    Scoped per site rather than per contractor: the same contractor may buy
    cement in bags at one job and tons at another, and stock is only meaningful
    against a place.
    """

    __tablename__ = "site_materials"
    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_site_materials_site_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[MaterialUnit] = mapped_column(
        SAEnum(
            MaterialUnit,
            values_callable=lambda e: [m.value for m in e],
            name="material_unit",
        ),
        nullable=False,
    )
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="materials")
    logs: Mapped[list["MaterialLog"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<SiteMaterial id={self.id} name={self.name!r} unit={self.unit}>"


class MaterialLog(Base):
    """One movement of a material at a site.

    `quantity` is always POSITIVE; `type` says which way it went. Storing the
    sign in a separate column keeps sums unambiguous and makes stock on hand a
    single grouped query (deliveries minus consumption) rather than something
    that depends on every writer remembering to negate.

    Numeric(12, 3) because materials are not counted in whole units — 2.5 tons
    of sand, 0.75 brass of aggregate.
    """

    __tablename__ = "material_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("site_materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[MaterialLogType] = mapped_column(
        SAEnum(
            MaterialLogType,
            values_callable=lambda e: [m.value for m in e],
            name="material_log_type",
        ),
        nullable=False,
        default=MaterialLogType.CONSUMPTION,
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="material_logs")
    material: Mapped["SiteMaterial"] = relationship(back_populates="logs")

    def __repr__(self) -> str:
        return (
            f"<MaterialLog id={self.id} material_id={self.material_id} "
            f"type={self.type} quantity={self.quantity}>"
        )
