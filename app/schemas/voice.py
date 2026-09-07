from datetime import date as date_type
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.enums import TransactionType
from app.schemas.transaction import TransactionRead


class ParsedEntry(BaseModel):
    """One transaction the language model believes it heard.

    A single utterance often carries more than one:
    "Aaj 600 rupaye wage aur 200 rupaye advance diya" is a wage AND an advance.
    """

    worker_id: Optional[int] = Field(
        default=None,
        gt=0,
        description="Resolved worker. None when the speaker never named one and no default was supplied.",
    )
    type: TransactionType
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    date: date_type
    note: Optional[str] = Field(default=None, max_length=255)

    # How sure the model is, 0-1. Low confidence routes to confirmation
    # instead of being written straight to the ledger.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ParsedIntent(BaseModel):
    """Everything the LLM extracted from one transcript."""

    entries: list[ParsedEntry] = Field(default_factory=list)
    # Anything the model could not resolve: an unknown name, a missing amount,
    # two workers with the same first name.
    problems: list[str] = Field(default_factory=list)


class VoiceEntryResponse(BaseModel):
    """What the app gets back after speaking.

    `status` drives the UI:
      recorded            - written to the ledger, show a confirmation
      needs_confirmation  - understood, but something is ambiguous; show the
                            parsed entries and let the contractor confirm or fix
      not_understood      - nothing usable came out of the transcript
    """

    status: Literal["recorded", "needs_confirmation", "not_understood"]
    transcript: str
    message: str
    created: list[TransactionRead] = Field(default_factory=list)
    # Populated for needs_confirmation so the app can prefill the entry form
    # rather than making the contractor start over.
    pending: list[ParsedEntry] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
