"""Downloadable reports: PDF statements for a worker, PDF and CSV for the job."""

from collections import defaultdict
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.core.clock import business_today
from app.dependencies import CurrentContractor, CurrentUser, DbSession
from app.models.attendance import Attendance
from app.models.cash import CashEntry
from app.models.petty_cash import PettyCash
from app.models.enums import UserRole
from app.models.site import Site
from app.models.transaction import Transaction
from app.models.user import User
from app.services.cash import build_series, build_summary
from app.services.csv_report import attendance_csv, cash_entries_csv
from app.services.csv_report import filename as csv_filename
from app.services.csv_report import monthly_csv, transactions_csv
from app.services.ledger import calculate_balance
from app.services.pdf_report import build_business_report, build_worker_statement
from app.services.wages import weight

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _authorise(db: DbSession, user: User, worker_id: int) -> User:
    """Return the worker, if this user is allowed to see their statement.

    Two callers are legitimate: the worker themselves, and the contractor whose
    crew they are on. Anyone else gets a 404 rather than a 403, matching the
    rest of the API — a wrong id should not confirm that the worker exists.
    """
    if user.role is UserRole.WORKER:
        if user.id != worker_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
        return user

    worker = db.get(User, worker_id)
    if (
        worker is None
        or worker.role is not UserRole.WORKER
        or worker.contractor_id != user.id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    return worker


@router.get(
    "/worker/{worker_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The statement"}},
    summary="Download a worker's cashbook statement as a PDF",
)
def worker_statement_pdf(
    worker_id: int,
    user: CurrentUser,
    db: DbSession,
    start_date: Optional[date_type] = Query(
        default=None, description="First day of the period. Omit for everything up to end_date."
    ),
    end_date: Optional[date_type] = Query(
        default=None, description="Last day of the period. Defaults to today."
    ),
) -> Response:
    """Build the statement and return it as a downloadable file."""
    worker = _authorise(db, user, worker_id)

    today = business_today()
    period_end = end_date or today
    if start_date is not None and start_date > period_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must not be after end_date",
        )

    # Rows inside the period, oldest first — a statement reads forward in time.
    tx_stmt = select(Transaction).where(
        Transaction.worker_id == worker.id, Transaction.date <= period_end
    )
    att_stmt = select(Attendance).where(
        Attendance.worker_id == worker.id, Attendance.date <= period_end
    )
    if start_date is not None:
        tx_stmt = tx_stmt.where(Transaction.date >= start_date)
        att_stmt = att_stmt.where(Attendance.date >= start_date)

    transactions = list(
        db.scalars(tx_stmt.order_by(Transaction.date, Transaction.id)).all()
    )
    attendance = list(db.scalars(att_stmt.order_by(Attendance.date, Attendance.id)).all())

    # Three windows: what was owed going in, what moved during the period, and
    # what is owed coming out. With no start_date the period covers everything,
    # so nothing was carried in.
    opening = (
        calculate_balance(db, worker.id, end=start_date - timedelta(days=1)).balance
        if start_date is not None
        else Decimal("0.00")
    )
    period = calculate_balance(db, worker.id, start=start_date, end=period_end)
    closing = calculate_balance(db, worker.id, end=period_end).balance

    contractor = db.get(User, worker.contractor_id) if worker.contractor_id else None

    pdf = build_worker_statement(
        worker=worker,
        contractor=contractor,
        transactions=transactions,
        attendance=attendance,
        opening_balance=opening,
        period=period,
        closing_balance=closing,
        start_date=start_date,
        end_date=period_end,
        generated_on=today,
    )

    filename = _filename(worker.name, start_date, period_end)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # `filename*` carries the UTF-8 name for clients that understand it;
            # `filename` is the ASCII fallback for those that do not.
            "Content-Disposition": (
                f'attachment; filename="statement.pdf"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(len(pdf)),
            # The ledger changes; a cached statement would quietly go stale.
            "Cache-Control": "no-store",
        },
    )


def _filename(worker_name: str, start: Optional[date_type], end: date_type) -> str:
    """e.g. 'Suresh-Kumar-statement-2026-08-01-to-2026-09-03.pdf'"""
    safe = "".join(char if char.isalnum() else "-" for char in worker_name).strip("-")
    safe = "-".join(part for part in safe.split("-") if part) or "worker"
    span = f"{start.isoformat()}-to-{end.isoformat()}" if start else f"upto-{end.isoformat()}"
    return f"{safe}-statement-{span}.pdf"


