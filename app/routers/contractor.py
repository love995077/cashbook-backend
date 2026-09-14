from datetime import date as date_type
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.security import hash_password
from app.dependencies import (
    CurrentContractor,
    CurrentSiteStaff,
    DbSession,
    accessible_site_ids,
    require_site_access,
    require_site_worker,
)
from app.models.advance_request import AdvanceRequest
from app.models.attendance import Attendance
from app.models.cash import CashCategory, CashEntry
from app.models.deduction import AutoDeduction, DeductionInstallment
from app.models.enums import (
    AdvanceRequestStatus,
    AttendanceStatus,
    CashDirection,
    PaymentMode,
    TransactionType,
    UserRole,
)
from app.models.piece_rate import PieceRateLog
from app.models.site import Site
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.advance_request import AdvanceRequestDecision, AdvanceRequestRead
from app.routers.media import attach_audio_note
from app.schemas.attendance import (
    AttendanceCreate,
    AttendanceRead,
    AttendanceScan,
    AttendanceUpdate,
)
from app.schemas.bulk_attendance import BulkAttendanceRequest, BulkAttendanceResult
from app.schemas.deduction import DeductionInstallmentRead
from app.schemas.cash import (
    AnalyticsSeries,
    CashCategoryCreate,
    CashCategoryRead,
    CashCategoryUpdate,
    CashEntryCreate,
    CashEntryRead,
    CashEntryUpdate,
    CashSummary,
)
from app.schemas.ledger import WorkerLedger
from app.schemas.piece_rate import PieceRateLogRead
from app.schemas.transaction import TransactionCreate, TransactionRead
from app.schemas.user import UserRead, WorkerCreate, WorkerUpdate
from app.services.cash import (
    build_series,
    build_summary,
    ensure_default_categories,
)
from app.routers.piece_rate import build_piece_rate_log, ensure_no_active_plan
from app.services.deductions import active_plans, apply_installment, recompute_balance
from app.services.ledger import build_worker_ledger, calculate_balance
from app.services.storage import public_url
from app.services.notifications import notify_transaction, should_notify

router = APIRouter(prefix="/contractor", tags=["contractor"])


def _get_own_worker(db: Session, contractor: User, worker_id: int) -> User:
    """Fetch a worker, refusing anyone who is not on this contractor's crew.

    Workers belonging to someone else get a 404 rather than a 403, so the
    endpoint does not confirm that the other worker exists.
    """
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


def _check_site(db: Session, contractor: User, site_id: Optional[int]) -> Optional[int]:
    """Refuse a site that is not this contractor's. 0 and None both mean unposted."""
    if not site_id:
        return None
    site = db.get(Site, site_id)
    if site is None or site.contractor_id != contractor.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    return site.id


def _check_staff_site(db: Session, staff: User, site_id: Optional[int]) -> Optional[int]:
    """The same check, widened to a supervisor and their assigned site.

    An admin may leave the site out entirely — a contractor running one job
    has never needed sites. A supervisor may not: their authority IS a site,
    so a request without one has nothing to check them against and is refused
    rather than silently treated as "all of them".
    """
    if staff.role is UserRole.ADMIN:
        return _check_site(db, staff, site_id)

    if not site_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A supervisor must say which site the day is being marked for",
        )
    return require_site_access(db, staff, site_id).id


@router.post("/workers", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def add_worker(
    payload: WorkerCreate, contractor: CurrentContractor, db: DbSession
) -> User:
    """Add a worker to the calling contractor's crew.

    Only the name is required. A worker booked without a phone number or a PIN
    is a record the contractor keeps rather than an account that can sign in;
    both can be filled in later. See `WorkerCreate`.
    """
    # Only worth asking when a number was actually given. `User.phone` is
    # nullable and MySQL allows many NULLs in a unique index, so an absent
    # number cannot collide with anything.
    if payload.phone is not None:
        if db.scalar(select(User).where(User.phone == payload.phone)) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this phone number already exists",
            )

    worker = User(
        name=payload.name,
        phone=payload.phone,
        # No PIN means no digest to compare, which the login route reads as
        # "this worker cannot sign in" rather than hashing an empty string —
        # that would be a real password, and a guessable one.
        password_hash=hash_password(payload.password) if payload.password else None,
        role=UserRole.WORKER,
        daily_wage=payload.daily_wage,
        trade=payload.trade,
        wage_type=payload.wage_type,
        site_id=_check_site(db, contractor, payload.site_id),
        contractor_id=contractor.id,
    )
    db.add(worker)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this phone number already exists",
        ) from None
    db.refresh(worker)
    return worker


@router.get("/workers", response_model=list[UserRead])
def list_workers(
    staff: CurrentSiteStaff,
    db: DbSession,
    include_inactive: bool = Query(default=False),
    role: Literal["worker", "supervisor"] = Query(
        default="worker",
        description="Which of the contractor's people to list. Admin only for supervisors.",
    ),
) -> list[User]:
    """The crew this caller may see.

    An admin sees everyone on their books. A supervisor sees only the workers
    posted to a site they run — enough to mark the day and hand out tools, and
    nothing about the crews on the contractor's other jobs.

    `role=supervisor` lists the contractor's supervisors instead, which is how
    the app fills the picker on the site screen; a site with nobody to assign
    to it is a role that can never be used. Admin only, and deliberately: a
    supervisor has no business enumerating the others.
    """
    wanted = UserRole.SUPERVISOR if role == "supervisor" else UserRole.WORKER

    if staff.role is not UserRole.ADMIN:
        if wanted is UserRole.SUPERVISOR:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an admin can list supervisors",
            )
        reachable = accessible_site_ids(db, staff)
        if not reachable:
            return []
        stmt = select(User).where(User.site_id.in_(reachable), User.role == wanted)
    else:
        stmt = select(User).where(User.contractor_id == staff.id, User.role == wanted)
    # Removed workers are deactivated rather than deleted (see remove_worker),
    # so the default list has to exclude them or "remove" would appear to do
    # nothing at all.
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))

    return list(db.scalars(stmt.order_by(User.name)).all())


@router.patch("/workers/{worker_id}", response_model=UserRead)
def edit_worker(
    worker_id: int,
    payload: WorkerUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> User:
    """Change a worker's details, chiefly which site they are posted to.

    Posting the crew to a site is what makes the site screens mean anything —
    the gallery, the crew list and the rain-out button all read `site_id`.
    """
    worker = _get_own_worker(db, contractor, worker_id)

    fields = payload.model_dump(exclude_unset=True)
    if "site_id" in fields:
        # Sent explicitly, so None here means "take them off the site" rather
        # than "leave it alone".
        fields["site_id"] = _check_site(db, contractor, fields["site_id"])

    for field, value in fields.items():
        setattr(worker, field, value)

    db.commit()
    db.refresh(worker)
    return worker


@router.delete("/workers/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_worker(
    worker_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    purge: bool = Query(
        default=False,
        description=(
            "Delete the row outright. Refused once the worker has any history — "
            "use it only to undo a mistyped entry."
        ),
    ),
) -> Response:
    """Take a worker off the crew.

    DEACTIVATES BY DEFAULT, AND THAT IS DELIBERATE
    ---------------------------------------------
    Every table that matters hangs off `users.id` with `ondelete=CASCADE`:
    attendance, transactions, advance_requests, piece_rate_logs,
    auto_deductions, deduction_installments, tool_assignments. A hard delete
    of a worker who has been paid for six months therefore erases six months
    of the CONTRACTOR'S OWN books — the wages they paid vanish from every
    total, and the cash box stops reconciling. "Remove from my crew" and
    "erase the record that I ever paid this person" are different requests,
    and only the first one is being made here.

    So the row stays, `is_active` goes false, and the worker drops out of the
    crew list, the attendance grid and the payroll preview while their ledger
    survives for the reports that already went out.

    `?purge=true` is the escape hatch for the real case a soft delete does not
    cover: a name typed wrong thirty seconds ago. It is refused the moment any
    history exists, so it can never be the thing that quietly deletes a
    ledger.
    """
    worker = _get_own_worker(db, contractor, worker_id)

    if purge:
        # Counted rather than fetched: the question is only whether anything
        # points at this worker at all.
        history = {
            "attendance": db.scalar(
                select(func.count())
                .select_from(Attendance)
                .where(Attendance.worker_id == worker.id)
            ),
            "transactions": db.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.worker_id == worker.id)
            ),
            "advance requests": db.scalar(
                select(func.count())
                .select_from(AdvanceRequest)
                .where(AdvanceRequest.worker_id == worker.id)
            ),
            "piece-rate logs": db.scalar(
                select(func.count())
                .select_from(PieceRateLog)
                .where(PieceRateLog.worker_id == worker.id)
            ),
            "deduction plans": db.scalar(
                select(func.count())
                .select_from(AutoDeduction)
                .where(AutoDeduction.worker_id == worker.id)
            ),
        }
        blocking = [f"{count} {label}" for label, count in history.items() if count]
        if blocking:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{worker.name} has "
                    + ", ".join(blocking)
                    + " on record. Deleting the row would delete that history too. "
                    "Remove them from the crew instead — their ledger is kept."
                ),
            )
        db.delete(worker)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    worker.is_active = False
    # Taken off the site as well, so the rain-out button and the site crew
    # list stop counting someone who is no longer on the job.
    worker.site_id = None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/attendance", response_model=AttendanceRead, status_code=status.HTTP_201_CREATED
)
def log_attendance(
    payload: AttendanceCreate, contractor: CurrentContractor, db: DbSession
) -> Attendance:
    """Record one day's attendance for a worker."""
    _get_own_worker(db, contractor, payload.worker_id)

    attendance = Attendance(
        worker_id=payload.worker_id,
        date=payload.date,
        status=payload.status,
        shift=payload.shift,
        note=payload.note,
    )
    db.add(attendance)
    try:
        db.commit()
    except IntegrityError:
        # One attendance row per worker per day (uq_attendance_worker_date).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attendance for this worker on this date has already been logged",
        ) from None
    db.refresh(attendance)
    return attendance


def _resettle_installment(
    db: Session,
    worker_id: int,
    old_date: date_type,
    old_status: AttendanceStatus,
    new_date: date_type,
    new_status: AttendanceStatus,
) -> None:
    """Move a day's installment to match a corrected attendance row.

    A day marked present under an active plan takes that day's deduction.
    Correcting the day to absent has to give it back, or the worker quietly
    pays an installment for a day they did not work — which is the exact thing
    `EARNING_STATUSES` exists to prevent, and an edit must not be a way around
    it.

    Done by deleting the old day's row and re-applying against the new one,
    rather than by arithmetic on `balance_remaining`: that column is a rollup
    and `recompute_balance` is the only thing allowed to move it (see
    app/services/deductions.py).

    ONE THING IT DELIBERATELY DOES NOT DO
    -------------------------------------
    A plan closed by its final installment stays closed when that installment
    is reversed, leaving a small balance on an inactive plan. `is_active=False`
    means both "paid off" and "written off by hand" and the row cannot tell
    them apart, so reopening one the contractor deliberately closed is the
    worse mistake. The deductions screen shows the balance and can reopen it.
    """
    if (old_date, old_status) == (new_date, new_status):
        return

    # Found through the installment itself rather than through `active_plans`,
    # so a day can still be given back after its plan has closed.
    stale = db.scalar(
        select(DeductionInstallment).where(
            DeductionInstallment.worker_id == worker_id,
            DeductionInstallment.date == old_date,
        )
    )
    if stale is not None:
        plan = db.get(AutoDeduction, stale.deduction_id)
        db.delete(stale)
        # So the sum below does not still see the row being removed.
        db.flush()
        if plan is not None:
            recompute_balance(db, plan)

    plan = active_plans(db, [worker_id]).get(worker_id)
    if plan is not None:
        apply_installment(db, plan, new_date, new_status)


@router.patch("/attendance/{attendance_id}", response_model=AttendanceRead)
def edit_attendance(
    attendance_id: int,
    payload: AttendanceUpdate,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> Attendance:
    """Correct a day that was already marked.

    The grid locks a marked row so a second submission cannot rewrite the day
    by accident, which left no way at all to fix a genuine mistake — somebody
    marked absent who was on the other block, a half day that was a full one.
    This is that way, and it is a separate deliberate act rather than a
    relaxation of the lock.

    Open to a supervisor as well as an admin. They mark the day, so they are
    the ones who find it wrong; `require_site_worker` narrows them to the crew
    posted to a site they actually run.

    Changing the status re-settles that day's installment — see
    `_resettle_installment`. Piece-rate wages are untouched: they are paid on
    quantity x rate, not on a status, so correcting who was present does not
    change what was measured.
    """
    attendance = db.get(Attendance, attendance_id)
    if attendance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )
    # Also proves the row belongs to a worker this caller may act on.
    worker = require_site_worker(db, staff, attendance.worker_id)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return attendance

    old_date = attendance.date
    old_status = attendance.status

    new_date = fields.get("date", old_date)
    if new_date > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A day cannot be marked before it happens",
        )

    for field, value in fields.items():
        setattr(attendance, field, value)

    try:
        # Flushed before the installment work rather than left to the commit
        # below. `apply_installment` runs its insert inside a SAVEPOINT and
        # swallows IntegrityError as "this day is already deducted" — a clash
        # on uq_attendance_worker_date surfacing in there would be caught by
        # the wrong handler and roll this edit back without saying so.
        db.flush()
    except IntegrityError:
        # The day it was moved onto is already marked. Merging the two rows is
        # not this endpoint's call to make.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This worker is already marked for that date",
        ) from None

    _resettle_installment(
        db, worker.id, old_date, old_status, attendance.date, attendance.status
    )

    db.commit()
    db.refresh(attendance)
    return attendance


@router.post(
    "/transactions", response_model=TransactionRead, status_code=status.HTTP_201_CREATED
)
def log_transaction(
    payload: TransactionCreate,
    contractor: CurrentContractor,
    db: DbSession,
    background: BackgroundTasks,
) -> TransactionRead:
    """Log a wage earned, an advance given, or a payment settled.

    A settled payment or a large advance also sends the worker a message. That
    runs as a background task after the response, so a messaging outage cannot
    turn a successful ledger write into a failed request.
    """
    worker = _get_own_worker(db, contractor, payload.worker_id)

    if payload.payment_mode is not None and payload.type is TransactionType.WAGE:
        # A wage is an amount earned, not an amount handed over. Letting one
        # carry "paid by UPI" would put a payment nobody made into the record.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A wage is money earned, not money paid - it has no payment mode",
        )

    if payload.daily_deduction is not None:
        # Refuse before writing anything: an advance that lands while its
        # plan is rejected would leave the contractor thinking installments
        # were arranged when they were not.
        ensure_no_active_plan(db, worker.id)

    transaction = Transaction(
        worker_id=payload.worker_id,
        type=payload.type,
        amount=payload.amount,
        date=payload.date,
        payment_mode=payload.payment_mode,
        note=payload.note,
        audio_note_path=attach_audio_note(payload.audio_note_path, contractor),
    )
    db.add(transaction)

    deduction: Optional[AutoDeduction] = None
    if payload.daily_deduction is not None:
        # Same database transaction as the advance itself, so a plan can
        # never point at a row that was never written - and the advance can
        # never land without the arrangement the contractor agreed to it on.
        db.flush()
        deduction = AutoDeduction(
            worker_id=worker.id,
            principal_amount=payload.amount,
            daily_deduction=payload.daily_deduction,
            balance_remaining=payload.amount,
            transaction_id=transaction.id,
            note=payload.note,
        )
        db.add(deduction)

    db.commit()
    db.refresh(transaction)
    if deduction is not None:
        db.refresh(deduction)

    if should_notify(transaction):
        # Read the balance now, while the session is open — the background task
        # runs after the request's session has been closed.
        balance = calculate_balance(db, worker.id).balance
        background.add_task(notify_transaction, transaction, worker, contractor, balance)

    return _read_transaction(transaction, deduction)


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_transaction(
    transaction_id: int, contractor: CurrentContractor, db: DbSession
) -> Response:
    """Undo a mistyped ledger entry.

    WHY THIS DELETES INSTEAD OF DEACTIVATING
    ----------------------------------------
    A worker is deactivated because "off my crew" and "never paid" are
    different claims (see `remove_worker`). A transaction has no such split:
    either the money moved or the number was a typo. A typo has no history
    worth preserving, and leaving it visible-but-flagged would keep it in
    every total, which is the thing being complained about.

    WHAT IS REFUSED, AND WHY
    ------------------------
    A transaction other rows were built on is not a typo, it is a record with
    dependents:

      * an `AutoDeduction` plan - the installments already taken off this
        worker's daily wage were calculated from this advance. Dropping the
        advance leaves the deductions collecting against nothing.
      * an `AdvanceRequest` the worker raised and the contractor approved -
        deleting the payout silently reopens a settled request.
      * a `PieceRateLog` wage, which a log writes in its own DB transaction so
        piece work lands in the same balance as everything else. The wage is
        not editable apart from the work it paid for; delete the log.

    Each is refused with a 409 naming the dependent, so the caller is told
    what to unwind first rather than being left with a corrupted ledger.
    """
    transaction = db.get(Transaction, transaction_id)
    # Ownership is checked through the worker: a transaction belonging to
    # another contractor's crew answers 404, never 403, so this cannot be used
    # to probe for the existence of somebody else's ledger rows.
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found"
        )
    _get_own_worker(db, contractor, transaction.worker_id)

    blockers = (
        (AutoDeduction, "an instalment plan"),
        (AdvanceRequest, "an advance request"),
        (PieceRateLog, "a piece-rate log"),
    )
    for model, describe in blockers:
        if db.scalar(
            select(model.id).where(model.transaction_id == transaction.id).limit(1)
        ) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This entry is backed by {describe} - remove that first",
            )

    db.delete(transaction)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workers/{worker_id}/ledger", response_model=WorkerLedger)
def read_worker_ledger(
    worker_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
) -> WorkerLedger:
    """The same ledger the worker sees, for one worker on this crew."""
    worker = _get_own_worker(db, contractor, worker_id)
    return build_worker_ledger(db, worker, start_date, end_date)


# --- the whole crew, one request --------------------------------------------


@router.post(
    "/attendance/bulk",
    response_model=BulkAttendanceResult,
    status_code=status.HTTP_201_CREATED,
    summary="Mark a whole crew for one day",
)
def log_bulk_attendance(
    payload: BulkAttendanceRequest, staff: CurrentSiteStaff, db: DbSession
) -> BulkAttendanceResult:
    """One submission of the attendance grid.

    Three things happen per row, and they happen together because they are one
    act on site — the contractor walking the line at the end of the shift:

      1. the attendance row is written
      2. a piece-rate row (quantity x rate) also writes its WAGE transaction
      3. an active installment plan takes that day's deduction

    All of it commits or none of it does. Twenty separate requests could
    half-succeed and leave the day partly marked with no way to tell which
    rows landed, which is the failure this endpoint exists to remove.

    REPLAY SAFETY
    -------------
    A worker already marked for the day is left alone unless `overwrite` is
    set — the grid locks marked rows, so a second submission is a retry rather
    than a correction. Piece-rate rows are skipped when the worker already has
    one for that date, and installments are guarded by a unique constraint on
    (plan, day). Submitting the same grid twice therefore changes nothing the
    second time, rather than paying the crew twice.
    """
    when = payload.date or date_type.today()
    if when > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A day cannot be marked before it happens",
        )

    site_id = _check_staff_site(db, staff, payload.site_id)
    worker_ids = [mark.worker_id for mark in payload.marks]

    # An admin may mark anyone on their books, with or without a site. A
    # supervisor may only mark the crew posted to the site they named, which
    # is the narrowing that makes this endpoint safe to hand them.
    if staff.role is UserRole.ADMIN:
        scope = User.contractor_id == staff.id
    else:
        scope = User.site_id == site_id

    crew = {
        worker.id: worker
        for worker in db.scalars(
            select(User).where(
                User.id.in_(worker_ids),
                scope,
                User.role == UserRole.WORKER,
            )
        ).all()
    }
    missing = [worker_id for worker_id in worker_ids if worker_id not in crew]
    if missing:
        # One 404 naming the ids, rather than marking most of the crew and
        # failing silently on the rest.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Not your workers: {', '.join(str(i) for i in missing)}",
        )

    existing = {
        row.worker_id: row
        for row in db.scalars(
            select(Attendance).where(
                Attendance.worker_id.in_(worker_ids), Attendance.date == when
            )
        ).all()
    }
    # Workers who already have piece work logged for this day; their rows are
    # skipped so a retried submission cannot pay for the same tiles twice.
    already_logged = set(
        db.scalars(
            select(PieceRateLog.worker_id).where(
                PieceRateLog.worker_id.in_(worker_ids), PieceRateLog.date == when
            )
        ).all()
    )

    plans = active_plans(db, worker_ids)

    created = updated = unchanged = 0
    rows: list[Attendance] = []
    logs: list[PieceRateLog] = []
    installments: list[DeductionInstallment] = []

    for mark in payload.marks:
        worker = crew[mark.worker_id]
        # A piece-rate worker who laid 120 sq ft was evidently there, so their
        # row defaults to present rather than demanding a second tap.
        wanted = mark.status or AttendanceStatus.PRESENT

        row = existing.get(worker.id)
        if row is None:
            row = Attendance(
                worker_id=worker.id,
                date=when,
                site_id=site_id or worker.site_id,
                status=wanted,
                shift=mark.shift,
                note=mark.note,
            )
            db.add(row)
            created += 1
        elif payload.overwrite and (row.status is not wanted or row.shift is not mark.shift):
            row.status = wanted
            row.shift = mark.shift
            if row.site_id is None:
                row.site_id = site_id or worker.site_id
            if mark.note:
                row.note = mark.note
            updated += 1
        else:
            unchanged += 1
        rows.append(row)

        if mark.is_piece_rate and worker.id not in already_logged:
            log, transaction = build_piece_rate_log(
                worker,
                mark.work_description or "Piece work",
                mark.quantity,
                mark.rate,
                when,
                site_id or worker.site_id,
                mark.note,
            )
            db.add(transaction)
            # The log has to point at the wage it earned, so the id is needed
            # before the commit below.
            db.flush()
            log.transaction_id = transaction.id
            db.add(log)
            logs.append(log)

        plan = plans.get(worker.id)
        if plan is not None:
            # Safe to call for an unchanged row too: the unique constraint on
            # (plan, day) turns a repeat into a no-op rather than a second
            # deduction. `row.status` rather than `wanted`, so a row left
            # alone deducts against what it actually says.
            installment = apply_installment(db, plan, when, row.status)
            if installment is not None:
                installments.append(installment)

    db.commit()

    for row in rows:
        db.refresh(row)
    for log in logs:
        db.refresh(log)

    return BulkAttendanceResult(
        date=when,
        site_id=site_id,
        created=created,
        updated=updated,
        unchanged=unchanged,
        attendance=[AttendanceRead.model_validate(row) for row in rows],
        piece_rate_logs=[PieceRateLogRead.model_validate(log) for log in logs],
        piece_rate_total=sum((log.total_amount for log in logs), Decimal("0.00")),
        installments=[
            DeductionInstallmentRead.model_validate(row) for row in installments
        ],
        deductions_applied=sum(
            (row.amount for row in installments), Decimal("0.00")
        ),
    )


# --- QR check-in ------------------------------------------------------------


@router.post("/attendance/scan", response_model=AttendanceRead)
def scan_attendance(
    payload: AttendanceScan, contractor: CurrentContractor, db: DbSession
) -> Attendance:
    """Mark a worker present from a scan of their QR code.

    Deliberately an upsert, not a create. A camera fires the same code several
    times in a second, and a contractor walking the line will re-scan someone
    they are not sure about; neither should be an error they have to read in
    the sun. Scanning again is a no-op if nothing changed.

    Overwrites a status already recorded for the day, which is the useful
    behaviour: the worker is standing in front of the contractor, so the scan
    is the better evidence.
    """
    worker = _get_own_worker(db, contractor, payload.worker_id)
    day = payload.date or date_type.today()

    attendance = db.scalar(
        select(Attendance).where(
            Attendance.worker_id == worker.id, Attendance.date == day
        )
    )
    if attendance is None:
        attendance = Attendance(
            worker_id=worker.id,
            date=day,
            # Where the worker is posted today. Copied onto the row so moving
            # them tomorrow does not rewrite where they were seen.
            site_id=worker.site_id,
            status=payload.status,
            shift=payload.shift,
        )
        db.add(attendance)
    else:
        attendance.status = payload.status
        attendance.shift = payload.shift
        if attendance.site_id is None:
            attendance.site_id = worker.site_id

    try:
        db.commit()
    except IntegrityError:
        # Two scans of the same badge racing each other. The row that won is
        # the one to return.
        db.rollback()
        attendance = db.scalar(
            select(Attendance).where(
                Attendance.worker_id == worker.id, Attendance.date == day
            )
        )
        if attendance is None:
            raise
        return attendance

    db.refresh(attendance)
    return attendance


# --- advance requests -------------------------------------------------------


def _read_request(request: AdvanceRequest) -> AdvanceRequestRead:
    """Flatten the worker's name onto the row for the approvals queue."""
    read = AdvanceRequestRead.model_validate(request)
    read.worker_name = request.worker.name if request.worker else None
    read.audio_note_url = public_url(request.audio_note_path)
    return read


@router.get("/advance-requests", response_model=list[AdvanceRequestRead])
def list_advance_requests(
    contractor: CurrentContractor,
    db: DbSession,
    status_filter: Optional[AdvanceRequestStatus] = Query(
        default=AdvanceRequestStatus.PENDING,
        alias="status",
        description="Defaults to pending, which is what the dashboard queue shows",
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AdvanceRequestRead]:
    """Advance requests from this contractor's crew, oldest first.

    Oldest first on purpose: this is a queue, and whoever has been waiting
    longest belongs at the top of it.
    """
    stmt = (
        select(AdvanceRequest)
        .join(User, User.id == AdvanceRequest.worker_id)
        .options(joinedload(AdvanceRequest.worker))
        .where(User.contractor_id == contractor.id)
    )
    if status_filter is not None:
        stmt = stmt.where(AdvanceRequest.status == status_filter)

    requests = db.scalars(
        stmt.order_by(AdvanceRequest.created_at.asc(), AdvanceRequest.id.asc()).limit(
            limit
        )
    ).all()
    return [_read_request(request) for request in requests]


@router.patch("/advance-requests/{request_id}", response_model=AdvanceRequestRead)
def decide_advance_request(
    request_id: int,
    payload: AdvanceRequestDecision,
    contractor: CurrentContractor,
    db: DbSession,
    background: BackgroundTasks,
) -> AdvanceRequestRead:
    """Approve or deny one request.

    Approving writes the advance into the worker's ledger in the same database
    transaction as the decision, so there is no window where a request reads
    "approved" but the money has not been recorded against the worker. The
    request keeps the id of the row it created.
    """
    if payload.status is AdvanceRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A decision must be approved or rejected",
        )

    request = db.get(AdvanceRequest, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Advance request not found"
        )
    # Also proves the request belongs to this contractor's crew.
    worker = _get_own_worker(db, contractor, request.worker_id)

    if request.status is not AdvanceRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {request.status.value}",
        )

    transaction: Optional[Transaction] = None
    if payload.status is AdvanceRequestStatus.APPROVED:
        amount = payload.approved_amount or request.amount
        transaction = Transaction(
            worker_id=worker.id,
            type=TransactionType.ADVANCE,
            amount=amount,
            date=date_type.today(),
            # How the contractor actually handed it over. Defaults to cash,
            # which is what happens on site when nobody says otherwise.
            payment_mode=payload.payment_mode or PaymentMode.CASH,
            # The note survives in the ledger even if the request row is ever
            # pruned, so the entry stays explicable on its own.
            note=payload.note or f"Advance request #{request.id}",
        )
        db.add(transaction)
        # Needed before the request can point at it; both are committed below.
        db.flush()
        request.transaction_id = transaction.id

    request.status = payload.status
    # The database clock, like every other timestamp in the schema - an app
    # server whose clock has drifted must not stamp the ledger.
    request.decided_at = func.now()
    db.commit()
    db.refresh(request)

    if transaction is not None:
        db.refresh(transaction)
        if should_notify(transaction):
            # Read while the session is open - the background task runs after
            # the request's session has been closed.
            balance = calculate_balance(db, worker.id).balance
            background.add_task(
                notify_transaction, transaction, worker, contractor, balance
            )

    return _read_request(request)


# --- disputes ---------------------------------------------------------------


@router.patch("/attendance/{attendance_id}/dispute", response_model=AttendanceRead)
def resolve_dispute(
    attendance_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    disputed: bool = Query(
        default=False, description="False clears the flag; true re-raises it"
    ),
) -> Attendance:
    """Clear the flag a worker raised, once it has been settled on site.

    Only the flag moves. Whatever the two of them agreed about the day itself
    is a normal attendance edit, so the correction and the complaint stay
    separate entries in the record.
    """
    attendance = db.get(Attendance, attendance_id)
    if attendance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )
    _get_own_worker(db, contractor, attendance.worker_id)

    attendance.is_disputed = disputed
    db.commit()
    db.refresh(attendance)
    return attendance


# --- the contractor's own cash box -----------------------------------------
#
# Everything below is money that is not tied to a worker: what the site owner
# pays in, and what gets spent on fuel, transport, materials and rentals.


def _read_transaction(
    transaction: Transaction, deduction: Optional[AutoDeduction] = None
) -> TransactionRead:
    """Attach the playable URL, which is derived rather than stored."""
    read = TransactionRead.model_validate(transaction)
    read.audio_note_url = public_url(transaction.audio_note_path)
    if deduction is not None:
        read.deduction_id = deduction.id
    return read


def _read_entry(entry: CashEntry) -> CashEntryRead:
    """Flatten the category onto the row so a list renders in one pass."""
    category = entry.category
    return CashEntryRead(
        id=entry.id,
        category_id=entry.category_id,
        site_id=entry.site_id,
        direction=entry.direction,
        amount=entry.amount,
        date=entry.date,
        note=entry.note,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        category_name=category.name if category else None,
        category_icon=category.icon if category else None,
        category_color=category.color if category else None,
    )


def _get_own_category(db: Session, contractor: User, category_id: int) -> CashCategory:
    category = db.get(CashCategory, category_id)
    if category is None or category.contractor_id != contractor.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found"
        )
    return category


@router.get("/categories", response_model=list[CashCategoryRead])
def list_categories(
    contractor: CurrentContractor,
    db: DbSession,
    direction: Optional[CashDirection] = Query(default=None),
    include_inactive: bool = Query(default=False),
) -> list[CashCategory]:
    """The contractor's expense and income categories.

    Seeds the starter set on the first call, so a new account has something to
    pick from without a separate onboarding step.
    """
    ensure_default_categories(db, contractor.id)

    stmt = select(CashCategory).where(CashCategory.contractor_id == contractor.id)
    if direction is not None:
        stmt = stmt.where(CashCategory.direction == direction)
    if not include_inactive:
        stmt = stmt.where(CashCategory.is_active.is_(True))

    return list(db.scalars(stmt.order_by(CashCategory.direction, CashCategory.name)).all())


@router.post(
    "/categories", response_model=CashCategoryRead, status_code=status.HTTP_201_CREATED
)
def add_category(
    payload: CashCategoryCreate, contractor: CurrentContractor, db: DbSession
) -> CashCategory:
    # Seed first: otherwise a contractor whose very first action is adding
    # "Scaffolding" would never receive the defaults.
    ensure_default_categories(db, contractor.id)

    category = CashCategory(
        contractor_id=contractor.id,
        name=payload.name,
        direction=payload.direction,
        icon=payload.icon,
        color=payload.color,
    )
    db.add(category)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a category with this name",
        ) from None
    db.refresh(category)
    return category


@router.patch("/categories/{category_id}", response_model=CashCategoryRead)
def edit_category(
    category_id: int,
    payload: CashCategoryUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> CashCategory:
    category = _get_own_category(db, contractor, category_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a category with this name",
        ) from None
    db.refresh(category)
    return category


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_category(
    category_id: int, contractor: CurrentContractor, db: DbSession
) -> Response:
    """Delete a category, or retire it if it has been used.

    A category with entries against it is deactivated rather than removed: the
    rows would survive the delete (the FK is SET NULL) but they would lose the
    label that makes a report readable.
    """
    category = _get_own_category(db, contractor, category_id)

    used = db.scalar(
        select(CashEntry.id).where(CashEntry.category_id == category.id).limit(1)
    )
    if used is None:
        db.delete(category)
    else:
        category.is_active = False
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/cash-entries", response_model=list[CashEntryRead])
def list_cash_entries(
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    direction: Optional[CashDirection] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CashEntryRead]:
    """Recent cash entries, newest first."""
    stmt = (
        select(CashEntry)
        .options(joinedload(CashEntry.category))
        .where(CashEntry.contractor_id == contractor.id)
    )
    if start_date is not None:
        stmt = stmt.where(CashEntry.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(CashEntry.date <= end_date)
    if direction is not None:
        stmt = stmt.where(CashEntry.direction == direction)

    entries = db.scalars(
        stmt.order_by(CashEntry.date.desc(), CashEntry.id.desc()).limit(limit)
    ).all()
    return [_read_entry(entry) for entry in entries]


@router.post(
    "/cash-entries", response_model=CashEntryRead, status_code=status.HTTP_201_CREATED
)
def add_cash_entry(
    payload: CashEntryCreate, contractor: CurrentContractor, db: DbSession
) -> CashEntryRead:
    """Log one expense or one payment received.

    Safe to retry. When the app sends a `client_ref` and a row already carries
    it, the stored row is returned unchanged - a queued write whose response
    was lost cannot charge the contractor twice.
    """
    if payload.client_ref:
        existing = db.scalar(
            select(CashEntry).where(
                CashEntry.contractor_id == contractor.id,
                CashEntry.client_ref == payload.client_ref,
            )
        )
        if existing is not None:
            return _read_entry(existing)

    category = (
        _get_own_category(db, contractor, payload.category_id)
        if payload.category_id is not None
        else None
    )

    direction = payload.direction or (category.direction if category else None)
    if direction is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An uncategorised entry needs an explicit direction",
        )
    if category is not None and payload.direction and payload.direction is not category.direction:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The category {category.name} records money {category.direction.value}",
        )

    entry = CashEntry(
        contractor_id=contractor.id,
        category_id=category.id if category else None,
        site_id=payload.site_id,
        direction=direction,
        amount=payload.amount,
        date=payload.date,
        note=payload.note,
        client_ref=payload.client_ref,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # Two devices replaying the same queued entry at once. The row that
        # won the race is the one to return.
        db.rollback()
        existing = db.scalar(
            select(CashEntry).where(
                CashEntry.contractor_id == contractor.id,
                CashEntry.client_ref == payload.client_ref,
            )
        )
        if existing is None:
            raise
        return _read_entry(existing)

    db.refresh(entry)
    return _read_entry(entry)


@router.patch("/cash-entries/{entry_id}", response_model=CashEntryRead)
def edit_cash_entry(
    entry_id: int,
    payload: CashEntryUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> CashEntryRead:
    entry = db.get(CashEntry, entry_id)
    if entry is None or entry.contractor_id != contractor.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

    fields = payload.model_dump(exclude_unset=True)
    if fields.get("category_id") is not None:
        category = _get_own_category(db, contractor, fields["category_id"])
        # Moving an entry between buckets moves it between sides of the chart
        # too, otherwise a spend refiled under "Site Advance" would still
        # count as money out.
        entry.direction = category.direction

    for field, value in fields.items():
        setattr(entry, field, value)

    db.commit()
    db.refresh(entry)
    return _read_entry(entry)


@router.delete("/cash-entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cash_entry(
    entry_id: int, contractor: CurrentContractor, db: DbSession
) -> Response:
    entry = db.get(CashEntry, entry_id)
    if entry is None or entry.contractor_id != contractor.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/summary", response_model=CashSummary)
def read_summary(
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
) -> CashSummary:
    """Cash in, cash out, and the balance between them.

    Unbounded by default, which is what the Home screen wants: the balance of
    the whole job, not of an arbitrary window.
    """
    return build_summary(db, contractor.id, start_date, end_date)


@router.get("/analytics", response_model=AnalyticsSeries)
def read_analytics(
    contractor: CurrentContractor,
    db: DbSession,
    period: Literal["day", "week", "month"] = Query(default="week"),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
) -> AnalyticsSeries:
    """Bars for the analytics chart: money out against money in, over time."""
    return build_series(db, contractor.id, period, start_date, end_date)
