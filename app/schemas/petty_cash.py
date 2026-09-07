from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


Money = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
# Required, unlike most notes in this API. An unlabelled ₹200 is the entry
# nobody can account for a month later, and petty cash is made of those.
Description = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_strip)]


class PettyCashCreate(BaseModel):
    amount: Money
    description: Description
    # Defaults to today in the router rather than here, so "not sent" and
    # "sent as today" stay distinguishable for as long as possible.
    date: Optional[date_type] = None


class PettyCashRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    amount: Decimal
    description: str
    date: date_type
    recorded_by: Optional[int] = None
    recorded_by_name: Optional[str] = None
    created_at: datetime


class PettyCashTotals(BaseModel):
    """The petty cash tab's header: what this site has leaked, and over what."""

    site_id: int
    #: Every petty cash row for the site, all time.
    total: Decimal
    #: Narrowed to the requested window, which is the month by default.
    period_total: Decimal
    start_date: Optional[date_type] = None
    end_date: date_type
    entry_count: int
