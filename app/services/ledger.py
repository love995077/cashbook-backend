"""Balance arithmetic and ledger assembly for a worker."""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import TransactionType
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.ledger import BalanceSummary, WorkerLedger

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def _money(value: Optional[Decimal]) -> Decimal:
    return (Decimal(value) if value is not None else ZERO).quantize(CENTS)


def calculate_balance(
    db: Session,
    worker_id: int,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
) -> BalanceSummary:
    """Money position for a worker.

    balance = total wages earned - advances taken - payments settled.
    A positive balance is what the contractor still owes the worker; a negative
    one means the worker has been paid ahead of what they have earned.

    With no bounds this is the worker's whole history — the running balance the
    ledger endpoints return. The bounds exist for PDF statements, which need
    three different windows: everything before the period (opening balance),
    the period itself (period totals), and everything through the period end
    (closing balance).
    """
    stmt = select(Transaction.type, func.sum(Transaction.amount)).where(
        Transaction.worker_id == worker_id
    )
    if start is not None:
        stmt = stmt.where(Transaction.date >= start)
    if end is not None:
        stmt = stmt.where(Transaction.date <= end)

    rows = db.execute(stmt.group_by(Transaction.type)).all()

    totals = {tx_type: ZERO for tx_type in TransactionType}
    for tx_type, total in rows:
        totals[tx_type] = _money(total)

    wages = totals[TransactionType.WAGE]
    advances = totals[TransactionType.ADVANCE]
    payments = totals[TransactionType.PAYMENT]

    return BalanceSummary(
        total_wages=wages,
        total_advances=advances,
        total_payments=payments,
        balance=wages - advances - payments,
    )


def build_worker_ledger(
    db: Session,
    worker: User,
    start_date: Optional[date_type] = None,
    end_date: Optional[date_type] = None,
) -> WorkerLedger:
    """Balance plus the transaction and attendance rows for a worker.

    The optional dates filter the listed rows only — the balance always covers
    the full history.
    """
    tx_stmt = select(Transaction).where(Transaction.worker_id == worker.id)
    att_stmt = select(Attendance).where(Attendance.worker_id == worker.id)

    if start_date is not None:
        tx_stmt = tx_stmt.where(Transaction.date >= start_date)
        att_stmt = att_stmt.where(Attendance.date >= start_date)
    if end_date is not None:
        tx_stmt = tx_stmt.where(Transaction.date <= end_date)
        att_stmt = att_stmt.where(Attendance.date <= end_date)

    # Newest first, so the app can render the ledger without re-sorting.
    transactions = db.scalars(
        tx_stmt.order_by(Transaction.date.desc(), Transaction.id.desc())
    ).all()
    attendance = db.scalars(
        att_stmt.order_by(Attendance.date.desc(), Attendance.id.desc())
    ).all()

    return WorkerLedger(
        worker=worker,
        balance=calculate_balance(db, worker.id),
        transactions=list(transactions),
        attendance=list(attendance),
    )
