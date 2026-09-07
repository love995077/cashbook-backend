"""Plant and diesel: what ran today, and what the fuel cost.

The cost that surprises a contractor at the end of a job is rarely the wages —
those are counted every day — it is the fuel nobody wrote down. So this is a
supervisor-facing endpoint rather than an admin one: the person who can say
how long the mixer ran is the person standing next to it.

The diesel receipt travels with the form, so the create route is multipart
rather than JSON, and the file is written to storage BEFORE the row is
inserted — the same ordering, for the same reason, as the site photo and the
material challan. See `app/routers/inventory.py`.

WHAT A SUPERVISOR MAY SEE HERE
------------------------------
Fuel cost is a cost, but it is not project finance: it is the number the
person running the site is best placed to record, and the one they need back
to argue that a machine is drinking more than it should. `CurrentSiteStaff`
therefore covers the whole router, scoped per site — a supervisor sees the
diesel bill for their own site and nothing else about the money.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.dependencies import CurrentSiteStaff, DbSession, require_site_access
from app.models.machinery import MachineryLog
from app.schemas.machinery import (
    MachineryLogCreate,
    MachineryLogRead,
    MachineryLogUpdate,
    MachineryTotals,
    MachineUsage,
)
from app.services.storage import (
    StorageError,
    UnsupportedMedia,
    delete_stored,
    public_url,
    store_upload,
)

router = APIRouter(prefix="/contractor/sites/{site_id}/machinery", tags=["machinery"])
settings = get_settings()

ZERO = Decimal("0")


def _read_log(log: MachineryLog) -> MachineryLogRead:
    read = MachineryLogRead.model_validate(log)
    read.logged_by_name = log.logger.name if log.logger else None
    read.receipt_image_url = public_url(log.receipt_image_path)
    return read


def _get_log(db: Session, site_id: int, log_id: int) -> MachineryLog:
    log = db.get(MachineryLog, log_id)
    if log is None or log.site_id != site_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machinery log not found"
        )
    return log


@router.get("", response_model=list[MachineryLogRead])
def list_machinery_logs(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MachineryLogRead]:
    """This site's machinery logs, newest day first."""
    require_site_access(db, staff, site_id)

    stmt = (
        select(MachineryLog)
        .options(joinedload(MachineryLog.logger))
        .where(MachineryLog.site_id == site_id)
    )
    if start_date is not None:
        stmt = stmt.where(MachineryLog.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(MachineryLog.date <= end_date)

    logs = db.scalars(
        stmt.order_by(MachineryLog.date.desc(), MachineryLog.id.desc()).limit(limit)
    ).all()
    return [_read_log(log) for log in logs]


@router.get("/totals", response_model=MachineryTotals)
def read_machinery_totals(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
) -> MachineryTotals:
    """What the plant on this site ran and burned over a window.

    Grouped in the database rather than summed over the list above, so the
    header figure stays right when the list is truncated by `limit` — a site
    with three hundred logs would otherwise total only the hundred it drew.
    """
    require_site_access(db, staff, site_id)
    until = end_date or date_type.today()

    filters = [MachineryLog.site_id == site_id, MachineryLog.date <= until]
    if start_date is not None:
        filters.append(MachineryLog.date >= start_date)

    rows = db.execute(
        select(
            MachineryLog.machine_name,
            func.sum(MachineryLog.hours_operated),
            func.sum(MachineryLog.diesel_litres),
            func.sum(MachineryLog.fuel_cost),
            func.count(),
        )
        .where(*filters)
        .group_by(MachineryLog.machine_name)
        .order_by(func.sum(MachineryLog.fuel_cost).desc())
    ).all()

    by_machine = [
        MachineUsage(
            machine_name=name,
            hours_operated=hours or ZERO,
            diesel_litres=litres or ZERO,
            fuel_cost=cost or ZERO,
            log_count=count,
        )
        for name, hours, litres, cost, count in rows
    ]

    return MachineryTotals(
        site_id=site_id,
        start_date=start_date,
        end_date=until,
        hours_operated=sum((row.hours_operated for row in by_machine), ZERO),
        diesel_litres=sum((row.diesel_litres for row in by_machine), ZERO),
        fuel_cost=sum((row.fuel_cost for row in by_machine), ZERO),
        log_count=sum(row.log_count for row in by_machine),
        by_machine=by_machine,
    )


@router.post(
    "",
    response_model=MachineryLogRead,
    status_code=status.HTTP_201_CREATED,
    summary="Log one machine for one day, with the diesel receipt",
)
async def add_machinery_log(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    machine_name: str = Form(..., description="Free text: JCB, mixer, hoist"),
    hours_operated: Decimal = Form(default=Decimal("0")),
    diesel_litres: Decimal = Form(default=Decimal("0")),
    fuel_cost: Decimal = Form(default=Decimal("0")),
    date: Optional[date_type] = Form(default=None, description="Defaults to today"),
    note: Optional[str] = Form(default=None, max_length=255),
    receipt: Optional[UploadFile] = File(
        default=None, description="Photo of the diesel receipt"
    ),
) -> MachineryLogRead:
    require_site_access(db, staff, site_id)

    # Validated through the schema rather than by hand, so a multipart form
    # cannot end up with a different idea of a valid log than a JSON body would.
    #
    # Constructed rather than declared as a body, because the diesel receipt
    # makes this multipart — which means FastAPI is not doing the validating
    # and a ValidationError raised here would surface as a 500. Re-raised as
    # the 422 the caller would have got from a JSON body.
    try:
        payload = MachineryLogCreate(
            machine_name=machine_name,
            hours_operated=hours_operated,
            diesel_litres=diesel_litres,
            fuel_cost=fuel_cost,
            date=date,
            note=note,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ],
        ) from exc

    when = payload.date or date_type.today()
    if when > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A machine cannot be logged before the day it ran",
        )

    stored = None
    if receipt is not None:
        image = await receipt.read()
        if image:
            if len(image) > settings.max_image_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        "Receipt photos must be under "
                        f"{settings.max_image_bytes // (1024 * 1024)} MB"
                    ),
                )
            try:
                stored = store_upload(
                    image,
                    kind="photos",
                    owner_id=staff.id,
                    content_type=receipt.content_type,
                    label=f"diesel {payload.machine_name}",
                )
            except UnsupportedMedia as exc:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
                ) from exc
            except StorageError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc

    log = MachineryLog(
        site_id=site_id,
        machine_name=payload.machine_name,
        hours_operated=payload.hours_operated,
        diesel_litres=payload.diesel_litres,
        fuel_cost=payload.fuel_cost,
        receipt_image_path=stored.path if stored else None,
        date=when,
        note=(payload.note or "").strip() or None,
        logged_by=staff.id,
    )
    db.add(log)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Do not leave the bytes behind for a log that does not exist.
        if stored:
            delete_stored(stored.path)
        raise

    db.refresh(log)
    return _read_log(log)


@router.patch("/{log_id}", response_model=MachineryLogRead)
def edit_machinery_log(
    site_id: int,
    log_id: int,
    payload: MachineryLogUpdate,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> MachineryLogRead:
    """Correct a log. The receipt photo is not replaceable — delete and re-add.

    A number typed wrong is the common case and needs no new paperwork; a
    different receipt is a different receipt.
    """
    require_site_access(db, staff, site_id)
    log = _get_log(db, site_id, log_id)

    fields = payload.model_dump(exclude_unset=True)
    if fields.get("date") and fields["date"] > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A machine cannot be logged before the day it ran",
        )

    for field, value in fields.items():
        setattr(log, field, value)

    db.commit()
    db.refresh(log)
    return _read_log(log)


@router.delete("/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_machinery_log(
    site_id: int, log_id: int, staff: CurrentSiteStaff, db: DbSession
) -> Response:
    require_site_access(db, staff, site_id)
    log = _get_log(db, site_id, log_id)

    path = log.receipt_image_path
    db.delete(log)
    db.commit()
    # After the commit: an orphaned file is recoverable, a row pointing at a
    # missing image is not.
    delete_stored(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
