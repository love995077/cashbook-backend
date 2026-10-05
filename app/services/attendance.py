"""Every write to a day of attendance, in one place.

WHY ONE MODULE
--------------
A day of attendance is not one row. It is the row, the WAGE it earns
(app/services/wages.py) and the installment it recovers from an active
advance plan (app/services/deductions.py), and all three have to move
together or the ledger stops matching the register.

They used to move together in five routes, each written separately: the
single mark, the crew grid, the QR scan, the site rain-out and the
correction. They had drifted. The single mark (which is also what the offline
queue replays) took no installment while the grid did; an overwriting grid
resubmission and a rain-out over a present day changed the status but left
that day's installment in place; nothing stopped a removed worker from being
marked. The routes now validate their own inputs and hand the day to the
three functions here, which are the only code that creates, changes or
deletes an attendance row:

    record_day    mark a day (create, or overwrite when asked)
    correct_day   change a day already marked (date, status, shift, note)
    remove_day    unmark a day that should never have been marked

None of them commits. The route owns the transaction, so the day, its wage
and its installment land together or not at all.
"""

from __future__ import annotations

import enum
from datetime import date as date_type
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.models.attendance import Attendance
from app.models.deduction import DeductionInstallment
from app.models.enums import AttendanceStatus, WorkShift
from app.models.piece_rate import PieceRateLog
from app.models.user import User
from app.services.deductions import apply_installment_for, resettle_installment
from app.services.wages import (
    clear_attendance_wage,
    rate_for_new_day,
    sync_attendance_wage,
)


class DayOutcome(str, enum.Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


#: Sentinel for "the caller has not looked the existing row up".
_NOT_LOOKED_UP: Any = object()


def check_markable(worker: User, day: date_type) -> None:
    """Refuse a day that cannot be marked for this worker. 422 either way.

    Two rules:

      * Not in the future, by the BUSINESS calendar (app/core/clock.py) - a
        server on UTC would otherwise refuse the morning's attendance for the
        first five and a half hours of every Indian day.
      * Not on or after the day a worker was deactivated. Days before it can
        still be marked: a worker who left on the 20th worked the 18th. A
        worker deactivated before `deactivated_on` existed has no date, and
        gets no new days at all.

    422 rather than 409 on purpose. The offline queue reads a 409 on an
    attendance replay as "already recorded" and drops it silently; a refusal
    has to surface to the contractor instead.
    """
    reason = why_unmarkable(worker, day)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=reason)


def why_unmarkable(worker: User, day: date_type) -> Optional[str]:
    """The reason `check_markable` would refuse, or None. For batch checks."""
    if day > business_today():
        return "A day cannot be marked before it happens"
    if not worker.is_active:
        cutoff = worker.deactivated_on
        if cutoff is None or day >= cutoff:
            since = f" on {cutoff:%d %b %Y}" if cutoff else ""
            return (
                f"{worker.name} was deactivated{since}, so this day cannot be "
                "marked. Reactivate them first if they are back on the crew."
            )
    return None


def find_day(db: Session, worker_id: int, day: date_type) -> Optional[Attendance]:
    """The worker's row for `day`, locked for the rest of the request.

    Locked because the caller is about to rewrite it and the wage hanging off
    it: two corrections racing on one day would otherwise each read the old
    status and each settle the installment against it. (A no-op on SQLite.)
    """
    return db.scalar(
        select(Attendance)
        .where(Attendance.worker_id == worker_id, Attendance.date == day)
        .with_for_update()
    )


