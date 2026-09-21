from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import UserRole, WageType

Phone = Annotated[str, Field(min_length=7, max_length=20, pattern=r"^\+?[0-9\- ]{7,20}$")]

# bcrypt reads at most 72 bytes, so there is no point accepting longer secrets.
Password = Annotated[str, Field(min_length=6, max_length=72)]


def _strip_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("name must not be blank")
    return v


Name = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_strip_name)]


def _normalise_trade(v: Optional[str]) -> Optional[str]:
    """Fold a typed-in post to one canonical spelling.

    The post used to be an enum, for a reason worth keeping: "Electrician",
    "electrician" and "ELECTRICIAN" are one post, and storing them as typed
    splits that post across three rows of every breakdown.

    Free text keeps the flexibility a real crew needs — shuttering carpenter,
    chowkidar, bar-bending helper — without giving that up: whitespace is
    collapsed, the value is lowercased and spaces and hyphens become
    underscores. So the well-known posts land on exactly the values the old
    enum used (`bar_bender`, `general_labour`) and the app's icon lookup still
    finds them, while anything new is merely consistent with itself.

    Display is the app's job: `site_engineer` is drawn as "Site engineer".
    Blank collapses to None — an empty string is not a post, it is an unset one.
    """
    if v is None:
        return None
    collapsed = "_".join(v.replace("-", " ").replace("_", " ").split())
    return collapsed.lower() or None


# A worker's post on site. Free text since the crew is not enumerable, but
# normalised so it stays countable. See `_normalise_trade`.
Trade = Annotated[
    Optional[str], Field(default=None, max_length=50), AfterValidator(_normalise_trade)
]


class UserBase(BaseModel):
    name: Name
    # Optional: a worker can be on the crew without a number to their name.
    # See the note on `User.phone`. Contractors always have one — they signed
    # up with it — so this being None in practice means "worker".
    phone: Optional[Phone] = None
    role: UserRole
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    # What the worker does on site. None means it was never recorded, which
    # the app shows as "No trade set" rather than guessing.
    trade: Trade = None
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

    `name` is the only thing required. Everything else describes a worker the
    contractor may not know yet on the morning they first turn up, and a form
    that refuses the booking until all of it is known is a form that gets
    filled with placeholder numbers.

    A worker created without `phone` or `password` cannot sign in. That is the
    intended reading: they are a record their contractor keeps and pays, and
    either field can be filled in later.
    """

    name: Name
    phone: Optional[Phone] = None
    password: Optional[Password] = None
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Trade = None
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
    trade: Trade = None
    # Switching an existing worker between daily and piece-rate. Optional, so
    # an edit that does not mention it leaves them on whatever they were.
    wage_type: Optional[WageType] = None
    # Explicit null unposts the worker from every site, which is why this
    # endpoint reads exclude_unset rather than treating None as "no change".
    site_id: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


class WorkerReplace(BaseModel):
    """The whole of an edit form, saved at once — PUT rather than PATCH.

    WHY A SECOND SHAPE INSTEAD OF REUSING WorkerUpdate
    --------------------------------------------------
    `WorkerUpdate` reads `exclude_unset`, so a field left out means "leave it
    alone". That is right for the one-field writes the app already makes -
    posting a worker to a site, deactivating one - and wrong for an edit
    screen, where clearing the day rate and never touching the day rate are
    different intentions that both arrive as an absent value.

    Here an omitted field means "make it empty", which is what replacing a
    record means and what lets the form clear a wage that was typed by
    mistake. `name` is the one exception: it is the only thing a worker record
    cannot be without, so it stays required.

    `phone` and `role` are deliberately still absent, for the reason
    `WorkerUpdate` gives - the number somebody signs in with is not a detail
    on a form.

    ON THE DAY RATE AND HISTORY
    ---------------------------
    Changing `daily_wage` does NOT reprice days already worked. Attendance
    writes its wage as a `Transaction` at the rate of the day (see
    app/services/wages.py), so raising someone's rate pays the new rate from
    here on and leaves last month's books saying what they actually said.
    """

    name: Name
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Trade = None
    wage_type: WageType = WageType.DAILY
    site_id: Optional[int] = Field(default=None, ge=0)
    is_active: bool = True


class UserUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    name: Optional[Name] = None
    phone: Optional[Phone] = None
    role: Optional[UserRole] = None
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    trade: Trade = None
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
