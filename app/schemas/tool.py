from datetime import datetime
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import ToolAssignmentStatus


def _strip(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


ToolName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_strip)]


class ToolCreate(BaseModel):
    name: ToolName
    # Three identical drills are one row with a count of 3, not three rows:
    # nobody labels them, and the only question asked is "is one free".
    inventory_count: int = Field(default=1, ge=1, le=999)
    notes: Optional[str] = Field(default=None, max_length=255)


class ToolUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    name: Optional[ToolName] = None
    inventory_count: Optional[int] = Field(default=None, ge=1, le=999)
    notes: Optional[str] = Field(default=None, max_length=255)
    is_active: Optional[bool] = None


class ToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    name: str
    inventory_count: int
    notes: Optional[str] = None
    is_active: bool

    # Derived from the open assignments rather than stored, so it cannot
    # drift out of step with them.
    out_count: int = 0
    available_count: int = 0
    #: The open checkouts, so a row can be expanded without a second request.
    assignments: list["ToolAssignmentRead"] = []


class ToolAssignmentCreate(BaseModel):
    worker_id: int = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=255)


class ToolReturn(BaseModel):
    """Booking a tool back in, or writing it off.

    LOST is a return in the bookkeeping sense — the assignment closes and the
    tool stops counting as out — but it is kept apart from RETURNED because
    it is the one that costs money and the one worth listing at the end of a
    job.
    """

    status: ToolAssignmentStatus = ToolAssignmentStatus.RETURNED
    note: Optional[str] = Field(default=None, max_length=255)


class ToolAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tool_id: int
    tool_name: Optional[str] = None
    site_id: Optional[int] = None
    worker_id: int
    worker_name: Optional[str] = None
    checkout_time: datetime
    return_time: Optional[datetime] = None
    status: ToolAssignmentStatus
    note: Optional[str] = None


class OutstandingTools(BaseModel):
    """What is still out, for the end-of-shift badge on the dashboard.

    A count and the rows behind it in one response: the badge needs the
    number, and tapping it needs the list, and asking twice for one glance
    at the dashboard is a round trip nobody is paying for.
    """

    #: Assignments still OUT across every site this contractor runs.
    out_count: int
    #: Of those, the ones checked out before today — the ones that matter,
    #: since a tool taken out this morning is not yet a problem.
    overdue_count: int
    assignments: list[ToolAssignmentRead] = []


ToolRead.model_rebuild()
