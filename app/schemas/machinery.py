"""What a machine did on a site for a day, and what it cost to feed it.

`fuel_cost` is sent rather than derived. See the note in
`app/models/machinery.py`: the rate changes between deliveries and between
pumps, and what the site actually holds is a receipt with a number on it.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


MachineName = Annotated[
    str, Field(min_length=1, max_length=120), AfterValidator(_strip)
]
#: A machine runs 7.5 hours, not 7. Capped at 24 — a log is one machine, one
#: day, so anything larger is a typed decimal point rather than a long shift.
Hours = Annotated[Decimal, Field(ge=0, le=24, max_digits=6, decimal_places=2)]
Litres = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


class MachineryLogCreate(BaseModel):
    machine_name: MachineName
    hours_operated: Hours = Decimal("0")
    diesel_litres: Litres = Decimal("0")
    fuel_cost: Money = Decimal("0")
    date: Optional[date_type] = None
    note: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _something_happened(self) -> "MachineryLogCreate":
        """Refuse a row that records nothing.

        All three default to zero so a site can log fuel without hours or
        hours without fuel, but a log where every number is zero is a machine
        that did not run — which is not a fact worth a row, and is almost
        always a form submitted before it was filled in.
        """
        if not (self.hours_operated or self.diesel_litres or self.fuel_cost):
            raise ValueError(
                "Record at least one of hours run, diesel taken or fuel cost"
            )
        return self


class MachineryLogUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    machine_name: Optional[MachineName] = None
    hours_operated: Optional[Hours] = None
    diesel_litres: Optional[Litres] = None
    fuel_cost: Optional[Money] = None
    date: Optional[date_type] = None
    note: Optional[str] = Field(default=None, max_length=255)


class MachineryLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    machine_name: str
    hours_operated: Decimal
    diesel_litres: Decimal
    fuel_cost: Decimal
    date: date_type
    note: Optional[str] = None
    logged_by: Optional[int] = None
    logged_by_name: Optional[str] = None
    created_at: datetime

    #: Derived from the stored key at read time, never persisted — the same
    #: rule as every other image in the schema.
    receipt_image_url: Optional[str] = None


class MachineUsage(BaseModel):
    """One machine's share of the period.

    Litres are carried next to the cost because a rising bill is either the
    machine or the market, and only the litres tell you which.
    """

    machine_name: str
    hours_operated: Decimal
    diesel_litres: Decimal
    fuel_cost: Decimal
    log_count: int


class MachineryTotals(BaseModel):
    site_id: int
    start_date: Optional[date_type] = None
    end_date: date_type
    hours_operated: Decimal
    diesel_litres: Decimal
    fuel_cost: Decimal
    log_count: int
    by_machine: list[MachineUsage] = []
