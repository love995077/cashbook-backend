"""Who has the drill.

A grinder walks off a site because nobody could say who took it out. A tool
goes OUT against one worker and stays out until somebody books it back in,
and the dashboard counts what is still open at the end of the shift.

Two prefixes' worth of routes live here, which is why the router carries none
of its own: the store is per site, and the outstanding count the contractor's
dashboard badge reads spans every site they run.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.dependencies import (
    CurrentSiteStaff,
    DbSession,
    accessible_site_ids,
    require_site_access,
    require_site_worker,
)
from app.models.enums import ToolAssignmentStatus
from app.models.tool import Tool, ToolAssignment
from app.models.user import User
from app.schemas.tool import (
    OutstandingTools,
    ToolAssignmentCreate,
    ToolAssignmentRead,
    ToolCreate,
    ToolRead,
    ToolReturn,
    ToolUpdate,
)

router = APIRouter(tags=["tools"])


def _read_assignment(assignment: ToolAssignment) -> ToolAssignmentRead:
    read = ToolAssignmentRead.model_validate(assignment)
    read.tool_name = assignment.tool.name if assignment.tool else None
    read.site_id = assignment.tool.site_id if assignment.tool else None
    read.worker_name = assignment.worker.name if assignment.worker else None
    return read


def _open_counts(db: Session, tool_ids: list[int]) -> dict[int, int]:
    """How many of each tool are currently out, in one grouped query.

    Aggregated rather than counted per row: a store of thirty tools would
    otherwise cost thirty round trips to draw one screen.
    """
    if not tool_ids:
        return {}
    return dict(
        db.execute(
            select(ToolAssignment.tool_id, func.count())
            .where(
                ToolAssignment.tool_id.in_(tool_ids),
                ToolAssignment.status == ToolAssignmentStatus.OUT,
            )
            .group_by(ToolAssignment.tool_id)
        ).all()
    )


def _read_tool(
    tool: Tool, out: int, assignments: Optional[list[ToolAssignment]] = None
) -> ToolRead:
    read = ToolRead.model_validate(tool)
    read.out_count = out
    # Clamped at zero: an inventory_count edited down below what is already
    # out would otherwise report a negative number of drills on the shelf.
    read.available_count = max(tool.inventory_count - out, 0)
    read.assignments = [_read_assignment(row) for row in (assignments or [])]
    return read


def _get_tool(db: Session, staff: User, site_id: int, tool_id: int) -> Tool:
    """A tool at a site this admin owns or this supervisor is assigned."""
    require_site_access(db, staff, site_id)
    tool = db.get(Tool, tool_id)
    if tool is None or tool.site_id != site_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    return tool


# --- the site's tool store --------------------------------------------------


@router.get("/contractor/sites/{site_id}/tools", response_model=list[ToolRead])
def list_tools(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    include_inactive: bool = Query(default=False),
) -> list[ToolRead]:
    """This site's tools, each with who currently has one."""
    require_site_access(db, staff, site_id)

    stmt = select(Tool).where(Tool.site_id == site_id)
    if not include_inactive:
        stmt = stmt.where(Tool.is_active.is_(True))
    tools = list(db.scalars(stmt.order_by(Tool.is_active.desc(), Tool.name)).all())
    if not tools:
        return []

    tool_ids = [tool.id for tool in tools]
    counts = _open_counts(db, tool_ids)

    # The open checkouts for the whole store in one query, then bucketed —
    # the screen needs the names, not just the counts.
    open_rows = db.scalars(
        select(ToolAssignment)
        .options(joinedload(ToolAssignment.tool), joinedload(ToolAssignment.worker))
        .where(
            ToolAssignment.tool_id.in_(tool_ids),
            ToolAssignment.status == ToolAssignmentStatus.OUT,
        )
        .order_by(ToolAssignment.checkout_time)
    ).all()

    by_tool: dict[int, list[ToolAssignment]] = {}
    for row in open_rows:
        by_tool.setdefault(row.tool_id, []).append(row)

    return [
        _read_tool(tool, counts.get(tool.id, 0), by_tool.get(tool.id)) for tool in tools
    ]


@router.post(
    "/contractor/sites/{site_id}/tools",
    response_model=ToolRead,
    status_code=status.HTTP_201_CREATED,
)
def add_tool(
    site_id: int, payload: ToolCreate, staff: CurrentSiteStaff, db: DbSession
) -> ToolRead:
    require_site_access(db, staff, site_id)

    tool = Tool(
        site_id=site_id,
        name=payload.name,
        inventory_count=payload.inventory_count,
        notes=payload.notes,
    )
    db.add(tool)
    try:
        db.commit()
    except IntegrityError:
        # uq_tools_site_name — two "Drill" rows would split the count and make
        # "one available" unanswerable.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This site already has a tool with that name",
        ) from None
    db.refresh(tool)
    return _read_tool(tool, 0)


