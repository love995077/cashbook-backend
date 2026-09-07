from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import AdvanceRequestStatus, PaymentMode


def _strip_reason(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v or None


Reason = Annotated[
    Optional[str], Field(default=None, max_length=255), AfterValidator(_strip_reason)
]


class AdvanceRequestCreate(BaseModel):
    """What a worker sends from the "Request Advance" modal.

    No worker_id: the row is always attached to the caller, so a worker cannot
    raise a request in someone else's name.
    """

    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: Reason = None
    # From POST /media/audio-notes. Checked against the caller before storing.
    audio_note_path: Optional[str] = Field(default=None, max_length=255)


class AdvanceRequestDecision(BaseModel):
    """The contractor's answer.

    Only the two terminal states are accepted - `pending` is where a request
    starts, not somewhere it can be put back.
    """

    status: AdvanceRequestStatus = Field(
        description="approved or rejected; pending is rejected as a no-op"
    )
    # Lets the contractor approve a different figure to the one asked for
    # ("I can do 2000, not 5000") without a second round trip. Omitted means
    # the requested amount stands.
    approved_amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=10, decimal_places=2
    )
    # How the approved advance was handed over. Ignored on a rejection, since
    # no money moves.
    payment_mode: Optional[PaymentMode] = None
    note: Optional[str] = Field(default=None, max_length=255)


class AdvanceRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_id: int
    amount: Decimal
    reason: Optional[str] = None
    status: AdvanceRequestStatus
    audio_note_url: Optional[str] = None
    transaction_id: Optional[int] = None
    decided_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # Denormalised onto the row so the contractor's approvals queue can render
    # "Ramesh - Rs. 2,000" without a second request per card.
    worker_name: Optional[str] = None
