from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import UserRole, WageType, WorkerTrade

Phone = Annotated[str, Field(min_length=7, max_length=20, pattern=r"^\+?[0-9\- ]{7,20}$")]

# bcrypt reads at most 72 bytes, so there is no point accepting longer secrets.
Password = Annotated[str, Field(min_length=6, max_length=72)]


def _strip_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("name must not be blank")
    return v


Name = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_strip_name)]


class UserBase(BaseModel):
    name: Name
    phone: Phone
    role: UserRole
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    # What the worker does on site. None means it was never recorded, which
    # the app shows as "No trade set" rather than guessing.
    trade: Optional[WorkerTrade] = None
    # How they are paid. Drives which control the attendance grid draws for
    # them: a P/A/H toggle, or a quantity and a rate.
    wage_type: WageType = WageType.DAILY
    contractor_id: Optional[int] = Field(default=None, gt=0)
    # Which site the worker is currently posted to. The rain-out endpoint and
    # the site crew list both key off this, so it has to be readable.
    site_id: Optional[int] = Field(default=None, gt=0)
    is_active: bool = True


class UserCreate(UserBase):
    pass


class WorkerCreate(BaseModel):
    """Payload a contractor sends to onboard one of their workers.

    The worker is always attached to the calling contractor, so contractor_id
    and role are not accepted here.
    """

    name: Name
    phone: Phone
    password: Password
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Optional[WorkerTrade] = None
    wage_type: WageType = WageType.DAILY
    site_id: Optional[int] = Field(default=None, gt=0)


class WorkerUpdate(BaseModel):
    """What a contractor may change about one of their own workers.

    Narrower than UserUpdate on purpose: `role`, `contractor_id` and `phone`
    are all absent, because moving a worker to another crew or changing the
    number they sign in with is not an edit, it is a different operation with
    different consequences.
    """

    name: Optional[Name] = None
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Optional[WorkerTrade] = None
    # Switching an existing worker between daily and piece-rate. Optional, so
    # an edit that does not mention it leaves them on whatever they were.
    wage_type: Optional[WageType] = None
    # Explicit null unposts the worker from every site, which is why this
    # endpoint reads exclude_unset rather than treating None as "no change".
    site_id: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


class UserUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    name: Optional[Name] = None
    phone: Optional[Phone] = None
    role: Optional[UserRole] = None
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Optional[WorkerTrade] = None
    wage_type: Optional[WageType] = None
    contractor_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = None


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class SiteBrief(BaseModel):
    """Just enough of a site to name it and point a map at it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: Optional[str] = None


class WorkerProfile(UserRead):
    """The worker's own record, with the site they are currently on.

    `site` is what the dashboard needs for the site card and the directions
    button; `id` is what goes into the QR code.
    """

    site: Optional[SiteBrief] = None
