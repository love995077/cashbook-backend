"""Marking a whole crew for one day in a single request.

The mobile grid used to POST one attendance row per tap. That is fine for
three workers and awful for twenty on site data: twenty round trips, twenty
chances to half-succeed, and no single moment at which "the day is marked".

This is that same screen as one request. It also carries piece-rate work,
because on a mixed crew the contractor is filling in one grid — some rows are
a P/A/H toggle, some are a quantity and a rate — and splitting it into two
endpoints would make the app responsible for keeping them in step.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.models.enums import AttendanceStatus, WorkShift
from app.schemas.attendance import AttendanceRead
from app.schemas.deduction import DeductionInstallmentRead
from app.schemas.piece_rate import PieceRateLogRead


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
Rate = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]


class BulkAttendanceMark(BaseModel):
    """One worker's row in the grid.

    A daily worker sends `status`. A piece-rate worker sends `quantity` and
    `rate` — and still sends a status, because the site needs to know who
    turned up whatever way they are paid; it defaults to present, since
    somebody who laid 120 sq ft was evidently there.
    """

    worker_id: int = Field(gt=0)
    status: Optional[AttendanceStatus] = None
    # Per row, not per request: a crew can be split across both shifts on the
    # same date, which is the whole reason the grid sends rows rather than a
    # single status.
    shift: WorkShift = WorkShift.DAY
    note: Optional[str] = Field(default=None, max_length=255)

    # Piece-rate half. Present together or not at all.
    quantity: Optional[Quantity] = None
    rate: Optional[Rate] = None
    work_description: Optional[
        Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_strip)]
    ] = None

    @model_validator(mode="after")
    def _check_shape(self) -> "BulkAttendanceMark":
        piece_fields = (self.quantity, self.rate)
        if any(f is not None for f in piece_fields) and not all(
            f is not None for f in piece_fields
        ):
            raise ValueError(
                "Piece-rate work needs both a quantity and a rate"
            )
        if self.quantity is None and self.status is None:
            raise ValueError(
                "Send a status for a daily worker, or a quantity and rate for piece work"
            )
        return self

    @property
    def is_piece_rate(self) -> bool:
        return self.quantity is not None and self.rate is not None


class BulkAttendanceRequest(BaseModel):
    marks: list[BulkAttendanceMark] = Field(min_length=1, max_length=200)
    # Defaults to today in the router.
    date: Optional[date_type] = None
    # Where the day was worked. Stamped onto every row that does not already
    # have one, so the site's own attendance view can find them later.
    site_id: Optional[int] = Field(default=None, gt=0)
    # A worker already marked for the day is left alone by default. The grid
    # locks marked rows, so a second submission is a retry rather than a
    # correction, and silently rewriting the day would be the wrong reading.
    overwrite: bool = False

    @model_validator(mode="after")
    def _no_duplicate_workers(self) -> "BulkAttendanceRequest":
        seen = {mark.worker_id for mark in self.marks}
        if len(seen) != len(self.marks):
            # Two rows for one worker would race each other through the
            # unique constraint and leave the caller unable to say which won.
            raise ValueError("Each worker may appear only once in a bulk mark")
        return self


class BulkAttendanceResult(BaseModel):
    """What the submission actually changed.

    Counted rather than just echoed, for the same reason `RainOutResult` is:
    "18 marked" hides that two of them already had a status and were skipped,
    which is exactly what the contractor needs to know before walking away.
    """

    date: date_type
    site_id: Optional[int] = None

    #: Workers who had no attendance for the day and now do.
    created: int
    #: Workers whose existing status was replaced (only with overwrite=true).
    updated: int
    #: Already marked, and left as they were.
    unchanged: int

    attendance: list[AttendanceRead] = []

    #: Piece-rate rows written, each of which also wrote a WAGE transaction.
    piece_rate_logs: list[PieceRateLogRead] = []
    piece_rate_total: Decimal = Decimal("0.00")

    #: Installments taken off active plans for the workers marked present.
    #: Empty when nobody on the crew is repaying an advance.
    installments: list[DeductionInstallmentRead] = []
    deductions_applied: Decimal = Decimal("0.00")
