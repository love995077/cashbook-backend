"""Piece work, and the installment plans that recover advances from it.

Two concerns, together because they are the two halves of what a piece-rate
worker's pay actually looks like: what they earned by output, and what is
being taken back off it.

EVERY PIECE-RATE LOG WRITES A WAGE
----------------------------------
A log is not a second definition of what a worker is owed. It creates the
matching WAGE `Transaction` in the same database transaction and keeps its id,
the way an approved `AdvanceRequest` does. Without that the balance, the
worker's own screen, payroll and the PDF statement would all silently ignore
piece work.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.clock import business_today
from app.dependencies import (
    CurrentContractor,
    DbSession,
    require_own_site,
    require_own_worker,
)
from app.models.deduction import AutoDeduction, DeductionInstallment
from app.models.enums import TransactionType
from app.models.piece_rate import PieceRateLog
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.deduction import (
    AutoDeductionCreate,
    AutoDeductionRead,
    AutoDeductionUpdate,
    DeductionInstallmentRead,
)
from app.schemas.piece_rate import PieceRateLogCreate, PieceRateLogRead
from app.services.attendance import check_markable
from app.services.deductions import recompute_balance
from app.services.ledger import CENTS

router = APIRouter(prefix="/contractor", tags=["piece rate"])


def _read_log(log: PieceRateLog) -> PieceRateLogRead:
    read = PieceRateLogRead.model_validate(log)
    read.worker_name = log.worker.name if log.worker else None
    return read


def _read_plan(plan: AutoDeduction) -> AutoDeductionRead:
    read = AutoDeductionRead.model_validate(plan)
    read.worker_name = plan.worker.name if plan.worker else None
    read.recovered_amount = plan.recovered_amount
    return read


def build_piece_rate_log(
    worker: User,
    work_description: str,
    quantity: Decimal,
    rate: Decimal,
    when: date_type,
    site_id: Optional[int] = None,
    note: Optional[str] = None,
) -> tuple[PieceRateLog, Transaction]:
    """The log and the WAGE it earns, unsaved and already linked by the caller.

    Shared with the bulk attendance route, so a tiler's quantity typed into
    the grid and one typed into the piece-rate form produce identical rows —
    including the note, which is what makes the ledger entry explicable
    without the log beside it.
    """
    total = (Decimal(quantity) * Decimal(rate)).quantize(CENTS)

    transaction = Transaction(
        worker_id=worker.id,
        type=TransactionType.WAGE,
        amount=total,
        date=when,
        # Where the work was done, the same site the log records.
        site_id=site_id,
        # No payment_mode: a wage is money earned, not money handed over. The
        # transactions endpoint enforces the same rule.
        note=(note or f"{work_description} — {quantity} x {rate}")[:255],
    )
    log = PieceRateLog(
        worker_id=worker.id,
        site_id=site_id,
        work_description=work_description,
        quantity=quantity,
        rate=rate,
        total_amount=total,
        date=when,
    )
    return log, transaction


# --- piece-rate work --------------------------------------------------------


@router.get("/piece-rate-logs", response_model=list[PieceRateLogRead])
def list_piece_rate_logs(
    contractor: CurrentContractor,
    db: DbSession,
    worker_id: Optional[int] = Query(default=None, gt=0),
    site_id: Optional[int] = Query(default=None, gt=0),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PieceRateLogRead]:
    """Piece work logged for this contractor's crew, newest first."""
    stmt = (
        select(PieceRateLog)
        .join(User, User.id == PieceRateLog.worker_id)
        .options(joinedload(PieceRateLog.worker))
        .where(User.contractor_id == contractor.id)
    )
    if worker_id is not None:
        require_own_worker(db, contractor, worker_id)
        stmt = stmt.where(PieceRateLog.worker_id == worker_id)
    if site_id is not None:
        require_own_site(db, contractor, site_id)
        stmt = stmt.where(PieceRateLog.site_id == site_id)
    if start_date is not None:
        stmt = stmt.where(PieceRateLog.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(PieceRateLog.date <= end_date)

    logs = db.scalars(
        stmt.order_by(PieceRateLog.date.desc(), PieceRateLog.id.desc()).limit(limit)
    ).all()
    return [_read_log(log) for log in logs]


@router.post(
    "/piece-rate-logs",
    response_model=PieceRateLogRead,
    status_code=status.HTTP_201_CREATED,
)
def add_piece_rate_log(
    payload: PieceRateLogCreate, contractor: CurrentContractor, db: DbSession
) -> PieceRateLogRead:
    """Log a measured piece of work, and the wage it earned."""
    worker = require_own_worker(db, contractor, payload.worker_id)

    when = payload.date or business_today()
    if when > business_today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Work cannot be logged before it has been done",
        )
    # A worker taken off the crew earns nothing for days after they left -
    # the same rule the attendance grid applies. See `check_markable`.
    check_markable(worker, when)

    site_id = payload.site_id
    if site_id is not None:
        require_own_site(db, contractor, site_id)

    log, transaction = build_piece_rate_log(
        worker,
        payload.work_description,
        payload.quantity,
        payload.rate,
        when,
        # Falls back to wherever the worker is posted, so the common case does
        # not need the site picked by hand for the second time today.
        site_id if site_id is not None else worker.site_id,
        payload.note,
    )

    db.add(transaction)
    # Needed before the log can point at it; both are committed together.
    db.flush()
    log.transaction_id = transaction.id
    db.add(log)
    db.commit()
    db.refresh(log)
    return _read_log(log)


