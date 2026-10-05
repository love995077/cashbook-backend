from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import CashDirection


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


CategoryName = Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_strip)]
Money = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]

# "#RRGGBB" or "#RRGGBBAA" — the app renders this straight into a style.
HexColor = Annotated[str, Field(pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")]


class CashCategoryBase(BaseModel):
    name: CategoryName
    direction: CashDirection = CashDirection.OUT
    icon: Optional[str] = Field(default=None, max_length=40)
    color: Optional[HexColor] = None


class CashCategoryCreate(CashCategoryBase):
    pass


class CashCategoryUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed.

    `direction` is deliberately absent: flipping a category from spend to
    income would re-sign every entry already filed under it.
    """

    name: Optional[CategoryName] = None
    icon: Optional[str] = Field(default=None, max_length=40)
    color: Optional[HexColor] = None
    is_active: Optional[bool] = None


class CashCategoryRead(CashCategoryBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_default: bool
    is_active: bool


class CashEntryCreate(BaseModel):
    category_id: Optional[int] = Field(default=None, gt=0)
    site_id: Optional[int] = Field(default=None, gt=0)
    amount: Money
    date: date_type
    note: Optional[str] = Field(default=None, max_length=255)
    # Idempotency key from the offline queue. Replaying a write that already
    # landed returns the stored row rather than creating a second one.
    client_ref: Optional[str] = Field(default=None, max_length=64)
    # Falls back to the category's direction when omitted; required only for
    # an uncategorised entry.
    direction: Optional[CashDirection] = None


class CashEntryUpdate(BaseModel):
    category_id: Optional[int] = Field(default=None, gt=0)
    site_id: Optional[int] = Field(default=None, gt=0)
    amount: Optional[Money] = None
    date: Optional[date_type] = None
    note: Optional[str] = Field(default=None, max_length=255)


class CashEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: Optional[int]
    site_id: Optional[int]
    direction: CashDirection
    amount: Decimal
    date: date_type
    note: Optional[str]
    created_at: datetime
    updated_at: datetime
    # Flattened off the relationship so a list of entries renders without the
    # app holding a category lookup of its own.
    category_name: Optional[str] = None
    category_icon: Optional[str] = None
    category_color: Optional[str] = None


class CashSummary(BaseModel):
    """The three numbers on the Home screen, for one period."""

    cash_in: Decimal
    """Money received — site advances and client payments."""

    wages_paid: Decimal
    """Cash that actually reached workers: advances given + payments settled."""

    expenses_paid: Decimal
    """Everything else spent — materials, fuel, transport, rentals."""

    total_out: Decimal
    """wages_paid + expenses_paid."""

    balance: Decimal
    """cash_in - total_out. What is left in the cash box for this period."""

    wages_earned: Decimal
    """Wages accrued to workers, whether or not they have been paid yet."""

    outstanding_payable: Decimal
    """Summed over workers the contractor still owes. Not part of `balance`."""

    worker_count: int
    start_date: Optional[date_type] = None
    end_date: Optional[date_type] = None
    # The site these figures are for; null for the whole business.
    site_id: Optional[int] = None


class AnalyticsBucket(BaseModel):
    """One bar in the chart."""

    start_date: date_type
    end_date: date_type
    label: str
    wages_paid: Decimal
    expenses_paid: Decimal
    total_out: Decimal
    cash_in: Decimal


class CategoryTotal(BaseModel):
    category_id: Optional[int]
    name: str
    direction: CashDirection
    color: Optional[str] = None
    icon: Optional[str] = None
    total: Decimal


class AnalyticsSeries(BaseModel):
    period: str
    start_date: date_type
    end_date: date_type
    buckets: list[AnalyticsBucket]
    totals: CashSummary
    by_category: list["CategoryTotal"]
