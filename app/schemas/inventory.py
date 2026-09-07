from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import MaterialUnit


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


ItemName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_strip)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]


class SiteInventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    item_name: str
    quantity_in_stock: Decimal
    unit: MaterialUnit
    updated_at: datetime


class MaterialReceiptCreate(BaseModel):
    """A delivery accepted at the gate.

    Sent as multipart rather than JSON, because the challan photo travels with
    it — see the endpoint. This model documents the field half of that form.
    """

    item_name: ItemName
    quantity_received: Quantity
    # Settled the first time an item is received at a site and reused after,
    # so bags and tons of the same cement can never end up added together.
    unit: MaterialUnit = MaterialUnit.BAG
    date: Optional[date_type] = None
    supplier: Optional[str] = Field(default=None, max_length=120)
    note: Optional[str] = Field(default=None, max_length=255)


class MaterialReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    item_name: str
    quantity_received: Decimal
    unit: MaterialUnit
    date: date_type
    supplier: Optional[str] = None
    note: Optional[str] = None
    received_by: Optional[int] = None
    received_by_name: Optional[str] = None
    created_at: datetime

    # Derived from the stored key at read time, never persisted — moving to a
    # CDN then changes one function instead of every row. Null when the
    # delivery was recorded without a photo.
    challan_image_url: Optional[str] = None

    #: Stock for this item AFTER the delivery was applied, so the app can
    #: confirm "40 bags now" without refetching the inventory list.
    quantity_in_stock: Optional[Decimal] = None
