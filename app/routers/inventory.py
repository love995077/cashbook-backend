"""Goods Receipt Note: what was delivered, and what is on the ground now.

Accepting a delivery does two things in one database transaction — files the
receipt with its challan photo, and moves the site's live stock. Splitting
them would let a delivery be recorded that never reached the stock list, which
is precisely the drift a GRN exists to prevent.

The photo travels with the form, so these are multipart rather than JSON. The
file is written to storage BEFORE the rows are inserted, so a failed write
never leaves a receipt pointing at nothing; the reverse leaves an orphan on
disk, which is the cheaper failure and is cleaned up on the error path. Same
ordering as the site photo upload, for the same reason.
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
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.clock import business_today
from app.config import get_settings
from app.dependencies import CurrentSiteStaff, DbSession, require_site_access
from app.models.enums import MaterialUnit
from app.models.inventory import MaterialReceipt, SiteInventory
from app.schemas.inventory import MaterialReceiptRead, SiteInventoryRead
from app.services.storage import (
    StorageError,
    UnsupportedMedia,
    delete_stored,
    public_url,
    store_upload,
)

router = APIRouter(prefix="/contractor/sites/{site_id}", tags=["materials"])
settings = get_settings()


def _read_receipt(
    receipt: MaterialReceipt, stock: Optional[Decimal] = None
) -> MaterialReceiptRead:
    read = MaterialReceiptRead.model_validate(receipt)
    read.received_by_name = receipt.receiver.name if receipt.receiver else None
    read.challan_image_url = public_url(receipt.challan_image_path)
    read.quantity_in_stock = stock
    return read


def _find_stock(db: Session, site_id: int, item_name: str) -> Optional[SiteInventory]:
    """The site's row for this item, matched without regard to case, locked.

    Case-insensitive because the old database was: MySQL's collation treated
    "Cement" and "cement" as one item, so a delivery typed either way landed on
    the same row. PostgreSQL compares exactly, and the same two deliveries
    would have opened two rows each holding part of the stock - neither of
    which answers "how many bags are left". The migration adds a matching
    unique index on lower(item_name), so this is not only a convention.

    Locked because the caller is about to add to it: two deliveries of one item
    accepted at the same moment would otherwise both read 10, both write their
    own sum, and one delivery would vanish from the stock.
    """
    return db.scalar(
        select(SiteInventory)
        .where(
            SiteInventory.site_id == site_id,
            func.lower(SiteInventory.item_name) == item_name.lower(),
        )
        .with_for_update()
    )


def _upsert_stock(
    db: Session, site_id: int, item_name: str, quantity: Decimal, unit: MaterialUnit
) -> SiteInventory:
    """Add a delivery to the site's stock, creating the item on first receipt.

    The unit is settled the FIRST time an item arrives at a site and reused
    after. A later delivery that claims a different unit is refused rather
    than converted: nobody here knows how many kg are in a brass of sand, and
    guessing would corrupt the only number this table exists to hold.
    """
    row = _find_stock(db, site_id, item_name)

    if row is None:
        try:
            # A SAVEPOINT around the insert, with the add inside it, so losing
            # a race to create the same item rolls back this row alone.
            with db.begin_nested():
                row = SiteInventory(
                    site_id=site_id,
                    item_name=item_name,
                    quantity_in_stock=quantity,
                    unit=unit,
                )
                db.add(row)
                db.flush()
            return row
        except IntegrityError:
            # Another delivery created the item a moment ago. Add to that one.
            row = _find_stock(db, site_id, item_name)
            if row is None:
                raise

    if row.unit is not unit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{row.item_name} is already tracked at this site in "
                f"{row.unit.value.replace('_', ' ')}. Record this delivery in the "
                "same unit, or receive it under a different item name."
            ),
        )

    row.quantity_in_stock = Decimal(row.quantity_in_stock) + quantity
    db.flush()
    return row


# --- live stock -------------------------------------------------------------


@router.get("/inventory", response_model=list[SiteInventoryRead])
def list_inventory(
    site_id: int, staff: CurrentSiteStaff, db: DbSession
) -> list[SiteInventory]:
    """What this site is holding right now, item by item."""
    require_site_access(db, staff, site_id)
    return list(
        db.scalars(
            select(SiteInventory)
            .where(SiteInventory.site_id == site_id)
            .order_by(SiteInventory.item_name)
        ).all()
    )


# --- deliveries -------------------------------------------------------------


@router.get("/material-receipts", response_model=list[MaterialReceiptRead])
def list_material_receipts(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MaterialReceiptRead]:
    """Deliveries accepted at this site, newest first."""
    require_site_access(db, staff, site_id)

    stmt = (
        select(MaterialReceipt)
        .options(joinedload(MaterialReceipt.receiver))
        .where(MaterialReceipt.site_id == site_id)
    )
    if start_date is not None:
        stmt = stmt.where(MaterialReceipt.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(MaterialReceipt.date <= end_date)

    receipts = db.scalars(
        stmt.order_by(MaterialReceipt.date.desc(), MaterialReceipt.id.desc()).limit(limit)
    ).all()
    return [_read_receipt(receipt) for receipt in receipts]


@router.post(
    "/material-receipts",
    response_model=MaterialReceiptRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a delivery and add it to site stock",
)
async def receive_material(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    item_name: str = Form(..., max_length=120),
    quantity_received: Decimal = Form(..., gt=0),
    unit: MaterialUnit = Form(default=MaterialUnit.BAG),
    date: Optional[date_type] = Form(
        default=None, description="The day it arrived. Defaults to today."
    ),
    supplier: Optional[str] = Form(default=None, max_length=120),
    note: Optional[str] = Form(default=None, max_length=255),
    challan: Optional[UploadFile] = File(
        default=None, description="Photo of the delivery challan. JPEG, PNG, WebP or HEIC."
    ),
) -> MaterialReceiptRead:
    """Accept one delivery: file the challan and move the stock.

    The challan photo is optional in the API and pushed hard for in the app.
    A delivery arriving in the rain with no paper still has to be recordable —
    but a GRN nobody photographed is a number somebody typed, and the
    difference matters when the supplier's invoice turns up.
    """
    require_site_access(db, staff, site_id)

    item = item_name.strip()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The item name must not be blank",
        )

    when = date or business_today()
    if when > business_today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A delivery cannot be dated in the future",
        )

    stored = None
    if challan is not None:
        payload = await challan.read()
        if payload:
            if len(payload) > settings.max_image_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        "Challan photos must be under "
                        f"{settings.max_image_bytes // (1024 * 1024)} MB"
                    ),
                )
            try:
                stored = store_upload(
                    payload,
                    kind="photos",
                    owner_id=staff.id,
                    content_type=challan.content_type,
                    label=f"challan {item}",
                )
            except UnsupportedMedia as exc:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
                ) from exc
            except StorageError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc

    try:
        stock = _upsert_stock(db, site_id, item, quantity_received, unit)
        receipt = MaterialReceipt(
            site_id=site_id,
            item_name=item,
            quantity_received=quantity_received,
            unit=unit,
            inventory_id=stock.id,
            challan_image_path=stored.path if stored else None,
            date=when,
            supplier=(supplier or "").strip() or None,
            note=(note or "").strip() or None,
            received_by=staff.id,
        )
        db.add(receipt)
        db.commit()
    except Exception:
        db.rollback()
        # Do not leave the bytes behind for a receipt that does not exist.
        if stored:
            delete_stored(stored.path)
        raise

    db.refresh(receipt)
    db.refresh(stock)
    return _read_receipt(receipt, stock.quantity_in_stock)
