"""Settling up: what a worker is owed for a period, and clearing it to zero."""

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.dependencies import CurrentContractor, DbSession
from app.models.enums import TransactionType, UserRole
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.payroll import PayrollSettlement, PayrollSettleRequest, PayrollSummary
from app.schemas.transaction import TransactionRead
from app.services.ledger import calculate_balance
from app.services.notifications import notify_transaction, should_notify
from app.services.payroll import build_clearing_transaction, build_summary

router = APIRouter(prefix="/payroll", tags=["payroll"])


def _get_own_worker(db: Session, contractor: User, worker_id: int) -> User:
    """Same rule as the contractor router: someone else's worker is a 404."""
    worker = db.get(User, worker_id)
    if (
        worker is None
        or worker.role is not UserRole.WORKER
        or worker.contractor_id != contractor.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found"
        )
    return worker


def _check_window(end_date: Optional[date_type]) -> None:
    if end_date is not None and end_date > business_today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A period cannot be settled before it has ended",
        )


def _settlement_by_ref(
    db: Session, worker: User, client_ref: str
) -> Optional[PayrollSettlement]:
    """The settlement a client_ref already wrote for this worker, if any."""
    transaction = db.scalar(
        select(Transaction).where(
            Transaction.client_ref == client_ref,
            Transaction.worker_id == worker.id,
            Transaction.type == TransactionType.PAYMENT,
        )
    )
    if transaction is None:
        return None
    # The summary is rebuilt for the window the payment closed, which is what
    # the first response showed; the balance is today's.
    summary = build_summary(db, worker, None, transaction.date)
    return PayrollSettlement(
        summary=summary,
        transaction=TransactionRead.model_validate(transaction),
        balance_after=calculate_balance(db, worker.id).balance,
    )


@router.get("/preview", response_model=PayrollSummary)
def preview_settlement(
    contractor: CurrentContractor,
    db: DbSession,
    worker_id: int = Query(gt=0),
    start_date: Optional[date_type] = Query(
        default=None, description="Omit to cover the worker's whole history"
    ),
    end_date: Optional[date_type] = Query(default=None, description="Defaults to today"),
) -> PayrollSummary:
    """What settling this window would pay, without writing anything.

    Backs the confirmation screen: the contractor sees the days, the wages,
    what has already been advanced against them, and the figure they are about
    to hand over — before anything is committed.
    """
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must not be after end_date",
        )
    _check_window(end_date)

    worker = _get_own_worker(db, contractor, worker_id)
    return build_summary(db, worker, start_date, end_date)


@router.post("/settle", response_model=PayrollSettlement, status_code=status.HTTP_201_CREATED)
def settle_payroll(
    payload: PayrollSettleRequest,
    contractor: CurrentContractor,
    db: DbSession,
    background: BackgroundTasks,
) -> PayrollSettlement:
    """Pay off what a worker is owed for a period and take the balance to zero.

    Writes one PAYMENT row for `wages - advances - payments` over the window,
    dated the window's end so it falls inside the period it settles.

    Repeating the call is safe in the ordinary case: the first settlement is
    itself inside the window, so a second attempt computes zero payable and is
    refused.

    TWO TAPS AT ONCE
    ----------------
    Both used to compute the same payable before either had written, and both
    wrote it - the worker paid twice. Now the worker's row is locked first, so
    the second request waits, then recomputes against the first one's payment
    and finds nothing left to pay. And a request carrying a `client_ref` that
    already settled gets that settlement back, so a retry whose response was
    lost is answered with what actually happened rather than a 409.
    """
    _check_window(payload.end_date)
    worker = _get_own_worker(db, contractor, payload.worker_id)

    if payload.client_ref:
        done = _settlement_by_ref(db, worker, payload.client_ref)
        if done is not None:
            return done

    # Serialise settlements for this worker (a no-op on SQLite). Everything
    # below reads the ledger, so it must read it after any racing settlement
    # has committed.
    db.scalar(select(User.id).where(User.id == worker.id).with_for_update())

    summary = build_summary(db, worker, payload.start_date, payload.end_date)

    if not summary.can_settle:
        # Three different situations, and telling them apart is the difference
        # between "you are done", "you already did this" and "something is
        # wrong".
        if summary.net_payable < 0:
            detail = (
                f"{worker.name} has taken {abs(summary.net_payable)} more than they have "
                "earned. Record the wages first, or widen the dates."
            )
        elif summary.window_balance > 0:
            # The window still shows wages, but they were cleared by a
            # settlement dated outside it.
            detail = (
                f"{worker.name} has already been settled - their balance is clear. "
                "Widen the dates to see the payment that cleared it."
            )
        else:
            detail = "There is nothing to settle for this period"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    transaction = build_clearing_transaction(summary, payload.payment_mode, payload.note)
    transaction.client_ref = payload.client_ref
    # Paid out where the worker is posted. A settlement clears every site the
    # worker earned on; the site it is filed under is where the cash left.
    transaction.site_id = worker.site_id
    db.add(transaction)
    try:
        db.commit()
    except IntegrityError:
        # The same key committed by a racing request. Return its settlement.
        db.rollback()
        done = (
            _settlement_by_ref(db, worker, payload.client_ref)
            if payload.client_ref
            else None
        )
        if done is not None:
            return done
        if payload.client_ref:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This settlement's reference is already in use. Try again.",
            ) from None
        raise
    db.refresh(transaction)

    balance_after = calculate_balance(db, worker.id).balance

    if should_notify(transaction):
        # Read while the session is open — the background task runs after the
        # request's session has been closed.
        background.add_task(
            notify_transaction, transaction, worker, contractor, balance_after
        )

    return PayrollSettlement(
        summary=summary,
        transaction=TransactionRead.model_validate(transaction),
        balance_after=balance_after,
    )