# --- installment plans ------------------------------------------------------


@router.get("/deductions", response_model=list[AutoDeductionRead])
def list_deductions(
    contractor: CurrentContractor,
    db: DbSession,
    worker_id: Optional[int] = Query(default=None, gt=0),
    active_only: bool = Query(default=True),
) -> list[AutoDeductionRead]:
    """Installment plans against this contractor's crew."""
    stmt = (
        select(AutoDeduction)
        .join(User, User.id == AutoDeduction.worker_id)
        .options(joinedload(AutoDeduction.worker))
        .where(User.contractor_id == contractor.id)
    )
    if worker_id is not None:
        require_own_worker(db, contractor, worker_id)
        stmt = stmt.where(AutoDeduction.worker_id == worker_id)
    if active_only:
        stmt = stmt.where(AutoDeduction.is_active.is_(True))

    plans = db.scalars(stmt.order_by(AutoDeduction.id.desc())).all()
    return [_read_plan(plan) for plan in plans]


@router.post(
    "/deductions", response_model=AutoDeductionRead, status_code=status.HTTP_201_CREATED
)
def add_deduction(
    payload: AutoDeductionCreate, contractor: CurrentContractor, db: DbSession
) -> AutoDeductionRead:
    """Open a plan against a debt that was not created through this app.

    The usual route is the "Recover via daily installments" switch on an
    advance, which opens the plan and links it to the ADVANCE row in one go.
    This is for a debt agreed off the books, and it deliberately does NOT
    link a transaction — see `payroll_adjustment`, which only puts back
    advances a plan actually points at.
    """
    require_own_worker(db, contractor, payload.worker_id)
    ensure_no_active_plan(db, payload.worker_id)

    plan = AutoDeduction(
        worker_id=payload.worker_id,
        principal_amount=payload.principal_amount,
        daily_deduction=payload.daily_deduction,
        balance_remaining=payload.principal_amount,
        note=payload.note,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _read_plan(plan)


def ensure_no_active_plan(db: DbSession, worker_id: int) -> None:
    """Refuse a second plan while one is running.

    Two plans against one worker would take two deductions a day, and neither
    would be the number the contractor agreed. `active_plans` picks the oldest
    if one ever gets past this, but the right place to stop it is here.
    """
    existing = db.scalar(
        select(AutoDeduction).where(
            AutoDeduction.worker_id == worker_id, AutoDeduction.is_active.is_(True)
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This worker is already repaying {existing.balance_remaining} "
                f"at {existing.daily_deduction} a day. Close that plan first."
            ),
        )


@router.patch("/deductions/{deduction_id}", response_model=AutoDeductionRead)
def edit_deduction(
    deduction_id: int,
    payload: AutoDeductionUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> AutoDeductionRead:
    """Change the daily rate, or stop collecting.

    Setting `is_active` false writes the rest off: collection stops and
    payroll stops adding the unrecovered principal back, so the remainder
    lands on the worker's balance as the plain advance it always was.
    """
    plan = db.get(AutoDeduction, deduction_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Deduction plan not found"
        )
    require_own_worker(db, contractor, plan.worker_id)

    fields = payload.model_dump(exclude_unset=True)
    if fields.get("daily_deduction") is not None and fields[
        "daily_deduction"
    ] > plan.balance_remaining:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The daily deduction cannot be larger than the "
                f"{plan.balance_remaining} still to recover"
            ),
        )

    for field, value in fields.items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return _read_plan(plan)


@router.get(
    "/deductions/{deduction_id}/installments",
    response_model=list[DeductionInstallmentRead],
)
def list_installments(
    deduction_id: int, contractor: CurrentContractor, db: DbSession
) -> list[DeductionInstallment]:
    """Every deduction actually taken off this plan, oldest first.

    The answer to "which days did you take 200 off me?", which a rollup on
    the plan row could never give.
    """
    plan = db.get(AutoDeduction, deduction_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Deduction plan not found"
        )
    require_own_worker(db, contractor, plan.worker_id)

    return list(
        db.scalars(
            select(DeductionInstallment)
            .where(DeductionInstallment.deduction_id == deduction_id)
            .order_by(DeductionInstallment.date, DeductionInstallment.id)
        ).all()
    )


@router.post("/deductions/{deduction_id}/recompute", response_model=AutoDeductionRead)
def recompute_deduction(
    deduction_id: int, contractor: CurrentContractor, db: DbSession
) -> AutoDeductionRead:
    """Re-derive `balance_remaining` from the installments behind it.

    A repair hatch, not part of the normal flow: the rollup is maintained on
    every write. It exists because the rollup is derived data, and derived
    data should always have a way back to the rows it came from.
    """
    plan = db.get(AutoDeduction, deduction_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Deduction plan not found"
        )
    require_own_worker(db, contractor, plan.worker_id)

    recompute_balance(db, plan)
    db.commit()
    db.refresh(plan)
    return _read_plan(plan)
