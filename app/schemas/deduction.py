from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.petty_cash import Money


class AutoDeductionCreate(BaseModel):
    """Open an installment plan against a worker.

    Usually created as a side effect of giving an advance (see
    `TransactionCreate.daily_deduction`); this stands alone for a debt that
    predates the app or was agreed away from it.
    """

    worker_id: int = Field(gt=0)
    principal_amount: Money
    daily_deduction: Money
    note: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _installment_must_fit(self) -> "AutoDeductionCreate":
        if self.daily_deduction > self.principal_amount:
            # Otherwise the first day clears the whole plan, which is not an
            # installment arrangement — it is a lump sum with extra steps.
            raise ValueError(
                "The daily deduction cannot be larger than the amount being recovered"
            )
        return self


class AutoDeductionUpdate(BaseModel):
    """Change the rate, or stop collecting.

    `principal_amount` is absent on purpose: it is what was actually handed
    over, and editing it after installments have been taken would leave
    `balance_remaining` describing a debt that never existed.
    """

    daily_deduction: Optional[Money] = None
    is_active: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=255)


class AutoDeductionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_id: int
    worker_name: Optional[str] = None
    principal_amount: Decimal
    daily_deduction: Decimal
    balance_remaining: Decimal
    #: principal - remaining, so the app does not repeat the subtraction.
    recovered_amount: Decimal
    is_active: bool
    transaction_id: Optional[int] = None
    note: Optional[str] = None
    created_at: datetime


class DeductionInstallmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    deduction_id: int
    worker_id: int
    amount: Decimal
    date: date_type
