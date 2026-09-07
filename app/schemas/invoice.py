"""What the contractor bills the client, and the figures it is built from.

TWO SHAPES, ONE PURPOSE
-----------------------
`InvoicePreview` is arithmetic: what a site cost over a window, broken into
lines the admin can read down. `InvoiceCreate` is a decision: the number the
client is actually asked for.

They are deliberately not the same number, and the create route does NOT
recompute the preview. An invoice carries markup, retention, a rounding, a
discount agreed on the phone — none of which live in this database. Forcing
the bill to equal the sum of the logs would either be wrong or would need
every commercial term modelled first. So the app seeds the field from the
preview and the admin types over it.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import InvoiceStatus


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


ClientName = Annotated[
    str, Field(min_length=1, max_length=160), AfterValidator(_strip)
]
#: Numeric(12, 2) in the model — a bill runs larger than a wage.
Total = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class InvoiceLine(BaseModel):
    """One row of the preview: a thing that was done or bought, and its cost."""

    #: "piece_rate", "materials" or "machinery" — what the app groups by.
    kind: str
    label: str
    #: Null on a money-only line, e.g. a category total with no natural unit.
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    rate: Optional[Decimal] = None
    amount: Decimal


class InvoicePreview(BaseModel):
    """What one site cost over a window, ready to be billed.

    THE THREE SUBTOTALS CAN OVERLAP
    -------------------------------
    `machinery_total` is the fuel logged against plant; `materials_total` is
    what the cash box paid out at this site, which may already include that
    same diesel if it was also filed as an expense. They are reported apart
    rather than added blindly so the admin can see the overlap and decide,
    instead of being handed one total that quietly counts a tanker twice.

    `suggested_total` is all three summed — a starting figure for the field,
    not an authority. See the module docstring.
    """

    site_id: int
    site_name: str
    start_date: Optional[date_type] = None
    end_date: date_type

    piece_rate_total: Decimal
    materials_total: Decimal
    machinery_total: Decimal
    suggested_total: Decimal

    lines: list[InvoiceLine] = []

    #: Deliveries accepted in the window. Quantities only — a receipt records
    #: what arrived, never what it cost — so they inform the bill rather than
    #: appearing in it.
    material_receipt_count: int = 0


class InvoiceCreate(BaseModel):
    site_id: int = Field(gt=0)
    client_name: ClientName
    total_amount: Total
    status: InvoiceStatus = InvoiceStatus.DRAFT
    note: Optional[str] = Field(default=None, max_length=255)


class InvoiceUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    client_name: Optional[ClientName] = None
    total_amount: Optional[Total] = None
    status: Optional[InvoiceStatus] = None
    note: Optional[str] = Field(default=None, max_length=255)


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    #: Flattened so a list row can name the job without a lookup per invoice.
    site_name: Optional[str] = None
    client_name: str
    total_amount: Decimal
    status: InvoiceStatus
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    #: Derived from the stored key at read time, never persisted. Null until a
    #: PDF has been built — a draft is a figure agreed on a phone call.
    pdf_url: Optional[str] = None