@router.patch("/contractor/sites/{site_id}/tools/{tool_id}", response_model=ToolRead)
def edit_tool(
    site_id: int,
    tool_id: int,
    payload: ToolUpdate,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> ToolRead:
    tool = _get_tool(db, staff, site_id, tool_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tool, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This site already has a tool with that name",
        ) from None
    db.refresh(tool)
    return _read_tool(tool, _open_counts(db, [tool.id]).get(tool.id, 0))


@router.delete(
    "/contractor/sites/{site_id}/tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_tool(
    site_id: int, tool_id: int, staff: CurrentSiteStaff, db: DbSession
) -> Response:
    """Retire a tool. Refused while one is still out with somebody.

    Deleting would cascade the assignments away with it, and the record of who
    had the grinder is the one thing worth keeping about a grinder that never
    came back.
    """
    tool = _get_tool(db, staff, site_id, tool_id)

    out = _open_counts(db, [tool.id]).get(tool.id, 0)
    if out:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{tool.name} is still out with {out} "
                f"{'worker' if out == 1 else 'workers'}. Book it back in first."
            ),
        )

    db.delete(tool)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- checkout and return ----------------------------------------------------


@router.post(
    "/contractor/sites/{site_id}/tools/{tool_id}/assign",
    response_model=ToolAssignmentRead,
    status_code=status.HTTP_201_CREATED,
)
def assign_tool(
    site_id: int,
    tool_id: int,
    payload: ToolAssignmentCreate,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> ToolAssignmentRead:
    """Hand one of this tool to a worker."""
    tool = _get_tool(db, staff, site_id, tool_id)
    worker = require_site_worker(db, staff, payload.worker_id)

    if not tool.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{tool.name} has been retired and cannot be assigned",
        )

    open_rows = db.scalars(
        select(ToolAssignment).where(
            ToolAssignment.tool_id == tool.id,
            ToolAssignment.status == ToolAssignmentStatus.OUT,
        )
    ).all()

    if len(open_rows) >= tool.inventory_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"All {tool.inventory_count} of the site's {tool.name} "
                f"{'is' if tool.inventory_count == 1 else 'are'} already out."
            ),
        )

    if any(row.worker_id == worker.id for row in open_rows):
        # Two open rows for one worker and one tool would make the return
        # endpoint pick arbitrarily between them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{worker.name} already has {tool.name} checked out",
        )

    assignment = ToolAssignment(
        tool_id=tool.id,
        worker_id=worker.id,
        status=ToolAssignmentStatus.OUT,
        note=payload.note,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return _read_assignment(assignment)


@router.patch(
    "/contractor/tool-assignments/{assignment_id}", response_model=ToolAssignmentRead
)
def return_tool(
    assignment_id: int,
    payload: ToolReturn,
    staff: CurrentSiteStaff,
    db: DbSession,
) -> ToolAssignmentRead:
    """Book a tool back in, or write it off as lost."""
    if payload.status is ToolAssignmentStatus.OUT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A return must be recorded as returned or lost",
        )

    assignment = db.get(ToolAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool assignment not found"
        )
    # Proves the caller may act on this assignment, via the tool's site.
    require_site_access(db, staff, assignment.tool.site_id)

    if assignment.status is not ToolAssignmentStatus.OUT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This tool was already marked {assignment.status.value}",
        )

    assignment.status = payload.status
    # The database clock, like every other timestamp in the schema: an app
    # server whose clock has drifted must not stamp the record.
    assignment.return_time = func.now()
    if payload.note:
        assignment.note = payload.note

    db.commit()
    db.refresh(assignment)
    return _read_assignment(assignment)


# --- the end-of-shift badge -------------------------------------------------


@router.get("/contractor/tools/outstanding", response_model=OutstandingTools)
def read_outstanding_tools(
    staff: CurrentSiteStaff,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> OutstandingTools:
    """Everything still out across the sites this caller runs.

    Backs the dashboard warning badge. `overdue_count` is the subset checked
    out before today: a drill taken out this morning is not yet a problem, and
    a badge that lights up every working day is a badge nobody reads.

    A supervisor sees the badge for their own site only — the same screen,
    counting a smaller store.
    """
    reachable = accessible_site_ids(db, staff)
    if not reachable:
        return OutstandingTools(out_count=0, overdue_count=0, assignments=[])

    scope = (
        Tool.site_id.in_(reachable),
        ToolAssignment.status == ToolAssignmentStatus.OUT,
    )

    stmt = (
        select(ToolAssignment)
        .join(Tool, Tool.id == ToolAssignment.tool_id)
        .options(joinedload(ToolAssignment.tool), joinedload(ToolAssignment.worker))
        .where(*scope)
    )

    assignments = list(
        db.scalars(stmt.order_by(ToolAssignment.checkout_time).limit(limit)).all()
    )
    total = db.scalar(
        select(func.count())
        .select_from(ToolAssignment)
        .join(Tool, Tool.id == ToolAssignment.tool_id)
        .where(*scope)
    )

    # checkout_time is stored by the database in its own timezone-naive clock,
    # so "before today" is compared naively too rather than against an aware
    # `now` that would never line up.
    midnight = datetime.now(timezone.utc).astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    overdue = sum(1 for row in assignments if row.checkout_time < midnight)

    return OutstandingTools(
        out_count=total or 0,
        overdue_count=overdue,
        assignments=[_read_assignment(row) for row in assignments],
    )
