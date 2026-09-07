"""Small cash spent at a site, logged where it is spent.

Site-scoped rather than contractor-scoped, and separate from the cash box on
purpose. A `CashEntry` is filed under a category the contractor maintains;
petty cash is the auto fare, the crew's afternoon tea, the twenty rupees for
a packet of nails — entries that would either bury themselves under
"Miscellaneous" or grow the category list by one bucket per shop.

Ownership is proved through the site, the same way the photo feed does it.
"""

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.dependencies import CurrentContractor, DbSession, require_own_site
from app.models.petty_cash import PettyCash
from app.schemas.petty_cash import PettyCashCreate, PettyCashRead, PettyCashTotals

router = APIRouter(prefix="/contractor/sites/{site_id}/petty-cash", tags=["petty cash"])


def _read(entry: PettyCash) -> PettyCashRead:
    read = PettyCashRead.model_validate(entry)
    read.recorded_by_name = entry.recorder.name if entry.recorder else None
    return read


@router.get("", response_model=list[PettyCashRead])
def list_petty_cash(
    site_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PettyCashRead]:
    """This site's petty cash, newest first."""
    require_own_site(db, contractor, site_id)

    stmt = (
        select(PettyCash)
        .options(joinedload(PettyCash.recorder))
        .where(PettyCash.site_id == site_id)
    )
    if start_date is not None:
        stmt = stmt.where(PettyCash.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(PettyCash.date <= end_date)

    entries = db.scalars(
        stmt.order_by(PettyCash.date.desc(), PettyCash.id.desc()).limit(limit)
    ).all()
    return [_read(entry) for entry in entries]


@router.post("", response_model=PettyCashRead, status_code=status.HTTP_201_CREATED)
def add_petty_cash(
    site_id: int,
    payload: PettyCashCreate,
    contractor: CurrentContractor,
    db: DbSession,
) -> PettyCashRead:
    """Log one cash outflow against this site."""
    require_own_site(db, contractor, site_id)

    when = payload.date or date_type.today()
    if when > date_type.today():
        # The same rule the photo feed and the rain-out both apply: money that
        # has not been spent yet is a plan, not a ledger entry.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Petty cash cannot be dated in the future",
        )

    entry = PettyCash(
        site_id=site_id,
        amount=payload.amount,
        description=payload.description,
        date=when,
        recorded_by=contractor.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _read(entry)


@router.get("/totals", response_model=PettyCashTotals)
def read_petty_cash_totals(
    site_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(
        default=None, description="Omit to total the site's whole history"
    ),
    end_date: Optional[date_type] = Query(default=None, description="Defaults to today"),
) -> PettyCashTotals:
    """What this site has leaked on small cash, all time and over a window.

    Two figures rather than one: the tab header shows the month, but the
    number a contractor argues about at the end of a job is the total, and
    fetching it separately would cost a second round trip for one glance.
    """
    require_own_site(db, contractor, site_id)
    end = end_date or date_type.today()

    total, count = db.execute(
        select(func.coalesce(func.sum(PettyCash.amount), 0), func.count()).where(
            PettyCash.site_id == site_id
        )
    ).one()

    period_stmt = select(func.coalesce(func.sum(PettyCash.amount), 0)).where(
        PettyCash.site_id == site_id, PettyCash.date <= end
    )
    if start_date is not None:
        period_stmt = period_stmt.where(PettyCash.date >= start_date)

    return PettyCashTotals(
        site_id=site_id,
        total=total,
        period_total=db.scalar(period_stmt) or 0,
        start_date=start_date,
        end_date=end,
        entry_count=count,
    )


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_petty_cash(
    site_id: int, entry_id: int, contractor: CurrentContractor, db: DbSession
) -> Response:
    require_own_site(db, contractor, site_id)

    entry = db.get(PettyCash, entry_id)
    if entry is None or entry.site_id != site_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Petty cash entry not found"
        )

    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
