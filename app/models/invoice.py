"""What the contractor bills the client for a site.

The other direction from everything else in this schema. `cash_entries` record
money the site owner has ALREADY paid in; an invoice is money that has been
asked for and has not arrived yet. Keeping them apart is what makes
"outstanding" a question with an answer.

TOTALS ARE STORED, NOT RECOMPUTED
---------------------------------
`total_amount` is what the client was actually billed, frozen at the moment
the invoice was raised. Re-deriving it later from piece-rate logs and material
receipts would quietly restate an invoice that has already been sent — and a
bill that changes after it is issued is not a bill.
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import InvoiceStatus

if TYPE_CHECKING:
    from app.models.site import Site


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Copied rather than referenced: the client is whoever was billed, and a
    # site changing hands must not rewrite last quarter's invoices.
    client_name: Mapped[str] = mapped_column(String(160), nullable=False)

    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        SAEnum(
            InvoiceStatus,
            values_callable=lambda e: [m.value for m in e],
            name="invoice_status",
        ),
        nullable=False,
        default=InvoiceStatus.DRAFT,
        server_default=InvoiceStatus.DRAFT.value,
        index=True,
    )

    # Storage key for the generated PDF, never a URL. Null until one is built —
    # a draft is a figure agreed on a phone call before any paper exists.
    pdf_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="invoices")

    def __repr__(self) -> str:
        return (
            f"<Invoice id={self.id} client_name={self.client_name!r} "
            f"total_amount={self.total_amount} status={self.status}>"
        )
