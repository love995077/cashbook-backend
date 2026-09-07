"""Working out what a worker is owed for a period, and clearing it.

The arithmetic is the same `calculate_balance` the ledger already uses — this
module does not invent a second definition of what a worker is owed. What it
adds is the window handling, the attendance counts that make the figure
explicable on screen, and the one write that zeroes the result.

WHY A CLEARING TRANSACTION RATHER THAN A FLAG
---------------------------------------------
Settling could have been a `settled_at` stamp on the rows it covers. It is a
PAYMENT row instead, because that is what actually happened: the contractor
handed over money. A flag would leave the ledger claiming a balance nobody
owes, and the worker's own screen — which reads the same balance — would
disagree with the payslip. One row, one arithmetic, both ends agree.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import AttendanceStatus, PaymentMode, TransactionType
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.payroll import PayrollSummary
from app.services.deductions import payroll_adjustment
from app.services.ledger import ZERO, calculate_balance

HALF = Decimal("0.5")


def _attendance_counts(
    db: Session, worker_id: int, start: Optional[date_type], end: date_type
) -> dict[AttendanceStatus, int]:
    stmt = select(Attendance.status, func.count()).where(Attendance.worker_id == worker_id)
    if start is not None:
        stmt = stmt.where(Attendance.date >= start)
    stmt = stmt.where(Attendance.date <= end)

    counts = {status: 0 for status in AttendanceStatus}
    for status, total in db.execute(stmt.group_by(Attendance.status)).all():
        counts[status] = int(total)
    return counts


def build_summary(
    db: Session,
    worker: User,
    start_date: Optional[date_type] = None,
    end_date: Optional[date_type] = None,
) -> PayrollSummary:
    """What `worker` is owed over the window, and the days behind it.

    `end_date` defaults to today. `start_date` left out means the whole of
    the worker's history, which is the usual request.
    """
    end = end_date or date_type.today()

    window = calculate_balance(db, worker.id, start_date, end)
    lifetime = calculate_balance(db, worker.id).balance
    counts = _attendance_counts(db, worker.id, start_date, end)

    # An advance being recovered day by day is not due in full this period.
    # The ledger has already subtracted it, so put it back and subtract the
    # installments actually taken instead. See app/services/deductions.py.
    emi_advances, installments, emi_outstanding = payroll_adjustment(
        db, worker.id, start_date, end
    )
    adjusted_window = window.balance + emi_advances - installments
    # The lifetime cap gets the same treatment, or it would immediately clamp
    # the adjustment straight back off again. Across all time the plan-backed
    # advances minus everything recovered from them IS the outstanding
    # balance, so that one figure is the whole lifetime adjustment.
    adjusted_lifetime = lifetime + emi_outstanding

    present = counts[AttendanceStatus.PRESENT]
    half = counts[AttendanceStatus.HALF_DAY]

    # The window on its own is not safe to pay out. A settlement dated at the
    # end of a WIDER period sits outside a NARROWER one, so re-running the
    # narrow window would see the wages, miss the payment that already cleared
    # them, and offer to pay the same money twice.
    #
    # Capping at the lifetime balance closes that: a contractor can never hand
    # over more than the worker is owed across their whole history, whatever
    # dates get typed. When the two agree - the ordinary case - the cap does
    # nothing.
    payable = min(adjusted_window, adjusted_lifetime)

    return PayrollSummary(
        worker_id=worker.id,
        worker_name=worker.name,
        start_date=start_date,
        end_date=end,
        total_wages=window.total_wages,
        total_advances=window.total_advances,
        total_payments=window.total_payments,
        window_balance=window.balance,
        net_payable=payable,
        emi_advances=emi_advances,
        installments_recovered=installments,
        emi_outstanding=emi_outstanding,
        days_present=present,
        days_half=half,
        days_absent=counts[AttendanceStatus.ABSENT],
        days_rain_out=counts[AttendanceStatus.RAIN_OUT],
        days_worked=Decimal(present) + (Decimal(half) * HALF),
        # Unbounded, so this is the whole history however narrow the window is.
        lifetime_balance=lifetime,
        can_settle=payable > ZERO,
    )


def build_clearing_transaction(
    summary: PayrollSummary,
    payment_mode: PaymentMode = PaymentMode.CASH,
    note: Optional[str] = None,
) -> Transaction:
    """The PAYMENT row that takes the worker's balance to zero.

    Dated `end_date` rather than today, so the row falls INSIDE the period it
    settles: re-previewing that same period then nets to nothing. It is only
    half the protection - a narrower window would still exclude this row - and
    the cap in `build_summary` is the other half.
    """
    return Transaction(
        worker_id=summary.worker_id,
        type=TransactionType.PAYMENT,
        amount=summary.net_payable,
        date=summary.end_date,
        payment_mode=payment_mode,
        note=note or _default_note(summary),
    )


def _default_note(summary: PayrollSummary) -> str:
    """A note that still explains the row once the request is long forgotten."""
    period = (
        f"{summary.start_date:%d %b %Y} to {summary.end_date:%d %b %Y}"
        if summary.start_date
        else f"up to {summary.end_date:%d %b %Y}"
    )
    return f"Payroll settlement, {period}"[:255]
