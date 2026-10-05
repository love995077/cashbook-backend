from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


SiteName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_strip)]


class SiteBase(BaseModel):
    name: SiteName
    # Free text, and what the app hands to Google Maps for directions — so it
    # is worth typing something a map can actually find.
    address: Optional[str] = Field(default=None, max_length=255)


class SiteCreate(SiteBase):
    pass


class SiteUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    name: Optional[SiteName] = None
    address: Optional[str] = Field(default=None, max_length=255)
    # Closing a site keeps its history and drops it out of the pickers.
    is_active: Optional[bool] = None
    # Who runs the site day to day. Sent explicitly as null to unassign, which
    # is why the whole payload is read with `exclude_unset` rather than by
    # dropping Nones — the two mean different things here.
    supervisor_id: Optional[int] = None


class SiteRead(SiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Counted for the list screen, which is otherwise a row of bare names with
    # no way to tell a live job from a finished one.
    worker_count: int = 0
    photo_count: int = 0

    # The supervisor running this site, if one is assigned. The name is
    # flattened alongside the id so the sites list can say who is on it
    # without a lookup per row.
    supervisor_id: Optional[int] = None
    supervisor_name: Optional[str] = None


class SiteOverview(BaseModel):
    """One site's card on the site-selection Home screen.

    Money is by the site stamped on each row; "still owed" and the crew counts
    are about who is posted here now. See app/services/site_overview.py.
    """

    site_id: int
    #: Active workers posted here.
    crew_active: int = 0
    #: Deactivated workers still recorded against this site.
    crew_inactive: int = 0
    #: Attendance rows for today at this site, and what they add up to in days
    #: (a half day counts half).
    marked_today: int = 0
    worked_today: Decimal = Decimal("0")
    #: All time, by the site on each row.
    wages_earned: Decimal = Decimal("0.00")
    wages_paid: Decimal = Decimal("0.00")
    cash_in: Decimal = Decimal("0.00")
    expenses_paid: Decimal = Decimal("0.00")
    petty_cash: Decimal = Decimal("0.00")
    #: Positive balances of the active crew posted here.
    outstanding_payable: Decimal = Decimal("0.00")
    #: Wages paid out, expenses and petty cash at this site since the 1st.
    month_spend: Decimal = Decimal("0.00")
    #: The business day these were counted on.
    today: date_type


class SitePhotoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    date: date_type
    description: Optional[str] = None
    uploaded_by: Optional[int] = None
    uploader_name: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: datetime

    # Derived from the stored key at read time, never persisted — moving to a
    # CDN then changes one function instead of every row.
    url: str


class RainOutResult(BaseModel):
    """What a site-wide rain-out actually changed.

    Split three ways on purpose. "12 workers marked" hides that three of them
    were already down as present and have just been overwritten, which is
    exactly the thing a contractor wants to know before they walk away.
    """

    site_id: int
    site_name: str
    date: date_type
    #: Workers who had no attendance for the day and now read rain_out.
    created: int
    #: Workers whose existing status was replaced by rain_out.
    updated: int
    #: Already marked rain_out; left alone.
    unchanged: int
    #: Every worker id the call touched or considered.
    worker_ids: list[int]