# --- CSV exports -----------------------------------------------------------
#
# A contractor exports the whole crew (or one worker with `worker_id`); a
# worker exports only themselves. The scoping is decided once, in
# `_export_scope`, so no endpoint below can widen it by accident.


def _attachment(name: str, fallback: str) -> str:
    """Content-Disposition with both an ASCII and a UTF-8 filename."""
    return f'attachment; filename="{fallback}"; ' + "filename*=UTF-8''" + quote(name)


def _csv_response(payload: bytes, name: str) -> Response:
    return Response(
        content=payload,
        # `text/csv` alone makes some browsers render the file instead of
        # saving it; the charset stops Excel guessing at the encoding.
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": _attachment(name, "export.csv"),
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


def _export_scope(db: DbSession, user: User, worker_id: Optional[int]) -> tuple[list[User], str]:
    """Which workers this export covers, and what to call the file.

    A worker gets exactly themselves, whatever `worker_id` says. A contractor
    gets their crew, narrowed to one worker when asked - and a `worker_id` that
    is not on the crew is a 404, matching the rest of the API.
    """
    if user.role is UserRole.WORKER:
        return [user], user.name

    crew = list(
        db.scalars(
            select(User)
            .where(User.contractor_id == user.id, User.role == UserRole.WORKER)
            .order_by(User.name)
        ).all()
    )
    if worker_id is None:
        return crew, user.name

    chosen = [worker for worker in crew if worker.id == worker_id]
    if not chosen:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    return chosen, chosen[0].name


def _report_site(db: DbSession, user: User, site_id: Optional[int]) -> Optional[Site]:
    """The site a report is narrowed to, if the caller may see it.

    A contractor must own it - someone else's site is a 404, like everywhere
    else. A worker's export is already only their own rows, so narrowing it by
    a site cannot reveal anything and is simply applied.
    """
    if site_id is None:
        return None
    if user.role is UserRole.WORKER:
        return db.get(Site, site_id)
    site = db.get(Site, site_id)
    if site is None or site.contractor_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    return site


def _window(
    start_date: Optional[date_type], end_date: Optional[date_type]
) -> tuple[Optional[date_type], date_type]:
    period_end = end_date or business_today()
    if start_date is not None and start_date > period_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must not be after end_date",
        )
    return start_date, period_end


@router.get(
    "/transactions/csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "The export"}},
    summary="Download transaction history as a CSV",
)
def transactions_export(
    user: CurrentUser,
    db: DbSession,
    worker_id: Optional[int] = Query(default=None, description="Contractors only: one worker."),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    site_id: Optional[int] = Query(
        default=None, gt=0, description="Only what happened at this site."
    ),
) -> Response:
    workers, owner = _export_scope(db, user, worker_id)
    start, end = _window(start_date, end_date)
    site = _report_site(db, user, site_id)

    ids = [worker.id for worker in workers]
    rows: list[Transaction] = []
    if ids:
        stmt = select(Transaction).where(
            Transaction.worker_id.in_(ids), Transaction.date <= end
        )
        if start is not None:
            stmt = stmt.where(Transaction.date >= start)
        if site_id is not None:
            # By the site on the row: a worker moved or deactivated since still
            # appears on the site where the money moved.
            stmt = stmt.where(Transaction.site_id == site_id)
        rows = list(db.scalars(stmt.order_by(Transaction.date, Transaction.id)).all())

    payload = transactions_csv(rows, {worker.id: worker for worker in workers})
    return _csv_response(
        payload, csv_filename("transactions", site.name if site else owner, start, end)
    )


@router.get(
    "/attendance/csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "The export"}},
    summary="Download worker attendance as a CSV",
)
def attendance_export(
    user: CurrentUser,
    db: DbSession,
    worker_id: Optional[int] = Query(default=None, description="Contractors only: one worker."),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    site_id: Optional[int] = Query(
        default=None, gt=0, description="Only what happened at this site."
    ),
) -> Response:
    workers, owner = _export_scope(db, user, worker_id)
    start, end = _window(start_date, end_date)
    site = _report_site(db, user, site_id)

    ids = [worker.id for worker in workers]
    rows: list[Attendance] = []
    if ids:
        stmt = select(Attendance).where(Attendance.worker_id.in_(ids), Attendance.date <= end)
        if start is not None:
            stmt = stmt.where(Attendance.date >= start)
        if site_id is not None:
            stmt = stmt.where(Attendance.site_id == site_id)
        rows = list(db.scalars(stmt.order_by(Attendance.date, Attendance.id)).all())

    payload = attendance_csv(rows, {worker.id: worker for worker in workers})
    return _csv_response(
        payload, csv_filename("attendance", site.name if site else owner, start, end)
    )


