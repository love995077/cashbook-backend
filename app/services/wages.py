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
contractor would be told they were owed zero.

This module is the tie between the two. Marking a day present earns the day's
wage, in the same database transaction as the mark, exactly the way piece work
does.

------------------------------------------------------------------------------
THE INVARIANT
------------------------------------------------------------------------------
For every attendance row:

    wage(row) = round_half_up(row.wage_rate x weight(row.status), 0.01)

    weight:   present 1 · half_day 0.5 · absent 0 · rain_out 0

and the row's linked WAGE transaction (`attendance.transaction_id`) exists
exactly when wage(row) > 0, on the row's date, for that amount. No other WAGE
is linked to the row. `app/services/integrity.py` checks this for a whole
database; the test suite checks it after every operation it performs.

------------------------------------------------------------------------------
THE RATE IS FROZEN ON THE DAY
------------------------------------------------------------------------------
The first version of this module read `users.daily_wage` every time it synced
a day. Since a resubmitted grid, a rain-out and a correction all re-sync, a
raise in October silently repriced September the next time any September day
was touched. Now the rate is snapshotted into `attendance.wage_rate` when the
day is first marked (`rate_for_new_day`), and syncing reads only the row.

A rate changes history only on purpose:

  * `price_unpriced_days` (app/services/integrity.py) - a day marked while
    the worker had no rate earns nothing until one is set; setting it prices
    those days. Without this the contractor who books a worker by name, marks
    a week, and fills in the rate on Saturday is told the worker is owed
    nothing for the week - the exact bug above, by another route.
  * `reprice_days` - the contractor says the rate was wrong ("agreed 600,
    typed 60") and asks for days already marked to follow it.

------------------------------------------------------------------------------
ONE FUNCTION, CALLED EVERYWHERE A DAY CHANGES
------------------------------------------------------------------------------
`sync_attendance_wage` is idempotent and total: given an attendance row, it
makes the linked WAGE match what that row now says, whatever it said before.
Every path that writes a day goes through app/services/attendance.py, which
calls it, so there is no sequence of edits that can leave a wage the day does
not justify.

------------------------------------------------------------------------------
WHAT DOES NOT EARN
------------------------------------------------------------------------------
  * `absent` and `rain_out`. Nothing was worked. (Rain-out is the contractor's
    to settle as goodwill if they choose - as an explicit wage entry, not as a
    silent side effect of the weather.)
  * A day marked while the worker was on PIECE_RATE (`wage_rate` 0). Their log
    already pays them; a day rate too would pay twice for one day.
  * A day with no rate yet (`wage_rate` NULL) - until one is set.

A half day earns half, quantised to paise the same way every other money path
is - and the same proportion `days_worked` and the deduction installments
already use, so the three cannot drift apart.
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import AttendanceStatus, TransactionType, WageType
from app.models.transaction import Transaction
from app.models.user import User

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
ONE = Decimal("1")
HALF = Decimal("0.5")

#: How much of a day's rate each status earns. Every status is listed, so a
#: new one cannot quietly default to earning - `weight` raises on a missing key
#: rather than guessing.
STATUS_WEIGHT: dict[AttendanceStatus, Decimal] = {
    AttendanceStatus.PRESENT: ONE,
    AttendanceStatus.HALF_DAY: HALF,
    AttendanceStatus.ABSENT: ZERO,
    AttendanceStatus.RAIN_OUT: ZERO,
}

#: The statuses that earn a day rate. `rain_out` is deliberately absent - see
#: the module docstring.
EARNING_STATUSES = frozenset(
    status for status, share in STATUS_WEIGHT.items() if share > ZERO
)

#: Marks the wage as created by attendance rather than typed by the
#: contractor. Cosmetic - the link in `attendance.transaction_id` is what the
#: code trusts - but it is what the contractor sees in the ledger, and "Day
#: wage" beside a row they did not type is the difference between a figure
#: that explains itself and one that looks like a bug.
WAGE_NOTE = "Day wage"
HALF_DAY_NOTE = "Half day wage"


def money(value: Decimal) -> Decimal:
    """Quantise to paise, rounding half up - the one rounding rule for wages."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def weight(status: AttendanceStatus) -> Decimal:
    return STATUS_WEIGHT[status]


def rate_for_new_day(worker: User) -> Optional[Decimal]:
    """The rate a day takes when it is first marked. See `Attendance.wage_rate`.

    0 for a piece-rate worker (not paid by the day, never priced later), NULL
    when the worker has no positive day rate yet (priced when one is set),
    otherwise the rate itself.

    A rate of zero typed into the form is read as "not known yet" rather than
    "works for free": the form treats the field as optional, and a day booked
    at an explicit zero could never be put right by filling the rate in later.
    """
    if worker.wage_type is WageType.PIECE_RATE:
        return ZERO
    if worker.daily_wage is None:
        return None
    rate = money(Decimal(worker.daily_wage))
    return rate if rate > ZERO else None


def expected_wage(status: AttendanceStatus, wage_rate: Optional[Decimal]) -> Optional[Decimal]:
    """What a day at `status` and `wage_rate` earns, or None if nothing.

    None and zero are the same answer and both mean "write no row": a
    zero-amount WAGE would sit in every ledger and statement saying nothing,
    and `TransactionCreate` refuses non-positive amounts anyway.
    """
    share = weight(status)
    if share <= ZERO or wage_rate is None:
        return None
    earned = money(Decimal(wage_rate) * share)
    return earned if earned > ZERO else None


def wage_note(status: AttendanceStatus) -> str:
    return HALF_DAY_NOTE if status is AttendanceStatus.HALF_DAY else WAGE_NOTE


def sync_attendance_wage(
    db: Session, attendance: Attendance, worker: User
) -> Optional[Transaction]:
    """Make the WAGE linked to `attendance` match what the row now says.

    Reads the row's own `wage_rate` and status - never the worker's current
    rate, which is the whole point of freezing it. Safe to call on an
    unchanged row, on a row whose status or date just moved, and repeatedly.

    Returns the live wage, or None when the day earns nothing.

    Does NOT commit. The caller owns the transaction boundary, so the wage and
    the day it came from land together or not at all.
    """
    earned = expected_wage(attendance.status, attendance.wage_rate)
    existing = (
        db.get(Transaction, attendance.transaction_id)
        if attendance.transaction_id is not None
        else None
    )

    if earned is None:
        # The day stopped earning - corrected to absent, or rained out. Drop
        # the wage rather than leaving a stale one.
        if existing is not None:
            attendance.transaction_id = None
            db.delete(existing)
            # So a caller that reads the balance before committing does not
            # still see the row being removed.
            db.flush()
        return None

    note = wage_note(attendance.status)

    if existing is None:
        wage = Transaction(
            worker_id=worker.id,
            type=TransactionType.WAGE,
            amount=earned,
            date=attendance.date,
            # Where the day was worked - see `Transaction.site_id`.
            site_id=attendance.site_id,
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
    if existing.site_id != attendance.site_id:
        existing.site_id = attendance.site_id
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


def reprice_days(
    db: Session,
    worker: User,
    from_date: Optional[date_type] = None,
    to_date: Optional[date_type] = None,
) -> int:
    """Move days already marked onto the worker's current rate. Explicit only.

    The correction for a rate typed wrong: every day-rated day in the window
    (all of them when no window is given) takes the current rate and its wage
    follows. Piece-rate days (`wage_rate` 0) are left alone - they were never
    paid by the day. Returns the number of days whose rate changed.

    Refuses nothing itself; the route checks the worker is on a day rate with
    a positive rate, because repricing onto "no rate" would strip wages that
    were earned. Call `price_unpriced_days` first, so days that never had a
    rate adopt any wage typed for them by hand rather than being paid twice.
    """
    rate = rate_for_new_day(worker)
    if rate is None or rate <= ZERO:
        return 0

    stmt = select(Attendance).where(
        Attendance.worker_id == worker.id,
        # NULL (unpriced) or a real rate; 0 marks a piece-rate day.
        (Attendance.wage_rate.is_(None)) | (Attendance.wage_rate > ZERO),
    )
    if from_date is not None:
        stmt = stmt.where(Attendance.date >= from_date)
    if to_date is not None:
        stmt = stmt.where(Attendance.date <= to_date)

    changed = 0
    for row in db.scalars(stmt).all():
        if row.wage_rate != rate:
            row.wage_rate = rate
            changed += 1
        sync_attendance_wage(db, row, worker)
    return changed
