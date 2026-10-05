"""Proving the ledger matches the register, and putting it right when it does not.

The invariant (app/services/wages.py) is that every attendance row's linked
WAGE is exactly `round(wage_rate x weight(status))`, on the row's date - and
that no row which earns nothing has one. Every write path keeps it true. This
module is how anyone can CHECK that it is true, for a whole database, rather
than taking the write paths' word for it:

  * `audit_wages`     read-only. Lists every attendance row that breaks the
                      invariant, and why.
  * `reconcile_wages` makes the invariant true. Dry-run by default: it does
                      the work inside a transaction, reports what it did, and
                      rolls back unless told to keep it.

WHY A RECONCILE EXISTS AT ALL
-----------------------------
Days marked before attendance earned wages (anything before revision
c4e8b2f71d95 reached the database) have no wage. That migration deliberately
did not invent them, for fear of double-counting wages a contractor had typed
by hand to work around the bug. That fear was right, and the answer to it is
here rather than in a migration: a reconcile that

  1. prices those days at the worker's CURRENT rate - the only rate on
     record, and the one the old screens were already showing - and
  2. ADOPTS a hand-typed WAGE for the same worker, same date and same amount
     instead of creating a second one, so a workaround is linked rather than
     doubled,

and that shows its report BEFORE anything is written. Running it is a
decision for whoever owns the books; it never runs on its own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import TransactionType
from app.models.piece_rate import PieceRateLog
from app.models.transaction import Transaction
from app.models.user import User
from app.services.wages import (
    expected_wage,
    rate_for_new_day,
    sync_attendance_wage,
    weight,
)


@dataclass
class WageIssue:
    """One attendance row that breaks the invariant.

    kind:
      unpriced      earns by status but has no rate - a daily worker booked
                    without one, or a day marked before wages were linked
      missing_wage  has a rate and earns, but no WAGE is linked
      wrong_amount  the linked WAGE is not rate x weight
      wrong_date    the linked WAGE is dated differently from the day
      wrong_site    the linked WAGE is filed under a different site from the
                    day, so the site's report would show it in the wrong place
      stray_wage    a WAGE is linked to a day that earns nothing
      bad_link      the linked row is not a WAGE, belongs to another worker,
                    or is also claimed by a piece-rate log
    """

    kind: str
    attendance_id: int
    worker_id: int
    worker_name: str
    date: date_type
    status: str
    wage_rate: Optional[Decimal]
    expected: Optional[Decimal]
    actual: Optional[Decimal]
    transaction_id: Optional[int]


def _crew_filter(stmt, contractor_id: Optional[int], worker_id: Optional[int] = None):
    if contractor_id is not None:
        stmt = stmt.where(User.contractor_id == contractor_id)
    if worker_id is not None:
        stmt = stmt.where(User.id == worker_id)
    return stmt


def audit_wages(
    db: Session, contractor_id: Optional[int] = None, worker_id: Optional[int] = None
) -> list[WageIssue]:
    """Every attendance row whose wage is not what the day says. Read-only.

    One query over attendance, its worker and its linked transaction, so the
    cost does not grow a query per row. An empty list is the proof.
    """
    stmt = (
        select(Attendance, User, Transaction)
        .join(User, User.id == Attendance.worker_id)
        .outerjoin(Transaction, Transaction.id == Attendance.transaction_id)
        .order_by(Attendance.worker_id, Attendance.date)
    )
    stmt = _crew_filter(stmt, contractor_id, worker_id)

    piece_linked = set(
        db.scalars(
            select(PieceRateLog.transaction_id).where(
                PieceRateLog.transaction_id.is_not(None)
            )
        ).all()
    )

    issues: list[WageIssue] = []
    for row, worker, wage in db.execute(stmt).all():
        expected = expected_wage(row.status, row.wage_rate)

        def issue(kind: str) -> WageIssue:
            return WageIssue(
                kind=kind,
                attendance_id=row.id,
                worker_id=worker.id,
                worker_name=worker.name,
                date=row.date,
                status=row.status.value,
                wage_rate=row.wage_rate,
                expected=expected,
                actual=wage.amount if wage is not None else None,
                transaction_id=row.transaction_id,
            )

        if wage is not None and (
            wage.type is not TransactionType.WAGE
            or wage.worker_id != row.worker_id
            or wage.id in piece_linked
        ):
            issues.append(issue("bad_link"))
            continue

        if expected is None:
            if row.wage_rate is None and weight(row.status) > 0:
                issues.append(issue("unpriced"))
            elif wage is not None:
                issues.append(issue("stray_wage"))
            continue

        if wage is None:
            issues.append(issue("missing_wage"))
        elif wage.amount != expected:
            issues.append(issue("wrong_amount"))
        elif wage.date != row.date:
            issues.append(issue("wrong_date"))
        elif wage.site_id != row.site_id:
            issues.append(issue("wrong_site"))

    return issues


@dataclass
class ReconcileReport:
    """What a reconcile did - or, in a dry run, would do."""

    applied: bool
    #: Days given a rate they did not have (`wage_rate` NULL -> current rate).
    days_priced: int = 0
    #: WAGE rows written for days that earned but had none.
    wages_created: int = 0
    #: Hand-typed WAGE rows linked to their day instead of being doubled.
    wages_adopted: int = 0
    #: Linked WAGE rows whose amount, date or note was corrected.
    wages_corrected: int = 0
    #: WAGE rows removed from days that earn nothing.
    wages_removed: int = 0
    #: Days that earn by status but whose worker still has no rate - left as
    #: they are, and listed so somebody can set the rate.
    still_unpriced: int = 0
    #: Total added to the crew's earnings by the above, net.
    net_change: Decimal = Decimal("0.00")
    #: Per-worker detail, for the report a contractor actually reads.
    by_worker: list[dict] = field(default_factory=list)
    #: Anything left that the reconcile could not resolve. Empty on success.
    remaining_issues: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def price_unpriced_days(db: Session, worker: User) -> int:
    """Price the days this worker has that were marked without a rate.

    Called when a worker's rate or pay basis is saved. Only days whose
    `wage_rate` is NULL are touched - a day already priced keeps its rate, and
    a piece-rate day (0) is never given a day rate - and a WAGE typed by hand
    for exactly that day and amount is adopted rather than doubled. That is
    the reconcile, narrowed to one worker; see `reconcile_wages`. Returns how
    many days were priced.
    """
    report = reconcile_wages(
        db, worker.contractor_id, worker_id=worker.id, apply=True, only_unpriced=True
    )
    return report.days_priced


def reconcile_wages(
    db: Session,
    contractor_id: Optional[int] = None,
    *,
    worker_id: Optional[int] = None,
    apply: bool = False,
    only_unpriced: bool = False,
) -> ReconcileReport:
    """Make every attendance row's wage what the day says. Dry-run by default.

    Runs inside a SAVEPOINT. With `apply=False` (the default) everything is
    rolled back after the report is built, so the report is exactly what
    applying would do - not a separate prediction that could disagree with it.
    The caller commits when `apply=True`.

    `worker_id` narrows it to one worker; `only_unpriced` to the days that
    have no rate yet, leaving every other day exactly as it is.
    """
    report = ReconcileReport(applied=apply)
    per_worker: dict[int, dict] = defaultdict(
        lambda: {
            "days_priced": 0,
            "wages_created": 0,
            "wages_adopted": 0,
            "wages_corrected": 0,
            "wages_removed": 0,
            "still_unpriced": 0,
            "net_change": Decimal("0.00"),
        }
    )

    savepoint = db.begin_nested()
    try:
        crew_stmt = _crew_filter(
            select(User).where(User.id.in_(select(Attendance.worker_id).distinct())),
            contractor_id,
            worker_id,
        )
        workers = {worker.id: worker for worker in db.scalars(crew_stmt).all()}
        if not workers:
            savepoint.rollback()
            return report

        rows_stmt = select(Attendance).where(Attendance.worker_id.in_(list(workers)))
        if only_unpriced:
            rows_stmt = rows_stmt.where(Attendance.wage_rate.is_(None))
        rows = db.scalars(
            rows_stmt.order_by(Attendance.worker_id, Attendance.date, Attendance.id)
            .with_for_update()
        ).all()

        # Every WAGE that nothing links to - the candidates a day may adopt.
        linked = set(
            db.scalars(
                select(Attendance.transaction_id).where(
                    Attendance.transaction_id.is_not(None)
                )
            ).all()
        ) | set(
            db.scalars(
                select(PieceRateLog.transaction_id).where(
                    PieceRateLog.transaction_id.is_not(None)
                )
            ).all()
        )
        adoptable: dict[tuple[int, date_type, Decimal], list[Transaction]] = defaultdict(list)
        for wage in db.scalars(
            select(Transaction)
            .where(
                Transaction.worker_id.in_(list(workers)),
                Transaction.type == TransactionType.WAGE,
            )
            .order_by(Transaction.id)
        ).all():
            if wage.id not in linked:
                adoptable[(wage.worker_id, wage.date, Decimal(wage.amount))].append(wage)

        for row in rows:
            worker = workers[row.worker_id]
            tally = per_worker[worker.id]
            before = (
                db.get(Transaction, row.transaction_id) if row.transaction_id else None
            )
            before_amount = Decimal(before.amount) if before is not None else Decimal("0.00")
            before_state = (
                (before.amount, before.date, before.note, before.site_id)
                if before is not None
                else None
            )

            if row.wage_rate is None:
                rate = rate_for_new_day(worker)
                if rate is None:
                    if weight(row.status) > 0:
                        tally["still_unpriced"] += 1
                        report.still_unpriced += 1
                    continue
                row.wage_rate = rate
                tally["days_priced"] += 1
                report.days_priced += 1

            expected = expected_wage(row.status, row.wage_rate)

            if expected is not None and before is None:
                pool = adoptable.get((row.worker_id, row.date, expected))
                if pool:
                    wage = pool.pop(0)
                    row.transaction_id = wage.id
                    db.flush()
                    tally["wages_adopted"] += 1
                    report.wages_adopted += 1
                    # Adopted, not created: the money was already in the
                    # ledger, so the balance does not move.
                    continue

            after = sync_attendance_wage(db, row, worker)
            after_amount = Decimal(after.amount) if after is not None else Decimal("0.00")

            if before is None and after is not None:
                tally["wages_created"] += 1
                report.wages_created += 1
            elif before is not None and after is None:
                tally["wages_removed"] += 1
                report.wages_removed += 1
            elif before is not None and after is not None and before_state != (
                after.amount,
                after.date,
                after.note,
                after.site_id,
            ):
                tally["wages_corrected"] += 1
                report.wages_corrected += 1

            delta = after_amount - before_amount
            tally["net_change"] += delta
            report.net_change += delta

        db.flush()
        if not only_unpriced:
            report.remaining_issues = [
                asdict(issue)
                for issue in audit_wages(db, contractor_id, worker_id)
                if issue.kind != "unpriced"
            ]

        report.by_worker = [
            {"worker_id": worker_id, "worker_name": workers[worker_id].name, **tally}
            for worker_id, tally in sorted(per_worker.items())
            if any(value for key, value in tally.items())
        ]

        if apply:
            savepoint.commit()
        else:
            savepoint.rollback()
    except Exception:
        savepoint.rollback()
        raise

    return report


def count_by_kind(issues: list[WageIssue]) -> dict[str, int]:
    """Issues by kind, for a one-line summary."""
    counts: dict[str, int] = defaultdict(int)
    for issue in issues:
        counts[issue.kind] += 1
    return dict(counts)


__all__ = [
    "ReconcileReport",
    "WageIssue",
    "audit_wages",
    "count_by_kind",
    "reconcile_wages",
]
