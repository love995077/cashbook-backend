"""The wage audit and reconcile, as the API returns them.

See app/services/integrity.py for what each field means. Money is Decimal, so
it crosses the wire as a string like every other amount.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class WageIssueRead(BaseModel):
    kind: str
    attendance_id: int
    worker_id: int
    worker_name: str
    date: date_type
    status: str
    wage_rate: Optional[Decimal] = None
    expected: Optional[Decimal] = None
    actual: Optional[Decimal] = None
    transaction_id: Optional[int] = None


class WageAuditRead(BaseModel):
    """Does the ledger match the register?

    `consistent` is the one-word answer: true when no day's wage disagrees
    with the day. Unpriced days - earning statuses for workers with no rate -
    are listed but do not make it false; they are waiting for a rate, not
    wrong.
    """

    consistent: bool
    counts: dict[str, int]
    issues: list[WageIssueRead]


class WorkerReconcileRead(BaseModel):
    worker_id: int
    worker_name: str
    days_priced: int
    wages_created: int
    wages_adopted: int
    wages_corrected: int
    wages_removed: int
    still_unpriced: int
    net_change: Decimal


class ReconcileRead(BaseModel):
    """What a reconcile did, or - when `applied` is false - would do."""

    applied: bool
    days_priced: int
    wages_created: int
    wages_adopted: int
    wages_corrected: int
    wages_removed: int
    still_unpriced: int
    net_change: Decimal
    by_worker: list[WorkerReconcileRead]
    remaining_issues: list[WageIssueRead]