@router.get(
    "/cash-entries/csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "The export"}},
    summary="Download general expenses and receipts as a CSV",
)
def cash_entries_export(
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    site_id: Optional[int] = Query(
        default=None, gt=0, description="Only what happened at this site."
    ),
) -> Response:
    """Contractor only - a worker has no visibility of the site cash box."""
    start, end = _window(start_date, end_date)
    site = _report_site(db, contractor, site_id)

    stmt = (
        select(CashEntry)
        .options(joinedload(CashEntry.category))
        .where(CashEntry.contractor_id == contractor.id, CashEntry.date <= end)
    )
    if start is not None:
        stmt = stmt.where(CashEntry.date >= start)
    if site_id is not None:
        stmt = stmt.where(CashEntry.site_id == site_id)
    entries = list(db.scalars(stmt.order_by(CashEntry.date, CashEntry.id)).all())

    payload = cash_entries_csv(entries)
    return _csv_response(
        payload, csv_filename("expenses", site.name if site else contractor.name, start, end)
    )


@router.get(
    "/business/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The report"}},
    summary="Download the full business report for a contractor as a PDF",
)
def business_report_pdf(
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    site_id: Optional[int] = Query(
        default=None, gt=0, description="Only what happened at this site."
    ),
) -> Response:
    """Cash in, cash out, the crew, and every expense - on one document.

    With `site_id`, the same document for one site. Its crew is everyone who
    worked or was paid there - deactivated workers and workers since moved
    elsewhere included, because their days and their money happened here -
    plus whoever is posted there now.
    """
    start, end = _window(start_date, end_date)
    today = business_today()
    site = _report_site(db, contractor, site_id)

    crew_stmt = select(User).where(
        User.contractor_id == contractor.id, User.role == UserRole.WORKER
    )
    if site_id is not None:
        crew_stmt = crew_stmt.where(
            or_(
                User.site_id == site_id,
                User.id.in_(select(Attendance.worker_id).where(Attendance.site_id == site_id)),
                User.id.in_(select(Transaction.worker_id).where(Transaction.site_id == site_id)),
            )
        )
    crew = list(db.scalars(crew_stmt.order_by(User.name)).all())

    # Days worked counts a half day as half and a rain-out or an absence as
    # nothing - the same weights the wage uses (app/services/wages.py), so the
    # report cannot count a day the ledger did not pay. The first version kept
    # its own table, left rain-out out of it, and raised KeyError on the first
    # rained-out day in the period: the whole PDF answered 500.
    #
    # The balance is deliberately lifetime rather than period: a partial view
    # of what is owed would be read as the whole thing.
    #
    # One grouped query for the whole crew, not one per worker.
    days_by_worker: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    if crew:
        att_stmt = select(
            Attendance.worker_id, Attendance.status, func.count()
        ).where(Attendance.worker_id.in_([w.id for w in crew]), Attendance.date <= end)
        if start is not None:
            att_stmt = att_stmt.where(Attendance.date >= start)
        if site_id is not None:
            att_stmt = att_stmt.where(Attendance.site_id == site_id)
        for worker_id, day_status, count in db.execute(
            att_stmt.group_by(Attendance.worker_id, Attendance.status)
        ).all():
            days_by_worker[worker_id] += weight(day_status) * count

    crew_rows: list[tuple[User, Decimal, Decimal]] = [
        (worker, days_by_worker[worker.id], calculate_balance(db, worker.id).balance)
        for worker in crew
    ]

    entry_stmt = (
        select(CashEntry)
        .options(joinedload(CashEntry.category))
        .where(CashEntry.contractor_id == contractor.id, CashEntry.date <= end)
    )
    if start is not None:
        entry_stmt = entry_stmt.where(CashEntry.date >= start)
    if site_id is not None:
        entry_stmt = entry_stmt.where(CashEntry.site_id == site_id)
    entries = list(db.scalars(entry_stmt.order_by(CashEntry.date, CashEntry.id)).all())

    summary = build_summary(db, contractor.id, start, end, site_id)
    # Only the category breakdown is used here; the bars belong on screen.
    breakdown = build_series(db, contractor.id, "month", start, end, site_id).by_category

    pdf = build_business_report(
        contractor=contractor,
        summary=summary,
        crew=crew_rows,
        cash_entries=entries,
        by_category=breakdown,
        start_date=start,
        end_date=end,
        generated_on=today,
        site_name=site.name if site else None,
    )

    name = csv_filename("report", site.name if site else contractor.name, start, end)[:-4] + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _attachment(name, "report.pdf"),
            "Content-Length": str(len(pdf)),
            "Cache-Control": "no-store",
        },
    )