def record_day(
    db: Session,
    worker: User,
    day: date_type,
    day_status: AttendanceStatus,
    *,
    shift: WorkShift = WorkShift.DAY,
    note: Optional[str] = None,
    site_id: Optional[int] = None,
    overwrite: bool = False,
    existing: Any = _NOT_LOOKED_UP,
) -> tuple[Attendance, DayOutcome, Optional[DeductionInstallment]]:
    """Mark one worker's day, with its wage and its installment.

    A worker with no row for the day gets one. A worker who already has one
    is left alone unless `overwrite` is set and the status or shift differs -
    then the row is corrected, and the wage and installment follow it.

    `existing` lets a caller that has already batch-loaded the day's rows (the
    crew grid, the rain-out) pass the one it found - None included - instead
    of costing a query per worker.

    May raise IntegrityError from the flush when another request marked the
    same worker for the same day a moment earlier; the caller decides what
    that means (a 409, a retry, or "return the row that won").
    """
    row = find_day(db, worker.id, day) if existing is _NOT_LOOKED_UP else existing

    if row is None:
        check_markable(worker, day)
        row = Attendance(
            worker_id=worker.id,
            date=day,
            # Where the day was worked, stamped on the row so moving the
            # worker later does not rewrite where they were.
            site_id=site_id,
            status=day_status,
            shift=shift,
            note=note,
            # The rate the day is worth, frozen now. See `Attendance.wage_rate`.
            wage_rate=rate_for_new_day(worker),
        )
        db.add(row)
        db.flush()
        sync_attendance_wage(db, row, worker)
        installment = apply_installment_for(db, worker.id, day, day_status)
        return row, DayOutcome.CREATED, installment

    if overwrite and (row.status is not day_status or row.shift is not shift):
        old_status = row.status
        row.status = day_status
        row.shift = shift
        if row.site_id is None and site_id is not None:
            row.site_id = site_id
        if note:
            row.note = note
        db.flush()
        installment = resettle_installment(db, worker.id, day, old_status, day, day_status)
        sync_attendance_wage(db, row, worker)
        return row, DayOutcome.UPDATED, installment

    # Left as it is. Still re-synced and still offered the installment, both of
    # which are idempotent: a resubmitted grid settles on the same single wage
    # and the (plan, day) unique constraint turns a repeat deduction into a
    # no-op. Re-syncing cannot reprice the day - the rate is frozen on the row.
    sync_attendance_wage(db, row, worker)
    installment = apply_installment_for(db, worker.id, day, row.status)
    return row, DayOutcome.UNCHANGED, installment


#: Fields a correction may set, and whether an explicit null is meaningful.
#: Only the note can be cleared; a null date, status or shift is a malformed
#: request, not an instruction, and is ignored rather than written into a NOT
#: NULL column.
_CORRECTABLE = {"date": False, "status": False, "shift": False, "note": True}


def correct_day(
    db: Session, row: Attendance, worker: User, fields: dict[str, Any]
) -> Attendance:
    """Change a day that was already marked.

    Moving a day to another date is checked like a new mark - it cannot move
    into the future or past the worker's deactivation. Changing only its
    status is a correction of history and is allowed for a worker who has
    since left, because the day itself is not new.

    May raise IntegrityError from the flush when the day is moved onto a date
    the worker is already marked for; the route answers that with a 409.
    """
    changes = {
        key: value
        for key, value in fields.items()
        if key in _CORRECTABLE and (value is not None or _CORRECTABLE[key])
    }
    if not changes:
        return row

    old_date, old_status = row.date, row.status
    new_date = changes.get("date", old_date)
    if new_date != old_date:
        check_markable(worker, new_date)

    for key, value in changes.items():
        setattr(row, key, value)

    # Flushed before the installment work rather than left to the commit.
    # `apply_installment` runs its insert inside a SAVEPOINT and swallows
    # IntegrityError as "this day is already deducted" - a clash on
    # uq_attendance_worker_date surfacing in there would be caught by the wrong
    # handler and roll this edit back without saying so.
    db.flush()

    resettle_installment(db, worker.id, old_date, old_status, row.date, row.status)
    # The day's wage follows the day: corrected to absent it is taken back,
    # to a half day it is halved, moved to another date it moves with it. The
    # amount comes from the rate frozen on the row, never today's rate.
    sync_attendance_wage(db, row, worker)
    return row


def remove_day(db: Session, row: Attendance, worker: User) -> None:
    """Unmark a day that should never have been marked at all.

    Refuses a day that paid piece-rate wages: the log wrote a WAGE of its own,
    and deleting the attendance would leave money paid for a day the worker is
    no longer recorded as having worked. The 409 names what to remove first.
    """
    if db.scalar(
        select(PieceRateLog.id)
        .where(PieceRateLog.worker_id == row.worker_id, PieceRateLog.date == row.date)
        .limit(1)
    ) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This day paid piece-rate wages - delete the piece-rate log first",
        )

    day, old_status = row.date, row.status

    # Before the delete, while the link is still readable.
    clear_attendance_wage(db, row)

    db.delete(row)
    # Flushed before the installment work for the same reason `correct_day` is.
    db.flush()

    # "Give back this day's installment and take nothing in its place".
    resettle_installment(db, worker.id, day, old_status, day, AttendanceStatus.ABSENT)
