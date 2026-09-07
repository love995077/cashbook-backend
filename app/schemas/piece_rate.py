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


WorkDescription = Annotated[
    str, Field(min_length=1, max_length=255), AfterValidator(_strip)
]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
Rate = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]


class PieceRateLogCreate(BaseModel):
    worker_id: int = Field(gt=0)
    site_id: Optional[int] = Field(default=None, gt=0)
    work_description: WorkDescription
    quantity: Quantity
    rate: Rate
    date: Optional[date_type] = None
    note: Optional[str] = Field(default=None, max_length=255)

    # `total_amount` is deliberately absent. The server multiplies, because a
    # client-supplied total is a number nobody can check against the two that
    # are meant to produce it.


class PieceRateLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_id: int
    worker_name: Optional[str] = None
    site_id: Optional[int] = None
    work_description: str
    quantity: Decimal
    rate: Decimal
    total_amount: Decimal
    date: date_type
    #: The WAGE row this log wrote into the worker's ledger.
    transaction_id: Optional[int] = None
    created_at: datetime
