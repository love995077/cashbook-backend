from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import PaymentMode, TransactionType


class TransactionBase(BaseModel):
    worker_id: int = Field(gt=0)
    type: TransactionType
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    date: date_type
    # How the money changed hands. Optional rather than required: a WAGE has
    # no mode (nothing was handed over), and the mobile app's offline queue
    # can still be holding entries written before this field existed.
    payment_mode: Optional[PaymentMode] = None
    note: Optional[str] = Field(default=None, max_length=255)


class TransactionCreate(TransactionBase):
    # A storage path from POST /media/audio-notes, not a URL the caller made
    # up: the endpoint checks it was uploaded by this account before storing.
    audio_note_path: Optional[str] = Field(default=None, max_length=255)

    # "Recover via daily installments", from the Add Advance modal. Sending a
    # daily_deduction opens an AutoDeduction plan against this advance in the
    # same database transaction, so a plan can never point at a row that was
    # never written.
    #
    # Only meaningful on an ADVANCE. Recovering a wage from itself is not a
    # thing, and recovering a settlement would undo the settlement.
    daily_deduction: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=10, decimal_places=2
    )

    # The app's idempotency key. A repeat of a key already stored returns the
    # stored row instead of writing a second one - see
    # `Transaction.client_ref` and mobile/src/offline/queue.ts.
    client_ref: Optional[str] = Field(default=None, min_length=1, max_length=64)

    # The site this money belongs to. Left out, it is the site the worker is
    # posted to right now - see `Transaction.site_id`.
    site_id: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_installments(self) -> "TransactionCreate":
        if self.daily_deduction is None:
            return self
        if self.type is not TransactionType.ADVANCE:
            raise ValueError(
                "Daily installments only apply to an advance"
            )
        if self.daily_deduction > self.amount:
            # The first day would clear the whole plan, which is a lump sum
            # with extra steps rather than an installment arrangement.
            raise ValueError(
                "The daily deduction cannot be larger than the advance itself"
            )
        return self


class TransactionUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    type: Optional[TransactionType] = None
    amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=10, decimal_places=2
    )
    date: Optional[date_type] = None
    payment_mode: Optional[PaymentMode] = None
    note: Optional[str] = Field(default=None, max_length=255)
    audio_note_path: Optional[str] = Field(default=None, max_length=255)


class TransactionRead(TransactionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    #: The site this money is attributed to; null when it belongs to none.
    site_id: Optional[int] = None
    #: The installment plan opened against this advance, when there is one.
    deduction_id: Optional[int] = None
    # Derived from audio_note_path at read time; see app/services/storage.py.
    audio_note_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime
