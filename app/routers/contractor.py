from dataclasses import asdict
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.clock import business_today
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
    WageType,
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
from app.schemas.integrity import ReconcileRead, WageAuditRead, WageIssueRead
from app.schemas.ledger import WorkerLedger
from app.schemas.piece_rate import PieceRateLogRead
from app.schemas.transaction import TransactionCreate, TransactionRead
from app.schemas.user import (
    UserRead,
    WorkerCreate,
    WorkerReplace,
    WorkerSaved,
    WorkerUpdate,
)
from app.services.cash import (
    build_series,
    build_summary,
    ensure_default_categories,
)
from app.routers.piece_rate import build_piece_rate_log, ensure_no_active_plan
from app.services.attendance import (
    DayOutcome,
    check_markable,
    correct_day,
    find_day,
    record_day,
    remove_day,
    why_unmarkable,
)
from app.services.integrity import (
    audit_wages,
    count_by_kind,
    price_unpriced_days,
    reconcile_wages,
)
from app.services.wages import reprice_days
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
    site_id: Optional[int] = Query(
        default=None,
        ge=0,
        description="Only the crew posted to this site. 0 lists the workers posted nowhere.",
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
    if site_id == 0:
        stmt = stmt.where(User.site_id.is_(None))
    elif site_id is not None:
        # Narrowing, never widening: a supervisor's statement is already
        # limited to the sites they run, so naming another site finds nobody.
        stmt = stmt.where(User.site_id == site_id)

    return list(db.scalars(stmt.order_by(User.name)).all())


def _set_active(worker: User, active: bool, last_day: Optional[date_type] = None) -> None:
    """Deactivate or reactivate, keeping `deactivated_on` in step with the flag.

    Deactivating records the first day the worker no longer works here: the
    day after `last_day`, which defaults to today - "remove from the crew"
    said at the end of a shift does not unmark the shift. Deactivating someone
    already inactive keeps the date they left. Reactivating clears it.
    """
    if active:
        worker.is_active = True
        worker.deactivated_on = None
        return
    if worker.is_active or worker.deactivated_on is None:
        worker.is_active = False
        worker.deactivated_on = (last_day or business_today()) + timedelta(days=1)


def _saved(worker: User, days_priced: int = 0, days_repriced: int = 0) -> WorkerSaved:
    read = WorkerSaved.model_validate(worker)
    read.days_priced = days_priced
    read.days_repriced = days_repriced
    return read


@router.patch("/workers/{worker_id}", response_model=WorkerSaved)
def edit_worker(
    worker_id: int,
    payload: WorkerUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> WorkerSaved:
    """Change a worker's details, chiefly which site they are posted to.

    Posting the crew to a site is what makes the site screens mean anything —
    the gallery, the crew list and the rain-out button all read `site_id`.

    Setting a rate (or the pay basis) prices any day marked while the worker
    had none - see `price_unpriced_days`. `days_priced` in the response says
    how many, so the app can tell the contractor rather than leave a balance
    to move unexplained.
    """
    worker = _get_own_worker(db, contractor, worker_id)

    fields = payload.model_dump(exclude_unset=True)
    if "site_id" in fields:
        # Sent explicitly, so None here means "take them off the site" rather
        # than "leave it alone".
        fields["site_id"] = _check_site(db, contractor, fields["site_id"])
    active = fields.pop("is_active", None)

    for field, value in fields.items():
        setattr(worker, field, value)
    if active is not None:
        _set_active(worker, active)
    db.flush()

    priced = (
        price_unpriced_days(db, worker)
        if {"daily_wage", "wage_type"} & fields.keys()
        else 0
    )

    db.commit()
    db.refresh(worker)
    return _saved(worker, days_priced=priced)


@router.put("/workers/{worker_id}", response_model=WorkerSaved)
def replace_worker(
    worker_id: int,
    payload: WorkerReplace,
    contractor: CurrentContractor,
    db: DbSession,
) -> WorkerSaved:
    """Save the whole of a worker's edit form.

    The correction route. A name typed wrong on the morning somebody first
    turned up, a day rate agreed at 600 and entered as 60, a post left blank
    because nobody had asked yet - all of it was write-once until this
    existed, and a record you cannot correct is one people stop trusting.

    PUT rather than PATCH because an edit screen sends every field it owns:
    an absent value here means "empty", not "unchanged", which is the only way
    the form can CLEAR a wage rather than just overwrite it. `PATCH` is still
    there for the app's one-field writes. See `WorkerReplace`.

    DAYS ALREADY MARKED
    -------------------
    Keep the rate they were worked at - changing the wage is not a rewrite of
    the books (see app/services/wages.py). Two exceptions, both reported back:

      * days marked while the worker had NO rate are priced at the new one
        (`days_priced`) - they were never priced at anything;
      * `reprice_existing` moves every day-rated day (from `reprice_from`, or
        all of them) onto the new rate (`days_repriced`) - the fix for "agreed
        600, typed 60", which is otherwise stuck at 60 forever.
    """
    worker = _get_own_worker(db, contractor, worker_id)

    if payload.reprice_existing and (
        payload.wage_type is not WageType.DAILY
        or payload.daily_wage is None
        or payload.daily_wage <= 0
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Only a positive day rate can be applied to days already marked - "
                "re-pricing onto no rate would take back wages that were earned"
            ),
        )

    worker.name = payload.name
    worker.daily_wage = payload.daily_wage
    worker.trade = payload.trade
    worker.wage_type = payload.wage_type
    _set_active(worker, payload.is_active)
    # Validated rather than assigned straight through: a site belonging to
    # another contractor answers 404 like everything else here, and 0 is the
    # form's way of saying "not posted anywhere".
    worker.site_id = _check_site(db, contractor, payload.site_id)
    db.flush()

    priced = price_unpriced_days(db, worker)
    repriced = (
        reprice_days(db, worker, from_date=payload.reprice_from)
        if payload.reprice_existing
        else 0
    )

    db.commit()
    db.refresh(worker)
    return _saved(worker, days_priced=priced, days_repriced=repriced)


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
    last_day: Optional[date_type] = Query(
        default=None,
        description=(
            "The last day they worked. Defaults to today; days after it can no "
            "longer be marked."
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
    crew list and the attendance grid while their ledger survives for the
    reports that already went out. Days up to `last_day` can still be marked
    (a back-dated register, an offline phone catching up); days after it
    cannot. See `User.deactivated_on`.

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

    _set_active(worker, False, last_day)
    # The site posting is KEPT. The crew list, the rain-out and the attendance
    # grid all filter on `is_active` already, so nothing counts them; keeping
    # the post means the site's reports can still say who was on the job, a
    # supervisor can still correct a day they worked there, and reactivating
    # puts them back where they were.
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/attendance", response_model=AttendanceRead, status_code=status.HTTP_201_CREATED
)
def log_attendance(
    payload: AttendanceCreate, contractor: CurrentContractor, db: DbSession
) -> Attendance:
    """Record one day's attendance for a worker.

    This is also the route the app's offline queue replays, so its contract is
    load-bearing: a day already marked answers 409, which the queue reads as
    "recorded" and drops; anything that cannot be marked answers 422, which the
    queue surfaces to the contractor instead of losing.

    The day, its wage and its installment are written together - see
    app/services/attendance.py. The row is stamped with the site it was worked
    at: the one named, or wherever the worker is posted.
    """
    worker = _get_own_worker(db, contractor, payload.worker_id)
    site_id = (
        _check_site(db, contractor, payload.site_id)
        if payload.site_id
        else worker.site_id
    )

    already = HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Attendance for this worker on this date has already been logged",
    )
    if find_day(db, worker.id, payload.date) is not None:
        raise already

    try:
        attendance, _, _ = record_day(
            db,
            worker,
            payload.date,
            payload.status,
            shift=payload.shift,
            note=payload.note,
            site_id=site_id,
            existing=None,
        )
    except IntegrityError:
        # Marked by another request between the check above and the insert
        # (uq_attendance_worker_date). Same answer as if it had been first.
        db.rollback()
        raise already from None

    db.commit()
    db.refresh(attendance)
    return attendance


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

    The wage and the day's installment follow the correction - see
    `correct_day`. Piece-rate wages are untouched: they are paid on quantity x
    rate, not on a status, so correcting who was present does not change what
    was measured.
    """
    # Locked: the wage and the installment are about to be rewritten from
    # what this row says, and a second correction racing this one must wait.
    attendance = db.get(Attendance, attendance_id, with_for_update=True)
    if attendance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )
    # Also proves the row belongs to a worker this caller may act on.
    worker = require_site_worker(db, staff, attendance.worker_id)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return attendance

    try:
        correct_day(db, attendance, worker, fields)
    except IntegrityError:
        # The day it was moved onto is already marked. Merging the two rows is
        # not this endpoint's call to make.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This worker is already marked for that date",
        ) from None

    db.commit()
    db.refresh(attendance)
    return attendance


@router.delete("/attendance/{attendance_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_attendance(
    attendance_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> Response:
    """Unmark a day that should never have been marked at all.

    WHY A CORRECTION IS NOT ENOUGH
    ------------------------------
    `edit_attendance` fixes a day that happened and was recorded wrong. It
    cannot express the other mistake: a day that did not happen to this worker
    at all - the wrong name tapped in the grid, a whole crew marked against a
    site that stood down. Correcting that to `absent` is not the truth either,
    because absent is a statement about somebody who was expected; it keeps the
    worker on the roster for a day they were never on.

    So this removes the row, and with it the wage the day earned and the
    installment it took - see `remove_day`, which also refuses a day that paid
    piece-rate wages (409, naming the log to remove first).
    """
    attendance = db.get(Attendance, attendance_id, with_for_update=True)
    if attendance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )
    # Ownership through the worker, and the same 404 above for a row belonging
    # to another contractor's crew - a 403 would confirm it exists.
    worker = require_site_worker(db, staff, attendance.worker_id)

    remove_day(db, attendance, worker)

    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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

    SAFE TO RETRY WITH A client_ref
    -------------------------------
    The app's offline queue sends one. When a row already carries it, that
    row is returned and nothing is written - so a queued advance whose first
    attempt committed but whose response was lost cannot pay the worker
    twice. Same contract as `POST /contractor/cash-entries`.
    """
    worker = _get_own_worker(db, contractor, payload.worker_id)

    if payload.client_ref:
        replay = _transaction_by_ref(db, contractor, payload.client_ref)
        if replay is not None:
            return _read_transaction(replay)

    if payload.date > business_today():
        # Money that has not moved yet is not a ledger entry. An advance typed
        # with tomorrow's date sits in the lifetime balance today while every
        # payroll window ending today leaves it out, so the two disagree.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A ledger entry cannot be dated in the future",
        )

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
        client_ref=payload.client_ref,
        # The site named, or where the worker is posted now. Validated: a site
        # belonging to someone else answers 404 like everywhere else.
        site_id=(
            _check_site(db, contractor, payload.site_id)
            if payload.site_id
            else worker.site_id
        ),
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

    try:
        db.commit()
    except IntegrityError:
        # Two replays of one queued entry racing each other through the
        # UNIQUE client_ref. The row that won is the one to return.
        db.rollback()
        replay = (
            _transaction_by_ref(db, contractor, payload.client_ref)
            if payload.client_ref
            else None
        )
        if replay is not None:
            return _read_transaction(replay)
        if payload.client_ref:
            # The key is held by a row this contractor cannot see. Refused
            # without saying whose - and never by returning that row.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This entry's reference is already in use. Save it again.",
            ) from None
        raise
    db.refresh(transaction)
    if deduction is not None:
        db.refresh(deduction)

    if should_notify(transaction):
        # Read the balance now, while the session is open — the background task
        # runs after the request's session has been closed.
        balance = calculate_balance(db, worker.id).balance
        background.add_task(notify_transaction, transaction, worker, contractor, balance)

    return _read_transaction(transaction, deduction)


def _transaction_by_ref(
    db: Session, contractor: User, client_ref: str
) -> Optional[Transaction]:
    """The row a client_ref already wrote, if it was this contractor's.

    Scoped through the worker so one contractor's key can never return
    another's row - the key space is shared, the books are not.
    """
    return db.scalar(
        select(Transaction)
        .join(User, User.id == Transaction.worker_id)
        .where(Transaction.client_ref == client_ref, User.contractor_id == contractor.id)
    )


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
        # A day's wage, written by marking attendance. Deleting it here would
        # leave the day on the books earning nothing, and the next edit to
        # that day would silently write the wage back - so the correction has
        # to be made where it belongs: unmark the day, or correct it to
        # absent. See app/services/wages.py.
        (Attendance, "a day of attendance"),
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


# --- does the ledger match the register? ------------------------------------


@router.get("/integrity/wages", response_model=WageAuditRead)
def read_wage_audit(contractor: CurrentContractor, db: DbSession) -> WageAuditRead:
    """Check every day this crew has marked against the wage it earned.

    Read-only. An empty `issues` list is the proof that every balance on every
    screen is built from wages that match attendance exactly. See
    app/services/integrity.py.
    """
    issues = audit_wages(db, contractor.id)
    return WageAuditRead(
        consistent=all(issue.kind == "unpriced" for issue in issues),
        counts=count_by_kind(issues),
        issues=[WageIssueRead(**asdict(issue)) for issue in issues],
    )


@router.post("/integrity/wages/reconcile", response_model=ReconcileRead)
def run_wage_reconcile(
    contractor: CurrentContractor,
    db: DbSession,
    apply: bool = Query(
        default=False,
        description=(
            "False (the default) reports what would change and writes nothing. "
            "True makes the change."
        ),
    ),
) -> ReconcileRead:
    """Bring every day's wage in line with the day - after showing what moves.

    Prices days that earn but have no rate at the worker's current rate,
    writes the wages they are missing, and links a wage typed by hand for the
    same worker, day and amount instead of doubling it. Run it without `apply`
    first: the report is the decision, and it says per worker how much their
    balance would move.
    """
    report = reconcile_wages(db, contractor.id, apply=apply)
    if apply:
        db.commit()
    return ReconcileRead(**report.as_dict())


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

      1. the attendance row is written, with the wage it earns
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

    Two phones submitting the same crew at the same moment both pass the
    "already marked" check, and the second insert then trips
    uq_attendance_worker_date. That request is rolled back and run once more,
    when it finds the first one's rows and leaves them alone - the same answer
    it would have got a second later.
    """
    when = payload.date or business_today()
    if when > business_today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A day cannot be marked before it happens",
        )

    site_id = _check_staff_site(db, staff, payload.site_id)

    try:
        result = _mark_crew(db, staff, payload, when, site_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        result = _mark_crew(db, staff, payload, when, site_id)
        db.commit()

    rows, logs, installments, counts = result
    for row in rows:
        db.refresh(row)
    for log in logs:
        db.refresh(log)

    return BulkAttendanceResult(
        date=when,
        site_id=site_id,
        created=counts[DayOutcome.CREATED],
        updated=counts[DayOutcome.UPDATED],
        unchanged=counts[DayOutcome.UNCHANGED],
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


def _mark_crew(
    db: Session,
    staff: User,
    payload: BulkAttendanceRequest,
    when: date_type,
    site_id: Optional[int],
) -> tuple[
    list[Attendance],
    list[PieceRateLog],
    list[DeductionInstallment],
    dict[DayOutcome, int],
]:
    """The body of `log_bulk_attendance`, separated so a race can re-run it."""
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
            select(Attendance)
            .where(Attendance.worker_id.in_(worker_ids), Attendance.date == when)
            .with_for_update()
        ).all()
    }

    # Refused up front, all at once, before anything is written: a worker who
    # has been taken off the crew is not marked for a day after they left. A
    # row that already exists is not a new mark and is not refused.
    refused = [
        reason
        for worker_id in worker_ids
        if worker_id not in existing
        and (reason := why_unmarkable(crew[worker_id], when)) is not None
    ]
    if refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=" ".join(refused)
        )

    # Workers who already have piece work logged for this day; their rows are
    # skipped so a retried submission cannot pay for the same tiles twice.
    already_logged = set(
        db.scalars(
            select(PieceRateLog.worker_id).where(
                PieceRateLog.worker_id.in_(worker_ids), PieceRateLog.date == when
            )
        ).all()
    )

    counts = {outcome: 0 for outcome in DayOutcome}
    rows: list[Attendance] = []
    logs: list[PieceRateLog] = []
    installments: list[DeductionInstallment] = []

    for mark in payload.marks:
        worker = crew[mark.worker_id]

        if mark.is_piece_rate and worker.id not in already_logged:
            check_markable(worker, when)
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
            # before the commit.
            db.flush()
            log.transaction_id = transaction.id
            db.add(log)
            logs.append(log)

        # A piece-rate worker who laid 120 sq ft was evidently there, so their
        # row defaults to present rather than demanding a second tap. Their
        # day earns no day rate - the log above paid them (see wages.py).
        row, outcome, installment = record_day(
            db,
            worker,
            when,
            mark.status or AttendanceStatus.PRESENT,
            shift=mark.shift,
            note=mark.note,
            site_id=site_id or worker.site_id,
            overwrite=payload.overwrite,
            existing=existing.get(worker.id),
        )
        counts[outcome] += 1
        rows.append(row)
        if installment is not None:
            installments.append(installment)

    return rows, logs, installments, counts


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
    is the better evidence. The wage and the installment follow the scan
    exactly as they follow a correction.

    A deactivated worker's badge is refused even when the day already has a
    row - the scan is a claim that they are here today.
    """
    worker = _get_own_worker(db, contractor, payload.worker_id)
    day = payload.date or business_today()
    check_markable(worker, day)

    try:
        attendance, _, _ = record_day(
            db,
            worker,
            day,
            payload.status,
            shift=payload.shift,
            # Where the worker is posted today. Copied onto the row so moving
            # them tomorrow does not rewrite where they were seen.
            site_id=worker.site_id,
            overwrite=True,
        )
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
            date=business_today(),
            # How the contractor actually handed it over. Defaults to cash,
            # which is what happens on site when nobody says otherwise.
            payment_mode=payload.payment_mode or PaymentMode.CASH,
            # Handed over where the worker is posted.
            site_id=worker.site_id,
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
    site_id: Optional[int] = Query(default=None, gt=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CashEntryRead]:
    """Recent cash entries, newest first - for the business, or one site."""
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
    if site_id is not None:
        stmt = stmt.where(CashEntry.site_id == _check_site(db, contractor, site_id))

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
        # Checked, not trusted: the column is a bare FK, so without this an
        # entry could be filed against another contractor's site id and turn
        # up in their site's spending.
        site_id=_check_site(db, contractor, payload.site_id),
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
    if "site_id" in fields:
        fields["site_id"] = _check_site(db, contractor, fields["site_id"])
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
    site_id: Optional[int] = Query(default=None, gt=0),
) -> CashSummary:
    """Cash in, cash out, and the balance between them.

    Unbounded by default, which is what the Home screen wants: the balance of
    the whole job, not of an arbitrary window. `site_id` narrows every figure
    to one site - see `build_summary`.
    """
    if site_id is not None:
        _check_site(db, contractor, site_id)
    return build_summary(db, contractor.id, start_date, end_date, site_id)


@router.get("/analytics", response_model=AnalyticsSeries)
def read_analytics(
    contractor: CurrentContractor,
    db: DbSession,
    period: Literal["day", "week", "month"] = Query(default="week"),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    site_id: Optional[int] = Query(default=None, gt=0),
) -> AnalyticsSeries:
    """Bars for the analytics chart: money out against money in, over time."""
    if site_id is not None:
        _check_site(db, contractor, site_id)
    return build_series(db, contractor.id, period, start_date, end_date, site_id)
