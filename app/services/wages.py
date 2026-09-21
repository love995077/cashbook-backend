"""What a day of attendance earns a worker on a day rate.

------------------------------------------------------------------------------
THE BUG THIS EXISTS TO FIX
------------------------------------------------------------------------------
Money in this app lives in `transactions`. Every figure a contractor reads -
`outstanding_payable` on Home, `net_payable` on the settlement screen, the
worker's own balance - is built by summing WAGE rows and subtracting advances
and payments.

A piece-rate worker earned theirs: `build_piece_rate_log` writes a WAGE
alongside the log, in the same DB transaction, so measured work lands in the
same balance as everything else.

A worker on a DAY RATE earned nothing. Marking them present wrote an
`attendance` row and stopped there, so a crew could work a full week and the
contractor would be told they were owed zero. Attendance and wages were two
systems that never met - `PayrollSummary` even reports `days_worked` and
`total_wages` side by side, one from each, with nothing tying them together.

This module is that tie. Marking a day present earns the day's wage, exactly
the way piece work does.

------------------------------------------------------------------------------
ONE FUNCTION, CALLED EVERYWHERE A DAY CHANGES
------------------------------------------------------------------------------
`sync_attendance_wage` is idempotent and total: given an attendance row, it
makes the linked WAGE match what that row now says, whatever it said before.
Create, correct, re-correct, unmark - every path calls the same function, so
there is no sequence of edits that can leave a wage the day does not justify.

That matters more than it looks. The alternative - "add a wage here, remove it
there" spread across the bulk route, the single route, the scan route, the
edit route and the delete route - is five chances to forget one, and the
symptom of forgetting is money.

------------------------------------------------------------------------------
WHAT DOES NOT EARN
------------------------------------------------------------------------------
  * `absent` and `rain_out`. Nothing was worked. (Rain-out is the contractor's
    to settle as goodwill if they choose - as an explicit wage entry, not as a
    silent side effect of the weather.)
  * A PIECE_RATE worker. Their log already pays them; earning a day rate too
    would pay twice for one day.
  * A worker with no `daily_wage`, or one set to zero. There is no figure to
    earn, and inventing one is worse than recording nothing. This is the
    ordinary state of a worker booked with only a name.

A half day earns half, quantised to paise the same way every other money path
is - and the same proportion `days_worked` and the deduction installments
already use, so the three cannot drift apart.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import AttendanceStatus, TransactionType, WageType
from app.models.transaction import Transaction
from app.models.user import User

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
HALF = Decimal("0.5")

#: The statuses that earn a day rate. `rain_out` is deliberately absent - see
#: the module docstring. Kept as a set rather than inlined so a new status
#: cannot quietly default to earning.
EARNING_STATUSES = frozenset({AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY})

#: Marks the wage as created by attendance rather than typed by the
#: contractor. Cosmetic - the link in `attendance.transaction_id` is what the
#: code trusts - but it is what the contractor sees in the ledger, and "Day
#: wage" beside a row they did not type is the difference between a figure
#: that explains itself and one that looks like a bug.
WAGE_NOTE = "Day wage"
HALF_DAY_NOTE = "Half day wage"


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def wage_for(worker: User, status: AttendanceStatus) -> Optional[Decimal]:
    """What one day at `status` earns `worker`, or None if it earns nothing.

    None and zero are the same answer here and both mean "write no row": a
    zero-amount WAGE would sit in every ledger and statement saying nothing,
    and `TransactionCreate` refuses non-positive amounts anyway.
    """
    if status not in EARNING_STATUSES:
        return None
    # Paid by the piece: `build_piece_rate_log` writes their wage instead.
    if worker.wage_type is WageType.PIECE_RATE:
        return None
    if worker.daily_wage is None:
        return None

    rate = Decimal(worker.daily_wage)
    if status is AttendanceStatus.HALF_DAY:
        rate = rate * HALF

    earned = _money(rate)
    return earned if earned > ZERO else None


def sync_attendance_wage(
    db: Session, attendance: Attendance, worker: User
) -> Optional[Transaction]:
    """Make the WAGE linked to `attendance` match what the row now says.

    The single entry point for the attendance-to-wage relationship. Safe to
    call on an unchanged row, on a row whose status or date just moved, and on
    a row about to be deleted (call it with the row still present, then
    delete).

    Returns the live wage, or None when the day earns nothing.

    Does NOT commit. The caller owns the transaction boundary, so the wage and
    the day it came from land together or not at all - the same guarantee
    piece work already has.
    """
    earned = wage_for(worker, attendance.status)
    existing = (
        db.get(Transaction, attendance.transaction_id)
        if attendance.transaction_id is not None
        else None
    )

    if earned is None:
        # The day stopped earning - corrected to absent, or the worker moved
        # onto piece rate. Drop the wage rather than leaving a stale one.
        if existing is not None:
            attendance.transaction_id = None
            db.delete(existing)
            # So a caller that reads the balance before committing does not
            # still see the row being removed.
            db.flush()
        return None

    note = HALF_DAY_NOTE if attendance.status is AttendanceStatus.HALF_DAY else WAGE_NOTE

    if existing is None:
        wage = Transaction(
            worker_id=worker.id,
            type=TransactionType.WAGE,
            amount=earned,
            date=attendance.date,
            # A wage is earned, not handed over: nothing changed hands, so
            # there is no payment_mode. See `TransactionBase`.
            note=note,
        )
        db.add(wage)
        # The id is needed to link the two, and the link is the only thing
        # that makes this row reversible later.
        db.flush()
        attendance.transaction_id = wage.id
        return wage

    # Already linked: move it to match. Assigning unconditionally would dirty
    # the row on every bulk resubmission, so each field is checked first.
    if existing.amount != earned:
        existing.amount = earned
    if existing.date != attendance.date:
        existing.date = attendance.date
    if existing.note != note:
        existing.note = note
    return existing


def clear_attendance_wage(db: Session, attendance: Attendance) -> None:
    """Drop the wage a day earned, because the day itself is going away.

    Separate from `sync_attendance_wage` because unmarking is not a status
    change: there will be no row left to read a status from, and the wage must
    go whatever that status was.
    """
    if attendance.transaction_id is None:
        return
    wage = db.get(Transaction, attendance.transaction_id)
    attendance.transaction_id = None
    if wage is not None:
        db.delete(wage)
    db.flush()