# --- monthly accounting export (Phase 11) -----------------------------------


@router.get(
    "/monthly-report",
    summary="Everything that happened in one month, as one CSV",
    response_class=Response,
)
def monthly_report(
    contractor: CurrentContractor,
    db: DbSession,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-\d{2}$",
        description="The month to export, as YYYY-MM",
        examples=["2026-09"],
    ),
    site_id: Optional[int] = Query(
        default=None, gt=0, description="Only what happened at this site."
    ),
) -> Response:
    """Transactions, attendance and petty cash for one month, in one file.

    ADMIN ONLY — `CurrentContractor` resolves to the admin role, so a
    supervisor cannot pull the project's finances.

    ONE FILE, THREE SECTIONS
    ------------------------
    A zip of three CSVs would be tidier and worse: this goes to an accountant
    over WhatsApp, and a zip on a phone is a support call. So it is one sheet
    with a labelled block per section, which opens in Excel and reads top to
    bottom.

    Written with the `csv` module rather than pandas — the whole job is
    quoting strings, and a 40 MB dependency to do that would be the largest
    thing in the image.
    """
    year, month_number = (int(part) for part in month.split("-"))
    if not 1 <= month_number <= 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Month must be between 01 and 12",
        )

    start = date_type(year, month_number, 1)
    # First of the next month, minus a day. Avoids a calendar table and gets
    # February right in a leap year.
    end = (
        date_type(year + (month_number == 12), (month_number % 12) + 1, 1)
        - timedelta(days=1)
    )

    site = _report_site(db, contractor, site_id)

    crew = list(
        db.scalars(
            select(User).where(
                User.contractor_id == contractor.id, User.role == UserRole.WORKER
            )
        ).all()
    )
    workers = {worker.id: worker for worker in crew}
    worker_ids = list(workers)
    # A site's month is the rows recorded there, whoever the worker is now.
    tx_site = [Transaction.site_id == site_id] if site_id is not None else []
    att_site = [Attendance.site_id == site_id] if site_id is not None else []
    petty_site = [PettyCash.site_id == site_id] if site_id is not None else []

    transactions = (
        list(
            db.scalars(
                select(Transaction)
                .where(
                    Transaction.worker_id.in_(worker_ids),
                    Transaction.date >= start,
                    Transaction.date <= end,
                    *tx_site,
                )
                .order_by(Transaction.date, Transaction.id)
            ).all()
        )
        if worker_ids
        else []
    )

    attendance = (
        list(
            db.scalars(
                select(Attendance)
                .where(
                    Attendance.worker_id.in_(worker_ids),
                    Attendance.date >= start,
                    Attendance.date <= end,
                    *att_site,
                )
                .order_by(Attendance.date, Attendance.id)
            ).all()
        )
        if worker_ids
        else []
    )

    # Petty cash is site-scoped, so ownership is proved through the site rather
    # than through the crew.
    petty = list(
        db.scalars(
            select(PettyCash)
            .join(Site, Site.id == PettyCash.site_id)
            .where(
                Site.contractor_id == contractor.id,
                PettyCash.date >= start,
                PettyCash.date <= end,
                *petty_site,
            )
            .order_by(PettyCash.date, PettyCash.id)
        ).all()
    )

    payload = monthly_csv(month, transactions, attendance, petty, workers)
    label = f"-{_slug(site.name)}" if site else ""
    return _csv_response(payload, f"cashbook-{month}{label}.csv")


def _slug(text: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in text).strip("-")
    return "-".join(part for part in safe.split("-") if part) or "site"
