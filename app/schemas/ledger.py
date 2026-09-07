from decimal import Decimal

from pydantic import BaseModel

from app.schemas.attendance import AttendanceRead
from app.schemas.transaction import TransactionRead
from app.schemas.user import UserRead


class BalanceSummary(BaseModel):
    """Lifetime money position for one worker."""

    total_wages: Decimal
    total_advances: Decimal
    total_payments: Decimal
    # total_wages - total_advances - total_payments; positive means the
    # contractor still owes the worker.
    balance: Decimal


class WorkerLedger(BaseModel):
    worker: UserRead
    balance: BalanceSummary
    transactions: list[TransactionRead]
    attendance: list[AttendanceRead]
