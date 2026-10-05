"""Taking an advance back a little at a time.

THE ARITHMETIC, IN ONE PLACE
----------------------------
`calculate_balance` is the single definition of what a worker is owed and this
module does not fork it. An advance under a plan is still an ADVANCE row and
still lands in `total_advances` — the worker's own screen and the contractor's
must not start disagreeing about what was handed over.

What changes is what may be PAID for a period. `payroll_adjustment` returns
the numbers that turn a ledger balance into a payable one:

    net_payable = window_balance + emi_advances - installments_recovered

Read it as: the advance is not due this period in full, so put it back; the
installments actually taken are due instead, so subtract those. Over the life
of a plan the two sides meet — every rupee of principal comes back as
installments — so nothing is recovered twice or forgiven by accident.

WHY INSTALLMENTS ARE ROWS
-------------------------
`balance_remaining` alone could not answer "how much was recovered in March",
and a windowed settlement needs exactly that. Each deduction taken is a dated
row, `balance_remaining` is their rollup, and `recompute_balance` below is the
only thing allowed to move it.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.deduction import AutoDeduction, DeductionInstallment
from app.models.enums import AttendanceStatus
from app.models.transaction import Transaction
from app.services.ledger import CENTS, ZERO
from app.services.wages import EARNING_STATUSES, HALF

# `EARNING_STATUSES` and `HALF` come from app/services/wages.py. Attendance
# that earns a day earns a day's deduction too; an absence takes nothing off -
# there were no wages to take it from, and quietly deepening a worker's debt on
# a day they did not work is how an installment arrangement turns into a trap.
# A half day recovers half an installment, the same proportion as its wage.
# Imported rather than restated, so the two cannot drift apart.


def _money(value: Optional[Decimal]) -> Decimal:
    return (Decimal(value) if value is not None else ZERO).quantize(CENTS)


def active_plans(db: Session, worker_ids: list[int]) -> dict[int, AutoDeduction]:
    """The one active plan per worker, for the workers asked about.

    One per worker by convention rather than by constraint: the endpoint that
    opens a plan refuses a second while one is running, because two plans
    against one worker would take two deductions a day and neither would be
    the number the contractor agreed.
    """
    if not worker_ids:
        return {}

    rows = db.scalars(
        select(AutoDeduction)
        .where(
            AutoDeduction.worker_id.in_(worker_ids),
            AutoDeduction.is_active.is_(True),
            AutoDeduction.balance_remaining > ZERO,
        )
        .order_by(AutoDeduction.id)
        # Locked for the rest of the request. Two requests taking different
        # days off one plan at once would otherwise each recompute
        # `balance_remaining` from a sum that cannot see the other's
        # uncommitted installment, and the later commit would overwrite the
        # earlier one's figure - a debt that reads higher than it is. Under
        # the lock the second waits, then sums both. (A no-op on SQLite.)
        .with_for_update()
    ).all()

    # Oldest wins if a second plan ever got past the guard — recovering the
    # debt outstanding longest is the defensible choice.
    plans: dict[int, AutoDeduction] = {}
    for plan in rows:
        plans.setdefault(plan.worker_id, plan)
    return plans


def recompute_balance(db: Session, plan: AutoDeduction) -> None:
    """Re-derive `balance_remaining` from the installments, and close at zero.

    Called after every write that touches a plan's installments. Deriving
    rather than decrementing means a deleted or corrected installment cannot
    leave the rollup describing a debt that is not there.
    """
    recovered = _money(
        db.scalar(
            select(func.sum(DeductionInstallment.amount)).where(
                DeductionInstallment.deduction_id == plan.id
            )
        )
    )
    remaining = _money(plan.principal_amount) - recovered
    plan.balance_remaining = max(remaining, ZERO)
    if plan.balance_remaining <= ZERO:
        # Paid off. Stop collecting, rather than leaving a plan that takes
        # zero-rupee installments for the rest of the job.
        plan.is_active = False


def apply_installment(
    db: Session,
    plan: AutoDeduction,
    when: date_type,
    status: AttendanceStatus,
) -> Optional[DeductionInstallment]:
    """Take one day's deduction off a plan. Returns None when nothing is due.

    Nothing is due when the worker did not earn that day, when the plan is
    already clear, or when this day has already been deducted — the last of
    which is enforced by the database rather than by the check below, so two
    requests racing on one day still take a single installment between them.

    The final installment is capped at whatever is left: a 5,000 plan at 200 a
    day ends on the exact remainder rather than overshooting into a negative
    balance the contractor would then owe back.
    """
    if status not in EARNING_STATUSES:
        return None
    if not plan.is_active or _money(plan.balance_remaining) <= ZERO:
        return None

    due = _money(plan.daily_deduction)
    if status is AttendanceStatus.HALF_DAY:
        due = _money(due * HALF)
    due = min(due, _money(plan.balance_remaining))
    if due <= ZERO:
        return None

    installment = DeductionInstallment(
        deduction_id=plan.id,
        worker_id=plan.worker_id,
        amount=due,
        date=when,
    )
    try:
        # A SAVEPOINT around the insert, so a duplicate day rolls back this
        # row alone rather than taking the whole crew's attendance with it.
        #
        # The `add` has to be INSIDE the savepoint. Added outside, the pending
        # object survives the rollback, gets picked up by the next flush and
        # fails again — this time with nothing to catch it.
        with db.begin_nested():
            db.add(installment)
            db.flush()
    except IntegrityError:
        # uq_deduction_installment_day — this day is already recovered.
        return None

    recompute_balance(db, plan)
    return installment


def apply_installment_for(
    db: Session, worker_id: int, when: date_type, status: AttendanceStatus
) -> Optional[DeductionInstallment]:
    """Take the day's installment off this worker's active plan, if they have one."""
    plan = active_plans(db, [worker_id]).get(worker_id)
    if plan is None:
        return None
    return apply_installment(db, plan, when, status)


def resettle_installment(
    db: Session,
    worker_id: int,
    old_date: date_type,
    old_status: AttendanceStatus,
    new_date: date_type,
    new_status: AttendanceStatus,
) -> Optional[DeductionInstallment]:
    """Move a day's installment to match a corrected attendance row.

    A day marked present under an active plan takes that day's deduction.
    Correcting the day to absent has to give it back, or the worker quietly
    pays an installment for a day they did not work — which is the exact thing
    `EARNING_STATUSES` exists to prevent, and an edit must not be a way around
    it.

    EVERY PATH THAT CHANGES A DAY'S STATUS COMES THROUGH HERE: the single
    correction, an overwriting grid resubmission, a rain-out over a day
    already marked present, a QR re-scan, and an unmark (with ABSENT as the
    new status, which is "give it back and take nothing"). The first version
    only had the single correction, so a rain-out over a present day kept the
    installment the present mark had taken.

    Done by deleting the old day's row and re-applying against the new one,
    rather than by arithmetic on `balance_remaining`: that column is a rollup
    and `recompute_balance` is the only thing allowed to move it.

    ONE THING IT DELIBERATELY DOES NOT DO
    -------------------------------------
    A plan closed by its final installment stays closed when that installment
    is reversed, leaving a small balance on an inactive plan. `is_active=False`
    means both "paid off" and "written off by hand" and the row cannot tell
    them apart, so reopening one the contractor deliberately closed is the
    worse mistake. The deductions screen shows the balance and can reopen it.
    """
    if (old_date, old_status) == (new_date, new_status):
        return None

    # Found through the installment itself rather than through `active_plans`,
    # so a day can still be given back after its plan has closed.
    stale = db.scalar(
        select(DeductionInstallment).where(
            DeductionInstallment.worker_id == worker_id,
            DeductionInstallment.date == old_date,
        )
    )
    if stale is not None:
        # Locked for the same reason `active_plans` locks: the rollup below is
        # recomputed from a sum another request could be changing.
        plan = db.get(AutoDeduction, stale.deduction_id, with_for_update=True)
        db.delete(stale)
        # So the sum below does not still see the row being removed.
        db.flush()
        if plan is not None:
            recompute_balance(db, plan)

    return apply_installment_for(db, worker_id, new_date, new_status)


def payroll_adjustment(
    db: Session,
    worker_id: int,
    start: Optional[date_type],
    end: date_type,
) -> tuple[Decimal, Decimal, Decimal]:
    """(emi_advances, installments_recovered, emi_outstanding) for a window.

    `emi_advances` is the principal of plan-backed ADVANCE rows dated inside
    the window — money the ledger has already subtracted in full and that
    payroll adds back, because it is not due this period.

    `installments_recovered` is what was actually taken inside the window,
    which is what payroll subtracts instead.

    `emi_outstanding` is every active plan's remaining balance, all time. It
    is not part of the window arithmetic; it is the debt carried forward, and
    the honest answer to a worker asking why their advance is still there.
    """
    # Only advances a plan actually points at. A plan opened by hand against
    # no transaction adjusts nothing here: there is no ledger row to put back,
    # so subtracting its installments would take money the ledger never had.
    advances_stmt = (
        select(func.sum(Transaction.amount))
        .join(AutoDeduction, AutoDeduction.transaction_id == Transaction.id)
        .where(AutoDeduction.worker_id == worker_id, Transaction.date <= end)
    )
    if start is not None:
        advances_stmt = advances_stmt.where(Transaction.date >= start)

    installments_stmt = select(func.sum(DeductionInstallment.amount)).where(
        DeductionInstallment.worker_id == worker_id,
        DeductionInstallment.date <= end,
    )
    if start is not None:
        installments_stmt = installments_stmt.where(DeductionInstallment.date >= start)

    outstanding_stmt = select(func.sum(AutoDeduction.balance_remaining)).where(
        AutoDeduction.worker_id == worker_id, AutoDeduction.is_active.is_(True)
    )

    return (
        _money(db.scalar(advances_stmt)),
        _money(db.scalar(installments_stmt)),
        _money(db.scalar(outstanding_stmt)),
    )
